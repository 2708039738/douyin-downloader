# -*- coding: utf-8 -*-
"""webui.py 的接口测试：真的起一个本地 HTTP 服务，用 HTTP 请求打它。

不需要抖音、不需要浏览器。
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402
from tests.helpers import LocalServer  # noqa: E402


class WebUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        with webui._jobs_lock:
            webui._jobs.clear()
            webui._job_order.clear()
        webui._thumbs.clear()
        webui._seq = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # -- 小工具 ---------------------------------------------------------- #
    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read(), dict(e.headers)
            finally:
                e.close()

    def post(self, path, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            finally:
                e.close()

    def add_done_job(self, filename="视频.mp4", content=b"hello"):
        path = os.path.join(self.tmp.name, filename)
        with open(path, "wb") as f:
            f.write(content)
        job = webui._new_job("https://www.douyin.com/video/7123456789012345678", False, "auto")
        webui._update(job["id"], status="done", path=path, filename=filename,
                      size=len(content), received=len(content))
        return job, path

    # -- 页面 ------------------------------------------------------------ #
    def test_root_serves_html_page(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("抖音", body.decode("utf-8"))

    def test_unknown_path_is_404(self):
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 404)

    # -- 解析接口 -------------------------------------------------------- #
    def test_parse_rejects_text_without_douyin_link(self):
        status, data = self.post("/api/parse", {"text": "没有链接"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_parse_creates_one_job_per_link(self):
        text = ("https://www.douyin.com/video/7123456789012345678 "
                "https://www.douyin.com/video/7123456789012345679")
        status, data = self.post("/api/parse", {"text": text, "watermark": True,
                                                "quality": "720", "prefer_h264": False})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["created"]), 2)
        with webui._jobs_lock:
            job = webui._jobs[data["created"][0]]
        self.assertEqual(job["status"], "queued")
        self.assertTrue(job["watermark"])
        self.assertEqual(job["quality"], "720")
        self.assertFalse(job["prefer_h264"])

    def test_parse_accepts_share_copy(self):
        text = "7.93 复制打开抖音，看看【作品】 https://v.douyin.com/iRNBho6u/ 复制此链接！"
        status, data = self.post("/api/parse", {"text": text})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["created"]), 1)

    def test_jobs_hides_internal_path(self):
        self.add_done_job()
        status, body, _ = self.get("/api/jobs")
        self.assertEqual(status, 200)
        job = json.loads(body.decode("utf-8"))["jobs"][0]
        self.assertNotIn("path", job)
        self.assertTrue(job["percent"])

    # -- 文件 / 封面 ----------------------------------------------------- #
    def test_file_download_sends_attachment(self):
        job, _ = self.add_done_job()
        status, body, headers = self.get("/api/file?id=" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("%E8%A7%86%E9%A2%91", headers["Content-Disposition"])  # 视频.mp4

    def test_file_missing_when_not_ready(self):
        job = webui._new_job("https://www.douyin.com/video/7123456789012345678", False, "auto")
        status, body, _ = self.get("/api/file?id=" + job["id"])
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_thumb_returns_404_when_absent(self):
        status, _, _ = self.get("/api/thumb?id=999")
        self.assertEqual(status, 404)

    # -- 队列操作 -------------------------------------------------------- #
    def test_remove_deletes_file_from_disk(self):
        job, path = self.add_done_job()
        status, data = self.post("/api/remove", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(os.path.exists(path))
        with webui._jobs_lock:
            self.assertNotIn(job["id"], webui._jobs)

    def test_retry_puts_job_back_in_queue(self):
        job, _ = self.add_done_job()
        status, data = self.post("/api/retry", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        with webui._jobs_lock:
            self.assertEqual(webui._jobs[job["id"]]["status"], "queued")

    def test_open_folder_uses_cross_platform_helper(self):
        with mock.patch.object(webui.core, "open_in_file_manager", return_value=True) as fn:
            status, data = self.post("/api/open-folder", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        fn.assert_called_once()

    def test_open_folder_reports_failure(self):
        with mock.patch.object(webui.core, "open_in_file_manager", return_value=False):
            status, data = self.post("/api/open-folder", {})
        self.assertEqual(status, 500)
        self.assertIn("error", data)

    # -- 下载落盘 -------------------------------------------------------- #
    def test_download_job_writes_video(self):
        payload = b"fake-mp4" * 100
        job = webui._new_job("https://www.douyin.com/video/7123456789012345678", False, "auto")
        info = {"kind": "video", "filename": "测试视频", "url": None}
        with tempfile.TemporaryDirectory() as out, \
                LocalServer({"/v.mp4": payload}) as base, \
                mock.patch.object(webui, "DOWNLOAD_DIR", out):
            info["url"] = base + "/v.mp4"
            webui._download_job(job["id"], info)
            with webui._jobs_lock:
                self.assertEqual(webui._jobs[job["id"]]["status"], "done")
            saved = os.path.join(out, "测试视频.mp4")
            self.assertTrue(os.path.exists(saved))
            with open(saved, "rb") as f:
                self.assertEqual(f.read(), payload)

    def test_download_job_packs_gallery_zip(self):
        job = webui._new_job("https://www.douyin.com/video/7123456789012345678", False, "auto")
        with tempfile.TemporaryDirectory() as out, \
                LocalServer({"/1.jpg": b"one", "/2.jpg": b"two"}) as base, \
                mock.patch.object(webui, "DOWNLOAD_DIR", out):
            info = {"kind": "gallery", "filename": "图集",
                    "files": [base + "/1.jpg", base + "/2.jpg"]}
            webui._download_job(job["id"], info)
            files = os.listdir(out)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].endswith(".zip"))


if __name__ == "__main__":
    unittest.main()
