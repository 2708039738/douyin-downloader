# -*- coding: utf-8 -*-
"""
抖音视频解析与下载核心。

思路：
  抖音的详情接口必须带 a_bogus / msToken 等签名参数，纯 HTTP 请求会被 403。
  所以这里用本机已安装的 Edge / Chrome 起一个无头实例，让抖音自己的 JS 去算签名，
  再通过 DevTools 协议（CDP）把 aweme/detail 的响应体截下来，
  里面有 play_addr（无水印）和 download_addr（带水印）两条地址。

  每条作品开一个独立标签页（共用一个浏览器进程），用完就关，
  这样既不会互相干扰 SPA 状态，也不会堆内存。

依赖：只用标准库，不需要 pip 安装任何东西。
"""

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
]

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fa5\"'<>，。、；：！？）】」]+", re.I)
ID_RE = re.compile(r"(?:/video/|/note/|/share/video/|/share/note/|modal_id=|item_ids=|/slides/)(\d{15,25})")
USER_RE = re.compile(r"douyin\.com/user/([A-Za-z0-9_\-]+)")

# 清理残留进程时，只认这些名字里带的浏览器。名字对不上就不杀，
# 免得 PID 被系统复用以后误杀你别的程序（issue #3）。
BROWSER_KEYWORDS = ("msedge", "chrome", "chromium", "microsoft edge", "google chrome")


# --------------------------------------------------------------------------- #
# 配置（命令行和网页版共用一份，别再各写一遍）
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "quality": "auto",       # auto / 1080 / 720 / 540
    "prefer_h264": True,     # 优先 H.264，兼容性最好
    "save_meta": True,       # 保存封面 + 文案
    "save_audio": False,     # 顺便提取 MP3
    "profile_limit": 20,     # 主页链接最多取多少个作品
    "out": "",               # 下载目录，留空就是程序目录下的 downloads
}


def load_config(path, wizard=None):
    """读 config.json，缺的字段用默认值补上。

    - 文件不存在 / 读坏了：有 wizard 就调它（首次运行的问答），没有就直接用默认值
    - 网页版传 wizard=None，这样它永远能起来，不会卡在等输入
    """
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = dict(DEFAULT_CONFIG)
                cfg.update(json.load(f))
                return cfg
        except Exception:
            pass
    if wizard is not None:
        return wizard()
    return dict(DEFAULT_CONFIG)


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def config_output_dir(base_dir, cfg, override=None, default=None):
    """决定下载到哪个目录：命令行 --out > config 里的 out > 默认目录。"""
    raw = (override or (cfg or {}).get("out") or "").strip()
    if not raw:
        return default or os.path.join(base_dir, "downloads")
    return raw if os.path.isabs(raw) else os.path.join(base_dir, raw)


# --------------------------------------------------------------------------- #
# 链接处理
# --------------------------------------------------------------------------- #
def extract_urls(text):
    """从分享文案里抠出所有链接（分享文案通常是一堆汉字夹着一条链接）。"""
    found = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;)]}'\"")
        if any(h in u for h in ("douyin.com", "iesdouyin.com", "amemv.com")):
            if u not in found:
                found.append(u)
    return found


def extract_aweme_id(url):
    m = ID_RE.search(url or "")
    return m.group(1) if m else None


def is_profile_url(url):
    return bool(USER_RE.search(url or ""))


def normalize_url(url):
    """
    把各种形态统一成可以直接打开的页面地址。
    短链 v.douyin.com/xxx 先走一次 HTTP 重定向拿到作品 id，避免落到已经失效的分享页。
    主页链接原样返回。
    """
    url = (url or "").strip()
    if not url:
        return None
    if is_profile_url(url):
        return url if url.startswith("http") else "https://" + url

    aweme_id = extract_aweme_id(url)
    if not aweme_id and "v.douyin.com" in url:
        final = _follow_redirect(url)
        if final:
            if is_profile_url(final):
                return final
            aweme_id = extract_aweme_id(final)
            if aweme_id:
                kind = "note" if "/note/" in final else "video"
                return f"https://www.douyin.com/{kind}/{aweme_id}"
    if aweme_id:
        kind = "note" if ("/note/" in url or "/slides/" in url) else "video"
        return f"https://www.douyin.com/{kind}/{aweme_id}"
    return url if url.startswith("http") else "https://" + url


def _follow_redirect(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                           "Mobile/15E148 Safari/604.1"),
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.geturl()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 极简 WebSocket 客户端（只够用来跟 CDP 说话）
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self, url, timeout=10):
        m = re.match(r"ws://([^:/]+):(\d+)(/.*)", url)
        if not m:
            raise ValueError("不支持的调试地址: %s" % url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(handshake.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手被拒绝")
            head += chunk
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("WebSocket 握手失败: %s" % head[:80])
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self._lock = threading.Lock()

    def send(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode()
        header = bytearray([0x81])
        n = len(data)
        mask = os.urandom(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._lock:
            self.sock.sendall(bytes(header) + mask + masked)

    def recv(self, timeout=1.0):
        """返回一条消息；超时返回 None。"""
        deadline = time.time() + timeout
        while True:
            if len(self.buf) >= 2:
                b0, b1 = self.buf[0], self.buf[1]
                opcode = b0 & 0x0F
                ln = b1 & 0x7F
                offset = 2
                if ln == 126:
                    if len(self.buf) >= 4:
                        ln = struct.unpack(">H", self.buf[2:4])[0]
                        offset = 4
                    else:
                        ln = None
                elif ln == 127:
                    if len(self.buf) >= 10:
                        ln = struct.unpack(">Q", self.buf[2:10])[0]
                        offset = 10
                    else:
                        ln = None
                if ln is not None and len(self.buf) >= offset + ln:
                    payload = self.buf[offset:offset + ln]
                    self.buf = self.buf[offset + ln:]
                    if opcode == 0x8:
                        raise ConnectionError("调试连接已关闭")
                    if opcode in (0x1, 0x2):
                        return json.loads(payload.decode("utf-8", "ignore"))
                    continue  # ping/pong 之类直接丢掉
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                raise ConnectionError("调试连接已断开")
            self.buf += chunk

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# 访问本机调试端口时必须绕开系统代理，否则会被代理拦成 502
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _local_get(url, timeout=5):
    return _LOCAL.open(url, timeout=timeout)


def _local_put(url, timeout=10):
    req = urllib.request.Request(url, method="PUT")
    return _LOCAL.open(req, timeout=timeout)


def _local_post(url, timeout=10):
    req = urllib.request.Request(url, method="POST")
    return _LOCAL.open(req, timeout=timeout)


def _pid_file():
    return os.path.join(tempfile.gettempdir(), "dydl_browser.pid")


def _parse_tasklist(text, pid):
    """从 tasklist 的输出里挑出 pid 那一行的进程名，挑不出来返回空串。

    匹配方式按「第二列必须正好等于 pid」来，不能直接 `pid in text`——
    内存占用里也可能出现同样的数字。
    """
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) == pid:
            return parts[0].lower()
    return ""


def _process_name(pid):
    """拿到 pid 对应的进程名（小写）。拿不到就返回空串，调用方据此「不杀」。"""
    try:
        if sys.platform == "win32":
            listed = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=0x08000000, timeout=10)
            return _parse_tasklist(listed.stdout.decode("latin-1", "ignore"), pid)
        out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=5)
        return out.stdout.decode("utf-8", "ignore").strip().lower()
    except Exception:
        return ""


def _looks_like_browser(name):
    name = (name or "").lower()
    return bool(name) and any(k in name for k in BROWSER_KEYWORDS)


def _kill_pid(pid):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000, timeout=10)
    else:
        os.kill(pid, 15)


def kill_stale_browser():
    """
    上次如果被强杀，无头浏览器会留在后台。

    Windows 用 tasklist / taskkill，其他系统用 ps / kill。
    两边都先确认「这个 pid 真的是浏览器」再动手——pid 会被系统复用，
    直接 kill 有可能打到你自己刚开的程序上。
    """
    path = _pid_file()
    if not os.path.isfile(path):
        return
    try:
        # 太久以前的记录不认，避免 PID 被别的程序复用时误杀
        if time.time() - os.path.getmtime(path) > 1800:
            os.remove(path)
            return
    except Exception:
        pass
    try:
        with open(path, "r") as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        pid = 0
    try:
        if not pid:
            return
        name = _process_name(pid)
        if not _looks_like_browser(name):
            # 拿不到名字（没 ps）或者名字对不上，一律不杀
            return
        _kill_pid(pid)
    except Exception:
        pass
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def find_browser():
    for path in BROWSER_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    for name in ("msedge", "chrome", "google-chrome", "chromium", "microsoft-edge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def open_in_file_manager(path):
    """用系统自带的文件管理器打开目录（或链接），返回是否成功。

    Windows 用 os.startfile，macOS 用 open，Linux 用 xdg-open。
    这三个命令都是系统自带的，不需要装东西。
    """
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 浏览器（拿签名数据）
# --------------------------------------------------------------------------- #
class BrowserError(RuntimeError):
    pass


class Browser:
    def __init__(self, log=None):
        self.log = log or (lambda *a: None)
        self.exe = find_browser()
        self.proc = None
        self.port = None
        self.profile = None
        self.lock = threading.Lock()
        self._msg_id = 0
        self._cache = {}
        self._started = False

    # -- 生命周期 ---------------------------------------------------------- #
    def start(self):
        if self._started:
            return self
        if not self.exe:
            raise BrowserError("没有找到 Edge 或 Chrome，请先安装其中一个浏览器")
        self.port = _free_port()
        self.profile = tempfile.mkdtemp(prefix="dydl_profile_")
        args = [
            self.exe,
            "--headless=new",
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % self.profile,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--disable-breakpad",
            "--mute-audio",
            "--disable-gpu",
            "--renderer-process-limit=2",
            "--window-size=1280,860",
            "--lang=zh-CN",
            "about:blank",
        ]
        creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=creationflags)
        try:
            with open(_pid_file(), "w") as f:
                f.write(str(self.proc.pid))
        except Exception:
            pass

        deadline = time.time() + 25
        version = None
        while time.time() < deadline:
            try:
                with _local_get("http://127.0.0.1:%d/json/version" % self.port, timeout=1) as r:
                    version = json.loads(r.read().decode("utf-8", "ignore"))
                break
            except Exception:
                time.sleep(0.2)
        if not version:
            self.close()
            raise BrowserError("浏览器启动失败（调试端口没起来）")
        self.log("浏览器已就绪：%s" % version.get("Browser", "Chromium"))
        self._started = True
        self._warmup()
        return self

    def _warmup(self, seconds=5.0):
        """先开一次首页，把 ttwid 之类的 cookie 拿下来，后面解析才不会被拦。"""
        try:
            tid, ws = self._open_tab()
        except Exception:
            return
        try:
            ws.send({"id": 1, "method": "Page.enable"})
            ws.send({"id": 2, "method": "Page.navigate", "params": {"url": "https://www.douyin.com/"}})
            end = time.time() + seconds
            while time.time() < end:
                msg = ws.recv(0.4)
                if msg and msg.get("method") == "Page.loadEventFired":
                    break
            time.sleep(0.6)
        except Exception:
            pass
        finally:
            ws.close()
            self._close_tab(tid)

    def close(self):
        self._started = False
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        try:
            os.remove(_pid_file())
        except Exception:
            pass
        if self.profile:
            for _ in range(5):
                try:
                    shutil.rmtree(self.profile, ignore_errors=True)
                    break
                except Exception:
                    time.sleep(0.3)
            self.profile = None

    # -- 标签页 ------------------------------------------------------------ #
    def ensure_alive(self):
        """浏览器要是被关掉/崩了，这里自己重启一个。"""
        if self._started and self.proc and self.proc.poll() is None:
            return
        self.close()
        self.start()

    def _with_tab(self, fn):
        """开一个标签页交给 fn，失败（浏览器已死）就重启一次再试。"""
        self.ensure_alive()
        try:
            tid, ws = self._open_tab()
        except Exception:
            self.close()
            self.start()
            tid, ws = self._open_tab()
        try:
            return fn(ws)
        finally:
            ws.close()
            self._close_tab(tid)

    def _open_tab(self):
        base = "http://127.0.0.1:%d" % self.port
        last = None
        for opener in (_local_put, _local_post):
            try:
                with opener(base + "/json/new?about:blank") as r:
                    tab = json.loads(r.read().decode("utf-8", "ignore"))
                return tab["id"], _WS(tab["webSocketDebuggerUrl"], timeout=15)
            except Exception as e:  # noqa: BLE001
                last = e
        raise BrowserError("无法创建浏览器标签页: %s" % last)

    def _close_tab(self, tab_id):
        try:
            _local_get("http://127.0.0.1:%d/json/close/%s" % (self.port, tab_id), timeout=5).read()
        except Exception:
            pass

    def _eval_on(self, ws, expression, timeout=6):
        # 用一个独立的高位 id 段，避免和 Network/Page 那些 1、2、3 撞车
        self._msg_id += 1
        mid = 900000 + self._msg_id
        ws.send({"id": mid, "method": "Runtime.evaluate",
                 "params": {"expression": expression, "returnByValue": True}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = ws.recv(0.5)
            if not msg or msg.get("id") != mid:
                continue
            result = (msg.get("result") or {}).get("result") or {}
            if "value" in result:
                return result["value"]
            return None
        return None

    # -- 作品详情 ---------------------------------------------------------- #
    def detail(self, url, timeout=25):
        aweme_id = extract_aweme_id(url)
        if aweme_id and aweme_id in self._cache:
            return self._cache[aweme_id]

        with self.lock:
            aweme = self._with_tab(lambda ws: self._capture(ws, url, aweme_id, timeout))
        key = str(aweme.get("aweme_id") or aweme_id or "")
        if key:
            self._cache[key] = aweme
        return aweme

    def _capture(self, ws, url, aweme_id, timeout):
        ws.send({"id": 1, "method": "Network.enable"})
        # 抖音注册了 Service Worker，请求会从页面 target 里"隐身"，
        # 绕过它才能截到 aweme/detail 的响应体
        ws.send({"id": 2, "method": "Network.setBypassServiceWorker", "params": {"bypass": True}})
        ws.send({"id": 3, "method": "Page.enable"})
        ws.send({"id": 4, "method": "Page.navigate", "params": {"url": url}})

        pending = {}
        page_url = ""
        deadline = time.time() + timeout
        started = time.time()
        redirect_checked = False
        msg_id = 100
        target_id = aweme_id
        renav_stage = 0
        renav_at = 0.0

        while time.time() < deadline:
            msg = ws.recv(0.5)
            now = time.time()

            # 短链没跳到作品页（落在分享页/首页）时，先清空页面再跳一次，
            # 同一标签页里直接跳会被 SPA 接管，detail 请求不会重新发
            if renav_stage == 0 and not target_id and page_url and now - started > 4:
                found = extract_aweme_id(page_url)
                if found:
                    target_id = found
                    renav_stage = 1
                    renav_at = now
                    ws.send({"id": 60, "method": "Page.navigate",
                             "params": {"url": "about:blank"}})
            elif renav_stage == 1 and now - renav_at > 0.6:
                renav_stage = 2
                started = now
                deadline = max(deadline, now + 18)
                ws.send({"id": 61, "method": "Page.navigate",
                         "params": {"url": "https://www.douyin.com/video/" + str(target_id)}})

            if msg is None:
                # 作品不存在时会被踢回首页，别一直傻等
                if target_id and not redirect_checked and now - started > 6:
                    redirect_checked = True
                    if page_url and target_id not in page_url:
                        raise BrowserError("这个链接打不开：作品可能已删除、设为私密，或者需要登录")
                continue
            method = msg.get("method")
            if method == "Page.frameNavigated":
                frame = msg.get("params", {}).get("frame", {})
                if not frame.get("parentId"):
                    page_url = frame.get("url", "")
                continue
            if method == "Network.responseReceived":
                resp = msg.get("params", {}).get("response", {})
                u = resp.get("url", "")
                if any(k in u for k in ("aweme/v1/web/aweme/detail",
                                        "aweme/v1/web/note/detail",
                                        "/web/slides/")):
                    msg_id += 1
                    pending[msg_id] = True
                    ws.send({"id": msg_id, "method": "Network.getResponseBody",
                             "params": {"requestId": msg["params"]["requestId"]}})
                continue
            if msg.get("id") in pending:
                pending.pop(msg["id"], None)
                body = (msg.get("result") or {}).get("body")
                if not body or "aweme_detail" not in body:
                    continue
                try:
                    data = json.loads(body)
                except Exception:
                    continue
                aweme = data.get("aweme_detail")
                if aweme:
                    # 首页推荐流也会发 detail 请求，别把别人的视频当成你的
                    if target_id and str(aweme.get("aweme_id") or "") != target_id:
                        continue
                    return aweme
                filter_detail = data.get("filter_detail") or {}
                reason = filter_detail.get("detail_msg") or filter_detail.get("notice") or ""
                raise BrowserError("抖音没有返回这个作品的数据%s" %
                                   ("：" + reason if reason else "（可能已删除、设为私密，或需要登录）"))

        hint = ""
        try:
            hint = self._eval_on(ws, "document.body ? document.body.innerText.slice(0,80) : ''", 3) or ""
        except Exception:
            pass
        raise BrowserError("解析超时，没拿到视频数据。%s" %
                           ("页面提示：" + re.sub(r"\s+", " ", hint)[:60] if hint else "请检查网络后重试"))

    # -- 主页作品列表 ------------------------------------------------------ #
    def profile_links(self, url, limit=20, timeout=70):
        """打开主页，滚动加载，收集作品链接。"""
        with self.lock:
            return self._with_tab(lambda ws: self._collect_links(ws, url, limit, timeout))

    def _collect_links(self, ws, url, limit, timeout):
        ws.send({"id": 1, "method": "Page.enable"})
        ws.send({"id": 2, "method": "Page.navigate", "params": {"url": url}})
        js = ("(function(){window.scrollTo(0,document.body.scrollHeight);"
              "var a=document.querySelectorAll('a[href*=\"/video/\"]');"
              "var out=[];for(var i=0;i<a.length;i++){out.push(a[i].href);}"
              "return out.join('\\n');})()")
        links = []
        deadline = time.time() + timeout
        idle = 0
        last = 0
        while time.time() < deadline:
            if time.time() - last > 1.3:
                last = time.time()
                raw = self._eval_on(ws, js, timeout=5) or ""
                before = len(links)
                for line in raw.split("\n"):
                    m = re.search(r"/video/(\d{15,25})", line)
                    if not m:
                        continue
                    u = "https://www.douyin.com/video/" + m.group(1)
                    if u not in links:
                        links.append(u)
                idle = idle + 1 if len(links) == before else 0
                if len(links) >= limit or idle >= 4:
                    break
        return links[:limit]


# --------------------------------------------------------------------------- #
# 数据整理
# --------------------------------------------------------------------------- #
def safe_name(text, limit=70):
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit] or "douyin"


def short_desc(desc, limit=34):
    """文案里常带一串 #话题，文件名里去掉更清爽。"""
    cleaned = re.sub(r"#\S+", " ", desc or "")
    cleaned = re.sub(r"@\S+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_·.。")
    return safe_name(cleaned or desc, limit)


def _nominal(addr):
    """抖音给的宽高是真实像素（竖屏 1080x1920），取短边当"多少 P"更准。"""
    w = int(addr.get("width") or 0)
    h = int(addr.get("height") or 0)
    if w and h:
        return min(w, h)
    return max(w, h)


def pick_media(aweme, watermark=False, quality="auto", prefer_h264=True):
    """
    返回 (url, 宽, 高, 预计大小, 说明)。
    无水印 → video.bit_rate 里挑清晰度；带水印 → video.download_addr（抖音官方那一版）。
    """
    video = aweme.get("video") or {}
    if watermark:
        addr = video.get("download_addr") or {}
        urls = addr.get("url_list") or []
        if not urls:
            raise BrowserError("这个作品没有带水印的下载地址，换成无水印试试")
        return urls[0], addr.get("width"), addr.get("height"), addr.get("data_size"), "官方带水印"

    gears = []
    for g in (video.get("bit_rate") or []):
        addr = g.get("play_addr") or {}
        if not addr.get("url_list"):
            continue
        gears.append({
            "res": _nominal(addr),
            "h265": bool(g.get("is_h265") or g.get("is_bytevc1")),
            "addr": addr,
        })
    # 分辨率优先，同分辨率优先 H.264（兼容性更好）
    gears.sort(key=lambda g: (g["res"], 0 if g["h265"] else 1), reverse=True)
    if prefer_h264:
        h264 = [g for g in gears if not g["h265"]]
        if h264:
            gears = h264

    target = None
    if quality not in (None, "", "auto", "max"):
        digits = re.sub(r"\D", "", str(quality))
        if digits:
            target = int(digits)

    picked = None
    if gears:
        if target:
            ok = [g for g in gears if g["res"] <= target]
            picked = ok[0] if ok else gears[-1]
        else:
            picked = gears[0]

    if picked:
        addr = picked["addr"]
        return (addr["url_list"][0], addr.get("width"), addr.get("height"),
                addr.get("data_size"), "%dP%s" % (picked["res"], " H.265" if picked["h265"] else ""))

    addr = video.get("play_addr") or {}
    urls = addr.get("url_list") or []
    if not urls:
        raise BrowserError("没有找到可下载的视频地址")
    return urls[0], addr.get("width"), addr.get("height"), addr.get("data_size"), "原画"


def _fmt_time(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return ""


def describe(aweme, watermark=False, quality="auto", prefer_h264=True):
    """把 aweme_detail 压成命令行/界面要用的信息。"""
    author = ((aweme.get("author") or {}).get("nickname") or "未知作者").strip()
    desc = (aweme.get("desc") or "").strip()
    video = aweme.get("video") or {}
    duration = int(video.get("duration") or aweme.get("duration") or 0)
    cover_list = (video.get("cover") or {}).get("url_list") or []
    stat = aweme.get("statistics") or {}
    images = aweme.get("images") or []
    aweme_id = str(aweme.get("aweme_id") or "")

    base = "%s-%s" % (safe_name(author, 20), short_desc(desc))
    info = {
        "author": author,
        "title": desc or "无标题作品",
        "desc": desc,
        "duration": duration,
        "aweme_id": aweme_id,
        "page_url": "https://www.douyin.com/video/%s" % aweme_id if aweme_id else "",
        "create_time": _fmt_time(aweme.get("create_time")),
        "stats": {
            "digg": stat.get("digg_count") or 0,
            "comment": stat.get("comment_count") or 0,
            "collect": stat.get("collect_count") or 0,
            "share": stat.get("share_count") or 0,
        },
        "is_gallery": bool(images),
        "image_count": len(images),
        "cover_url": cover_list[0] if cover_list else None,
        "base": base,
        "music": (((aweme.get("music") or {}).get("play_url") or {}).get("url_list") or [None])[0],
        "music_title": (aweme.get("music") or {}).get("title") or "",
    }

    if images:
        info["kind"] = "gallery"
        info["ext"] = "zip"
        info["files"] = [((img.get("url_list") or [None])[0]) for img in images]
        info["quality_label"] = "%d 张图" % len(images)
        info["size"] = 0
        info["filename"] = "%s[图集]" % base
        return info

    url, w, h, size, label = pick_media(aweme, watermark=watermark, quality=quality,
                                        prefer_h264=prefer_h264)
    info.update({
        "kind": "video",
        "ext": "mp4",
        "url": url,
        "width": w,
        "height": h,
        "size": int(size or 0),
        "quality_label": label,
        "filename": "%s[%s]" % (base, "带水印" if watermark else "无水印"),
    })
    return info


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "*/*",
}


def download_stream(url, path, on_progress=None, timeout=60, headers=None):
    """把 url 下载到 path。

    下载中断（断网、服务器提前断开）时抛 BrowserError，并把半截文件删掉，
    避免留下一个「看起来下载成功、其实放不了」的坏文件。
    """
    req = urllib.request.Request(url, headers=headers or HEADERS)
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
        if not got:
            # 有的服务器回 200 却一个字节都不给，这种也算失败
            raise BrowserError("下载到的内容是空的（0 字节），请重试")
        if total and got != total:
            raise BrowserError("下载不完整：只拿到 %d / %d 字节，已删除半截文件，请重试"
                               % (got, total))
        return got
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def unique_path(folder, base, ext):
    path = os.path.join(folder, "%s.%s" % (base, ext))
    n = 1
    while os.path.exists(path):
        path = os.path.join(folder, "%s(%d).%s" % (base, n, ext))
        n += 1
    return path


def download_gallery(urls, folder, base, on_progress=None):
    """图集：逐张下下来打包成 zip。"""
    path = unique_path(folder, base, "zip")
    tmpdir = tempfile.mkdtemp(prefix="dydl_img_")
    try:
        files = []
        for idx, u in enumerate(urls, 1):
            if not u:
                continue
            target = os.path.join(tmpdir, "%02d.jpg" % idx)
            try:
                download_stream(u, target, timeout=45)
                files.append(target)
            except Exception:
                continue
            if on_progress:
                on_progress(idx, len(urls))
        if not files:
            raise BrowserError("图集下载失败，一张图都没拿到")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            for f in files:
                zf.write(f, os.path.basename(f))
        return path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_bytes(url, timeout=15, limit=3 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def fmt_duration(ms):
    if not ms:
        return ""
    s = int(ms // 1000)
    return "%d:%02d" % (s // 60, s % 60)


def fmt_count(n):
    n = int(n or 0)
    if n >= 100000000:
        return "%.1f亿" % (n / 100000000.0)
    if n >= 10000:
        return "%.1f万" % (n / 10000.0)
    return str(n)


def save_meta(info, folder):
    """把文案、作者、数据存成 txt。"""
    path = unique_path(folder, info["base"] + "[信息]", "txt")
    lines = [
        "标题：%s" % info["title"],
        "作者：@%s" % info["author"],
        "链接：%s" % info.get("page_url", ""),
        "作品ID：%s" % info.get("aweme_id", ""),
        "发布时间：%s" % info.get("create_time", ""),
        "时长：%s" % fmt_duration(info.get("duration")),
        "清晰度：%s" % info.get("quality_label", ""),
        "数据：点赞 %s · 评论 %s · 收藏 %s · 分享 %s" % (
            fmt_count(info["stats"]["digg"]), fmt_count(info["stats"]["comment"]),
            fmt_count(info["stats"]["collect"]), fmt_count(info["stats"]["share"])),
    ]
    if info.get("music_title"):
        lines.append("音乐：%s" % info["music_title"])
    lines += ["", "文案", "-" * 32, info["desc"]]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def save_cover(info, folder):
    if not info.get("cover_url"):
        return None
    try:
        data = fetch_bytes(info["cover_url"], limit=5 * 1024 * 1024)
    except Exception:
        return None
    path = unique_path(folder, info["base"] + "[封面]", "jpg")
    with open(path, "wb") as f:
        f.write(data)
    return path


def save_extras(info, folder, with_meta=True):
    """保存文案 + 封面，返回真正写出来的文件列表。

    命令行和网页版都调这一个，免得两边存的東西不一样（issue #5）。
    任何一项失败都不影响另一项：视频已经下好了，附属文件不该把整个任务判死。
    """
    out = []
    if not with_meta:
        return out
    if info.get("base") and isinstance(info.get("stats"), dict):
        try:
            out.append(save_meta(info, folder))
        except Exception:
            pass
    try:
        cover = save_cover(info, folder)
        if cover:
            out.append(cover)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# 提取音频
# --------------------------------------------------------------------------- #
def find_ffmpeg(extra=None):
    if extra and os.path.isfile(extra):
        return extra
    env = os.environ.get("FFMPEG_PATH")
    if env and os.path.isfile(env):
        return env
    p = shutil.which("ffmpeg")
    if p:
        return p
    for path in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe"),
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ):
        if path and os.path.isfile(path):
            return path
    return None


def extract_mp3(video_path, out_path, ffmpeg, bitrate="192k"):
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", video_path, "-vn", "-c:a", "libmp3lame", "-b:a", bitrate, out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise BrowserError("ffmpeg 转换失败：%s" % proc.stderr.decode("utf-8", "ignore")[-160:])
    return out_path


def save_audio(info, video_path, folder, ffmpeg=None):
    """保存音频，返回 [(路径, 说明), ...]，拿不到就返回空列表。

    优先用 ffmpeg 从视频里抽音轨；没有 ffmpeg 就退回下载抖音的原始音频。
    命令行和网页版共用这一份逻辑（issue #5）。
    """
    if not info.get("base"):
        return []
    if ffmpeg:
        target = unique_path(folder, info["base"] + "[音频]", "mp3")
        try:
            extract_mp3(video_path, target, ffmpeg)
            return [(target, "从视频音轨转的")]
        except Exception:
            pass

    music = info.get("music")
    if music:
        ext = "mp3" if ".mp3" in music.lower() else "m4a"
        target = unique_path(folder, info["base"] + "[原声]", ext)
        try:
            download_stream(music, target, timeout=45)
            return [(target, "原声替代，装个 ffmpeg 就能从视频里抽音轨")]
        except Exception:
            pass
    return []
