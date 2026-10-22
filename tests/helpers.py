# -*- coding: utf-8 -*-
"""测试用的小工具：一个只在本机跑的静态文件服务器。

不联网、不依赖第三方库，Windows / macOS / Linux 都能跑。
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def static_handler(files, truncate_at=None, send_length=True):
    """生成一个只回固定内容的 handler。

    files: {"/1.jpg": b"...", ...}
    truncate_at: 只发这么多字节就断开，用来模拟“下载到一半断线”。
    send_length: 关掉就不发 Content-Length，模拟分块传输。
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if send_length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body if truncate_at is None else body[:truncate_at])
            if truncate_at is not None:
                self.close_connection = True

    return Handler


class LocalServer:
    """with LocalServer(files) as base_url: ..."""

    def __init__(self, files, truncate_at=None, send_length=True):
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), static_handler(files, truncate_at, send_length))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self.base_url

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False
