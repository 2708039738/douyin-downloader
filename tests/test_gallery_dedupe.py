# -*- coding: utf-8 -*-
"""最新提交的回归测试：图集「两个都要」不重复下，异常 JSON 不把服务打挂。"""

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402


def reset_state():
    with webui._jobs_lock:
        webui._jobs.clear()
        webui._job_order.clear()
    webui._thumbs.clear()
    webui._seq = 0


class InputGuardTests(unittest.TestCase):
    def test_versions_of_bad_types_fall_back(self):
        self.assertEqual(webui.versions_of({"versions": ["both"]}), [False])
        self.assertEqual(webui.versions_of({"versions": {"v": "both"}}), [False])
        self.assertEqual(webui.versions_of({"versions": 3, "watermark": True}), [True])

    def test_profile_limit_bad_type_falls_back_to_zero(self):
        for value in ("abc", [], {}, None):
            job = webui._new_job("https://www.douyin.com/video/1", False, "auto",
                                 opts={"profile_limit": value})
            self.assertEqual(job["profile_limit"], 0, value)
        neg = webui._new_job("https://www.douyin.com/video/1", False, "auto",
                             opts={"profile_limit": -5})
        self.assertEqual(neg["profile_limit"], 0)

    def test_string_booleans_are_not_truthy(self):
        job = webui._new_job("https://www.douyin.com/video/1", False, "auto",
                             opts={"save_meta": "false", "save_audio": "false"})
        self.assertIsNone(job["save_meta"])
        self.assertIsNone(job["save_audio"])

    def tearDown(self):
        reset_state()


class GalleryDedupeTests(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.url = "https://www.douyin.com/note/7123456789012345678"

    def tearDown(self):
        reset_state()

    def make_pair(self, batch="b1", wm=True, sibling_status="downloading"):
        nowm = webui._new_job(self.url, False, "auto", opts={"batch": batch})
        wm = webui._new_job(self.url, True, "auto", opts={"batch": batch})
        webui._update(nowm["id"], status=sibling_status)
        return nowm, wm

    def test_gallery_wm_skipped_when_same_batch_nowm_exists(self):
        _, wm = self.make_pair()
        self.assertTrue(webui._should_skip_gallery(wm, {"kind": "gallery"}))

    def test_gallery_wm_not_skipped_for_different_batch(self):
        _, wm = self.make_pair(batch="b1")
        webui._update(wm["id"], _batch="b2")
        self.assertFalse(webui._should_skip_gallery(wm, {"kind": "gallery"}))

    def test_gallery_wm_not_skipped_when_sibling_failed(self):
        _, wm = self.make_pair(sibling_status="error")
        self.assertFalse(webui._should_skip_gallery(wm, {"kind": "gallery"}))

    def test_gallery_wm_alone_is_not_skipped(self):
        wm = webui._new_job(self.url, True, "auto", opts={"batch": "only"})
        self.assertFalse(webui._should_skip_gallery(wm, {"kind": "gallery"}))

    def test_video_is_never_skipped(self):
        _, wm = self.make_pair()
        self.assertFalse(webui._should_skip_gallery(wm, {"kind": "video"}))

    def test_legacy_jobs_without_batch_use_time_window(self):
        nowm, wm = self.make_pair(batch="")
        self.assertTrue(webui._should_skip_gallery(wm, {"kind": "gallery"}))
        webui._update(nowm["id"], created=time.time() - 600)
        self.assertFalse(webui._should_skip_gallery(wm, {"kind": "gallery"}))

    def test_job_opts_carries_batch_to_profile_children(self):
        job = webui._new_job(self.url, True, "auto", opts={"batch": "carry"})
        self.assertEqual(webui._job_opts(job)["batch"], "carry")


class BadRequestTests(unittest.TestCase):
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
        reset_state()

    def tearDown(self):
        reset_state()

    def post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/parse", data=data,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            finally:
                e.close()

    def test_json_array_body_is_400_not_crash(self):
        status, data = self.post([])
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_bad_profile_limit_does_not_crash(self):
        status, data = self.post({
            "text": "https://www.douyin.com/video/7123456789012345678",
            "profile_limit": "abc",
        })
        self.assertEqual(status, 200)
        with webui._jobs_lock:
            job = webui._jobs[data["created"][0]]
        self.assertEqual(job["profile_limit"], 0)

    def test_bad_versions_does_not_crash(self):
        status, data = self.post({
            "text": "https://www.douyin.com/video/7123456789012345678",
            "versions": ["both"],
        })
        self.assertEqual(status, 200)
        with webui._jobs_lock:
            job = webui._jobs[data["created"][0]]
        self.assertFalse(job["watermark"])


if __name__ == "__main__":
    unittest.main()
