# -*- coding: utf-8 -*-
"""
网页版界面（可选）：python app.py --ui

命令行/拖拽是主用法，这个只是给想点鼠标的时候用。
"""

import atexit
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

# 请求体上限。正常请求只有几百字节，超过这个数一律当成恶意/出错。
MAX_BODY = 1 << 20

BROWSER = core.Browser(log=lambda m: print("[浏览器] " + m, flush=True))

_jobs = {}
_job_order = []
_jobs_lock = threading.Lock()
_queue = []
_queue_lock = threading.Condition()
_seq = 0
_thumbs = {}

_cfg_lock = threading.Lock()
_cfg_cache = (None, None)   # (mtime, cfg)


def _config():
    """读一次 config.json，文件改了就重新读 —— 网页版也能用上清晰度等默认值。"""
    global _cfg_cache
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = None
    with _cfg_lock:
        if _cfg_cache[1] is None or _cfg_cache[0] != mtime:
            _cfg_cache = (mtime, core.load_config(CONFIG_PATH))
        return dict(_cfg_cache[1])


def _download_dir(cfg=None):
    """下载目录：config 里写了 out 就用它，否则程序目录/downloads。"""
    cfg = _config() if cfg is None else cfg
    folder = core.config_output_dir(BASE_DIR, cfg, default=DOWNLOAD_DIR)
    os.makedirs(folder, exist_ok=True)
    return folder


def _new_job(url, watermark, quality, prefer_h264=True):
    global _seq
    job = {
        "id": "",
        "url": url,
        "watermark": bool(watermark),
        "quality": quality or "auto",
        "prefer_h264": bool(prefer_h264),
        "status": "queued",
        "title": "",
        "author": "",
        "duration": 0,
        "quality_label": "",
        "size": 0,
        "received": 0,
        "speed": 0,
        "error": "",
        "filename": "",
        "is_gallery": False,
        "extras": [],
        "saved_to": "",
        "batch_count": 0,
        "created": time.time(),
        "finished": 0,
    }
    with _jobs_lock:
        # 自增必须和插入在同一把锁里：两个请求同时进来不能拿到同一个 id
        _seq += 1
        job["id"] = str(_seq)
        _jobs[job["id"]] = job
        _job_order.append(job["id"])
    return job


def _public(job):
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    out.pop("path", None)
    out["duration_text"] = core.fmt_duration(job.get("duration"))
    out["percent"] = round(job["received"] * 100.0 / job["size"], 1) if job.get("size") else 0
    out["thumb"] = bool(_thumbs.get(job["id"]))
    return out


def _update(job_id, **kw):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(kw)


def _expand_profile(job_id, target, job, cfg):
    """主页链接：先把作品列表取出来，再一个一个排进队列。"""
    _update(job_id, status="parsing", title="主页：正在读取作品列表…")
    try:
        limit = int(cfg.get("profile_limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    try:
        links = BROWSER.profile_links(target, limit=limit)
    except Exception as e:  # noqa: BLE001
        _update(job_id, status="error", error="读取主页失败：%s" % e, finished=time.time())
        return
    links = [u for u in (links or []) if u]
    if not links:
        _update(job_id, status="error",
                error="主页里没读到作品（可能需要登录，或者主页不可见）",
                finished=time.time())
        return
    for u in links:
        child = _new_job(u, job.get("watermark"), job.get("quality"),
                         job.get("prefer_h264", True))
        with _queue_lock:
            _queue.append(child["id"])
            _queue_lock.notify()
    # 父任务本身没有文件，只用来告诉界面「拆出了 N 个」
    _update(job_id, status="done", title="主页：%d 个作品已加入队列" % len(links),
            batch_count=len(links), received=1, size=1, speed=0, saved_to="",
            finished=time.time())
    print("[主页] %d 个作品已加入队列" % len(links))


def _resolve_worker():
    while True:
        with _queue_lock:
            while not _queue:
                _queue_lock.wait()
            job_id = _queue.pop(0)
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job or job["status"] != "queued":
            continue
        cfg = _config()
        try:
            _update(job_id, status="parsing")
            target = core.normalize_url(job["url"])
            if core.is_profile_url(target):
                _expand_profile(job_id, target, job, cfg)
                continue
            aweme = BROWSER.detail(target)
            info = core.describe(aweme, watermark=job["watermark"], quality=job["quality"],
                                 prefer_h264=job.get("prefer_h264", True))
            cover = info.get("cover_url")
            if cover:
                try:
                    _thumbs[job_id] = core.fetch_bytes(cover, limit=2 * 1024 * 1024)
                except Exception:
                    pass
            _update(job_id, status="downloading", title=info["title"], author=info["author"],
                    duration=info["duration"], quality_label=info.get("quality_label") or "",
                    size=info.get("size") or 0, filename=info["filename"],
                    is_gallery=info.get("is_gallery", False))
            threading.Thread(target=_download_job, args=(job_id, info, cfg), daemon=True).start()
        except core.BrowserError as e:
            _update(job_id, status="error", error=str(e), finished=time.time())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _update(job_id, status="error", error="解析出错：%s" % e, finished=time.time())


def _download_job(job_id, info, cfg=None):
    started = time.time()
    cfg = _config() if cfg is None else cfg
    folder = _download_dir(cfg)
    try:
        if info["kind"] == "video":
            path = core.unique_path(folder, info["filename"], "mp4")

            def progress(got, total):
                with _jobs_lock:
                    job = _jobs.get(job_id)
                    if not job:
                        return
                    job["received"] = got
                    if total:
                        job["size"] = total
                    job["speed"] = int(got / max(time.time() - started, 0.001))

            core.download_stream(info["url"], path, on_progress=progress)
        else:
            def progress(done, total):
                with _jobs_lock:
                    job = _jobs.get(job_id)
                    if job:
                        job["received"] = done
                        job["size"] = total

            path = core.download_gallery(info["files"], folder, info["filename"],
                                         on_progress=progress)

        # 视频下好了再补封面 / 文案 / MP3 —— 和命令行走的是同一套 core 函数
        extras = core.save_extras(info, folder, with_meta=cfg.get("save_meta", True))
        if cfg.get("save_audio") and info.get("kind") == "video":
            for apath, note in core.save_audio(info, path, folder, core.find_ffmpeg()):
                extras.append(apath)
                print("[音频] %s（%s）" % (os.path.basename(apath), note))

        size = os.path.getsize(path) if os.path.exists(path) else 0
        _update(job_id, status="done", path=path, filename=os.path.basename(path),
                size=size, received=size, finished=time.time(), speed=0,
                extras=[os.path.basename(p) for p in extras],
                saved_to=os.path.basename(folder))
        print("[完成] %s (%.1f MB)" % (os.path.basename(path), size / 1048576.0))
    except Exception as e:  # noqa: BLE001
        print("[失败] 下载出错：%s" % e)
        _update(job_id, status="error", error="下载失败：%s" % e, finished=time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinDL/1.0"

    # 只认本机地址。别的网站即使把域名解析到 127.0.0.1（DNS rebinding），
    # 它发来的 Host 也是自己的域名，会被这里挡掉。
    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):            # [::1]:8765
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:          # 127.0.0.1:8765
            host = host.split(":", 1)[0]
        return host in self._LOCAL_HOSTS

    def _origin_ok(self):
        # 有的客户端会重复发 Origin，只要有一个不是本机就拒
        origins = self.headers.get_all("Origin") or []
        if not origins:
            return True                     # 命令行 / 脚本本来就不带 Origin
        for origin in origins:
            parsed = urllib.parse.urlparse(origin or "")
            if parsed.scheme != "http" or (parsed.hostname or "") not in self._LOCAL_HOSTS:
                return False
        return True

    def _drain(self):
        """把没读的请求体读掉，这样回 403 时连接能干净关闭，客户端不会只看到连接被重置。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if 0 < length <= MAX_BODY:
            try:
                self.rfile.read(length)
            except Exception:
                pass

    def _reject_foreign(self):
        """不是本机页面发来的请求，就拒绝。

        为什么需要：本地服务没有密码，任何网页都能往 127.0.0.1:8765 发请求。
        不加判断的话，你随便打开一个网站，它就能偷偷加下载任务、删你的文件。
        """
        if self._host_ok() and self._origin_ok():
            return False
        self._drain()
        self._json({"error": "只接受本机页面发来的请求"}, 403)
        return True

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY:
            # 别为了一个畸形的 Content-Length 把内存吃光
            self.close_connection = True
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "ignore"))
        except Exception:
            return {}

    def _serve_file(self, path):
        if not os.path.isfile(path):
            self._json({"error": "文件不存在"}, 404)
            return
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition", "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                         % (ascii_name, urllib.parse.quote(name)))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    break

    def do_GET(self):
        if self._reject_foreign():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/api/jobs":
            with _jobs_lock:
                jobs = [_public(_jobs[i]) for i in _job_order if i in _jobs]
            self._json({"jobs": jobs})
            return
        if path == "/api/config":
            # 给前端初始化表单用：网页版也遵守 config.json 里的默认值
            cfg = _config()
            self._json({k: cfg.get(k) for k in
                        ("quality", "prefer_h264", "save_meta", "save_audio", "profile_limit")})
            return
        if path == "/api/thumb":
            data = _thumbs.get((query.get("id") or [""])[0])
            if not data:
                self._send(404, b"", "image/jpeg")
                return
            self._send(200, data, "image/jpeg", cache="public, max-age=3600")
            return
        if path == "/api/file":
            with _jobs_lock:
                job = _jobs.get((query.get("id") or [""])[0])
            if not job or not job.get("path"):
                self._json({"error": "文件还没准备好"}, 404)
                return
            self._serve_file(job["path"])
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self._reject_foreign():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/parse":
            data = self._read_json()
            urls = core.extract_urls(data.get("text") or "")
            if not urls:
                self._json({"error": "没识别到抖音链接"}, 400)
                return
            cfg = _config()
            # 前端没显式指定时，用 config.json 里的默认值（以前网页版完全忽略配置）
            quality = data.get("quality") or cfg.get("quality") or "auto"
            prefer_h264 = data.get("prefer_h264", cfg.get("prefer_h264", True))
            created = []
            for u in urls:
                job = _new_job(u, data.get("watermark"), quality, prefer_h264)
                created.append(job["id"])
                with _queue_lock:
                    _queue.append(job["id"])
                    _queue_lock.notify()
            self._json({"created": created})
            return
        if path == "/api/open-folder":
            folder = _download_dir()
            if core.open_in_file_manager(folder):
                self._json({"ok": True, "folder": os.path.basename(folder)})
            else:
                self._json({"error": "打不开文件管理器，请手动打开：%s" % folder}, 500)
            return
        if path == "/api/retry":
            jid = str(self._read_json().get("id") or "")
            _update(jid, status="queued", error="", received=0, speed=0)
            with _queue_lock:
                _queue.append(jid)
                _queue_lock.notify()
            self._json({"ok": True})
            return
        if path == "/api/remove":
            jid = str(self._read_json().get("id") or "")
            with _jobs_lock:
                job = _jobs.pop(jid, None)
                if jid in _job_order:
                    _job_order.remove(jid)
            _thumbs.pop(jid, None)
            if job and job.get("path") and os.path.exists(job["path"]):
                try:
                    os.remove(job["path"])
                except Exception:
                    pass
            self._json({"ok": True})
            return
        self._json({"error": "未知接口"}, 404)


def _running_on(port):
    """看看这个端口是不是已经有一个网页版在跑。"""
    try:
        with core._local_get("http://127.0.0.1:%d/api/jobs" % port, timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def open_browser(url):
    """几种方式轮着试，总有一种能把浏览器叫起来。"""
    try:
        if webbrowser.open(url, new=2):
            return True
    except Exception:
        pass
    exe = core.find_browser()
    if exe:
        try:
            subprocess.Popen([exe, url], close_fds=True)
            return True
        except Exception:
            pass
    return core.open_in_file_manager(url)


def _console_commands(url):
    """在窗口里输入 o 回车，可以再把浏览器叫出来一次。"""
    while True:
        try:
            line = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        except Exception:
            return
        if line in ("o", "open", "打开", "b"):
            print("正在打开：%s" % url)
            open_browser(url)


def _write_shortcut(url):
    """把当前地址写成一个 .url 快捷方式，浏览器没弹出来时双击它就行。"""
    try:
        path = os.path.join(BASE_DIR, "打开网页版.url")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[InternetShortcut]\nURL=%s\n" % url)
        return path
    except Exception:
        return None


def serve(port=8765):
    folder = _download_dir()   # 顺便确保目录存在

    # 已经开着一个的话，直接把那个页面打开，不再起第二个
    for p in range(port, port + 8):
        if _running_on(p):
            url = "http://127.0.0.1:%d" % p
            _write_shortcut(url)
            print()
            print("已经有一个网页版在运行：%s" % url)
            print("这就给你打开它。没反应的话，双击同目录的「打开网页版.url」。")
            open_browser(url)
            return

    threading.Thread(target=_resolve_worker, daemon=True).start()
    atexit.register(BROWSER.close)

    server = None
    for offset in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port + offset), Handler)
            port += offset
            break
        except OSError:
            continue
    if not server:
        print("端口都被占用了")
        return

    url = "http://127.0.0.1:%d" % port
    _write_shortcut(url)
    print()
    print("=" * 56)
    print("  网页版已启动")
    print("  地址：%s" % url)
    print("  保存到：%s" % folder)
    print("  浏览器没自动弹出来的话：双击同目录的「打开网页版.url」")
    print("  或者把上面那行地址复制到浏览器打开")
    print("  也可以在这里输入 o 再按回车，让我再开一次")
    print("  这个窗口别关，关掉就等于退出")
    print("=" * 56)
    threading.Timer(1.0, lambda: open_browser(url)).start()
    threading.Thread(target=_console_commands, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        BROWSER.close()
