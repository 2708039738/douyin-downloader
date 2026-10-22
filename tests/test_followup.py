# -*- coding: utf-8 -*-
"""后续修复的回归测试：删任务要连附属文件一起删，重试要清掉上一次的状态。"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import webui  # noqa: E402
from tests.helpers import LocalServer  # noqa: E402


class ExtrasCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

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

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            finally:
                e.close()

    def write(self, name, content=b"x"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def done_job(self, with_extras=True):
        video = self.write("视频.mp4", b"video")
        cover = self.write("封面.jpg", b"cover")
        meta = self.write("文案.txt", b"meta")
        job = webui._new_job("https://www.douyin.com/video/7123456789012345678",
                             False, "auto")
        webui._update(
            job["id"], status="done", path=video, filename="视频.mp4",
            size=5, received=5, finished=123, saved_to="downloads",
            extras=["封面.jpg", "文案.txt"] if with_extras else [],
            _extras=[cover, meta] if with_extras else [],
        )
        return job, video, cover, meta

    def test_remove_deletes_main_file_and_extras(self):
        job, video, cover, meta = self.done_job()
        status, data = self.post("/api/remove", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(os.path.exists(video), "视频应该被删掉")
        self.assertFalse(os.path.exists(cover), "封面也应该被删掉")
        self.assertFalse(os.path.exists(meta), "文案也应该被删掉")

    def test_remove_does_not_touch_unrelated_files(self):
        job, _, _, _ = self.done_job()
        other = self.write("别的视频.mp4", b"keep me")
        self.post("/api/remove", {"id": job["id"]})
        self.assertTrue(os.path.exists(other), "不能误删同目录里别的文件")

    def test_remove_missing_extra_is_harmless(self):
        job, video, cover, meta = self.done_job()
        os.remove(cover)  # 模拟用户已经手动删过封面
        status, data = self.post("/api/remove", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(os.path.exists(video))
        self.assertFalse(os.path.exists(meta))

    def test_retry_clears_previous_result(self):
        job, video, cover, meta = self.done_job()
        status, data = self.post("/api/retry", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        with webui._jobs_lock:
            current = dict(webui._jobs[job["id"]])
        self.assertEqual(current["status"], "queued")
        self.assertIsNone(current["path"])
        self.assertEqual(current["filename"], "")
        self.assertEqual((current["size"], current["received"]), (0, 0))
        self.assertEqual(current["extras"], [])
        self.assertEqual(current["_extras"], [])
        self.assertEqual(current["saved_to"], "")
        self.assertEqual(current["finished"], 0)
        # 重试只是重新排队，不应该动硬盘上的旧文件（用户可能还想留一份）
        self.assertTrue(os.path.exists(video))

    def test_internal_extras_hidden_from_api(self):
        self.done_job()
        with urllib.request.urlopen(self.base + "/api/jobs", timeout=5) as r:
            job = json.loads(r.read().decode("utf-8"))["jobs"][0]
        self.assertNotIn("_extras", job)
        self.assertIn("extras", job)

    def test_remove_after_real_download_leaves_nothing(self):
        """端到端：真下完一个视频（视频+封面+文案+原声）再点「删」，一个不剩。

        上面的用例是手工拼 job，这里走完整的 _download_job，确认
        extras 里那三条（txt / jpg / m4a）都能被删掉——尤其是原声。
        """
        files = {"/v.mp4": b"video-bytes", "/cover.jpg": b"jpg-bytes",
                 "/m.m4a": b"music-bytes"}
        info = {
            "kind": "video", "filename": "作者-文案[无水印]", "base": "作者-文案",
            "url": None, "cover_url": None, "music": None, "music_title": "",
            "title": "标题", "desc": "文案", "author": "作者", "duration": 1000,
            "aweme_id": "7123456789012345678",
            "page_url": "https://www.douyin.com/video/7123456789012345678",
            "create_time": "2026-10-22 10:00", "quality_label": "720P",
            "stats": {"digg": 1, "comment": 2, "collect": 3, "share": 4},
        }
        with mock.patch.object(webui, "DOWNLOAD_DIR", self.tmp.name), \
                mock.patch.object(core, "find_ffmpeg", return_value=None), \
                LocalServer(files) as base, redirect_stdout(io.StringIO()):
            info["url"] = base + "/v.mp4"
            info["cover_url"] = base + "/cover.jpg"
            info["music"] = base + "/m.m4a"
            job = webui._new_job(info["url"], False, "auto")
            webui._download_job(job["id"], info,
                                {"save_meta": True, "save_audio": True})

        before = sorted(os.listdir(self.tmp.name))
        self.assertEqual(len(before), 4, before)          # mp4 + txt + jpg + m4a
        self.assertIn("作者-文案[原声].m4a", before)
        self.write("别人的.mp4")

        status, data = self.post("/api/remove", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(os.listdir(self.tmp.name), ["别人的.mp4"],
                         "这个视频的文件没删干净")


if __name__ == "__main__":
    unittest.main()
