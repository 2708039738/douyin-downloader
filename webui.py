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
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

BROWSER = core.Browser(log=lambda m: print("[浏览器] " + m, flush=True))

_jobs = {}
_job_order = []
_jobs_lock = threading.Lock()
_queue = []
_queue_lock = threading.Condition()
_seq = 0
_thumbs = {}


def _new_job(url, watermark, quality, prefer_h264=True):
    global _seq
    _seq += 1
    job = {
        "id": str(_seq),
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
        "created": time.time(),
        "finished": 0,
    }
    with _jobs_lock:
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
        try:
            _update(job_id, status="parsing")
            target = core.normalize_url(job["url"])
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
            threading.Thread(target=_download_job, args=(job_id, info), daemon=True).start()
        except core.BrowserError as e:
            _update(job_id, status="error", error=str(e), finished=time.time())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _update(job_id, status="error", error="解析出错：%s" % e, finished=time.time())


def _download_job(job_id, info):
    started = time.time()
    try:
        if info["kind"] == "video":
            path = core.unique_path(DOWNLOAD_DIR, info["filename"], "mp4")

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

            path = core.download_gallery(info["files"], DOWNLOAD_DIR, info["filename"],
                                         on_progress=progress)

        size = os.path.getsize(path) if os.path.exists(path) else 0
        _update(job_id, status="done", path=path, filename=os.path.basename(path),
                size=size, received=size, finished=time.time(), speed=0)
        print("[完成] %s (%.1f MB)" % (os.path.basename(path), size / 1048576.0))
    except Exception as e:  # noqa: BLE001
        print("[失败] 下载出错：%s" % e)
        _update(job_id, status="error", error="下载失败：%s" % e, finished=time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinDL/1.0"

    def log_message(self, fmt, *args):
        pass

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
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
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
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/parse":
            data = self._read_json()
            urls = core.extract_urls(data.get("text") or "")
            if not urls:
                self._json({"error": "没识别到抖音链接"}, 400)
                return
            created = []
            for u in urls:
                job = _new_job(u, data.get("watermark"), data.get("quality"),
                               data.get("prefer_h264", True))
                created.append(job["id"])
                with _queue_lock:
                    _queue.append(job["id"])
                    _queue_lock.notify()
            self._json({"created": created})
            return
        if path == "/api/open-folder":
            if core.open_in_file_manager(DOWNLOAD_DIR):
                self._json({"ok": True})
            else:
                self._json({"error": "打不开文件管理器，请手动打开 downloads 文件夹"}, 500)
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
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
