# -*- coding: utf-8 -*-
"""针对 2026-10-23 体检发现的问题补的回归测试。

这些用例在修复前**都是失败的**（并发撞路径、图集静默丢图、畸形 text 打断连接
都实测过），所以它们不是「测试陪着代码写」，而是先有故障、再有的断言。
"""

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

import core  # noqa: E402
import webui  # noqa: E402
from tests.helpers import LocalServer  # noqa: E402


class UniquePathTests(unittest.TestCase):
    """D1：并发任务必须拿到不同路径，否则会互相写坏 / 互删。"""

    def test_concurrent_callers_never_share_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = []
            lock = threading.Lock()

            def worker():
                p = core.unique_path(tmp, "同名", "mp4")
                with lock:
                    got.append(p)

            threads = [threading.Thread(target=worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(got), 20)
            self.assertEqual(len(set(got)), 20, "并发时有线程拿到了同一个路径")
            for p in got:
                self.assertTrue(os.path.exists(p), "占位文件应该被真的创建出来")

    def test_second_call_gets_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = core.unique_path(tmp, "a", "mp4")
            second = core.unique_path(tmp, "a", "mp4")
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith("a(1).mp4"))


class PartialFileTests(unittest.TestCase):
    """R6：写 .part 再原子改名，别留一个「看着像完整视频」的半截文件。"""

    def test_success_leaves_no_part_file(self):
        payload = b"video-bytes" * 100
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/v.mp4": payload}) as base:
            path = os.path.join(tmp, "v.mp4")
            self.assertEqual(core.download_stream(base + "/v.mp4", path), len(payload))
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".part"), "成功之后不该留 .part")

    def test_failure_leaves_neither_part_nor_target(self):
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/v.mp4": b"x" * 1000}, truncate_at=100) as base:
            path = os.path.join(tmp, "v.mp4")
            with self.assertRaises(core.BrowserError):
                core.download_stream(base + "/v.mp4", path)
            self.assertFalse(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".part"))

    def test_failure_does_not_delete_a_real_file(self):
        """占位清理只能删 0 字节的空壳，有内容的一律不碰。"""
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/v.mp4": b"x" * 1000}, truncate_at=10) as base:
            keep = os.path.join(tmp, "keep.mp4")
            with open(keep, "wb") as f:
                f.write(b"real content")
            with self.assertRaises(core.BrowserError):
                core.download_stream(base + "/v.mp4", keep)
            with open(keep, "rb") as f:
                self.assertEqual(f.read(), b"real content")


class RetryTests(unittest.TestCase):
    """R5：一次抖动不该毁掉整个任务。"""

    def test_retries_then_succeeds(self):
        calls = []

        def fake(url, path, on_progress=None, timeout=60):
            calls.append(url)
            if len(calls) < 3:
                raise core.BrowserError("下载不完整")
            return 123

        with mock.patch.object(core, "download_stream", side_effect=fake), \
                mock.patch.object(core.time, "sleep", return_value=None):
            got = core.download_with_retry("http://x/v.mp4", "/tmp/x.mp4", attempts=3)
        self.assertEqual(got, 123)
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_attempts(self):
        with mock.patch.object(core, "download_stream",
                               side_effect=core.BrowserError("一直失败")), \
                mock.patch.object(core.time, "sleep", return_value=None):
            with self.assertRaises(core.BrowserError):
                core.download_with_retry("http://x/v.mp4", "/tmp/x.mp4", attempts=2)


class GalleryFailureTests(unittest.TestCase):
    """D3：坏图可以跳过，但必须报出来，不能让残缺 zip 冒充完整。"""

    def test_reports_each_failed_image(self):
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/1.jpg": b"one", "/2.jpg": b"two"}) as base:
            failures = []
            path = core.download_gallery(
                [base + "/1.jpg", base + "/missing.jpg", base + "/2.jpg"],
                tmp, "图集",
                on_failure=lambda idx, url, err: failures.append(idx))
            self.assertTrue(path.endswith(".zip"))
            self.assertEqual(failures, [2], "第 2 张失败必须被回调报出来")
            self.assertFalse(os.path.exists(path + ".part"))

    def test_absent_url_is_reported_too(self):
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/1.jpg": b"one"}) as base:
            failures = []
            core.download_gallery(
                [base + "/1.jpg", None], tmp, "图集",
                on_failure=lambda idx, url, err: failures.append(idx))
            self.assertEqual(failures, [2])

    def test_all_broken_leaves_no_zip_behind(self):
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({}) as base:
            with self.assertRaises(core.BrowserError):
                core.download_gallery([base + "/missing.jpg"], tmp, "图集")
            self.assertEqual([n for n in os.listdir(tmp) if n.endswith(".zip")], [])


class _ServerCase(unittest.TestCase):
    """起一个本地服务打接口，跟 test_webui.py 里的做法一致。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        with webui._jobs_lock:
            webui._jobs.clear()
            webui._job_order.clear()
        with webui._queue_lock:
            webui._queue.clear()      # 别让上一条用例排的任务串到这条来
        webui._thumbs.clear()
        webui._seq = 0

    def post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8"))
            finally:
                e.close()


class ParseValidationTests(_ServerCase):
    """D2：畸形 text 要回 400 JSON，不能把连接打断。"""

    def test_non_string_text_returns_400_not_disconnect(self):
        for bad in (123, ["https://v.douyin.com/x/"], True, {"a": 1}):
            with self.subTest(text=bad):
                status, body = self.post("/api/parse", {"text": bad})
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    def test_empty_body_still_reports_no_link(self):
        status, body = self.post("/api/parse", {})
        self.assertEqual(status, 400)
        self.assertIn("没识别到抖音链接", body.get("error", ""))

    def test_string_false_is_not_treated_as_true(self):
        status, body = self.post("/api/parse", {
            "text": "https://www.douyin.com/video/7123456789012345678",
            "prefer_h264": "false",
            "quality": "不是清晰度",
        })
        self.assertEqual(status, 200)
        job = webui._jobs[body["created"][0]]
        self.assertIsInstance(job["prefer_h264"], bool)
        self.assertTrue(job["prefer_h264"], "字符串 false 应该回落到配置值")
        self.assertEqual(job["quality"], "auto", "非法 quality 应该回落到配置值")


class RetryEndpointTests(_ServerCase):
    """R13：重试不存在的任务要明确 404，别往队列里塞垃圾。"""

    def test_unknown_id_returns_404(self):
        status, body = self.post("/api/retry", {"id": "999999"})
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertEqual(webui._queue, [], "不该把不存在的 id 塞进队列")


class ThumbCacheTests(unittest.TestCase):
    """R1：缩略图缓存要有上限，否则长跑必然吃满内存。"""

    def setUp(self):
        webui._thumbs.clear()

    def tearDown(self):
        webui._thumbs.clear()

    def test_cache_is_capped_and_drops_oldest(self):
        for i in range(webui.MAX_THUMBS + 5):
            webui._remember_thumb(str(i), b"x")
        self.assertEqual(len(webui._thumbs), webui.MAX_THUMBS)
        self.assertNotIn("0", webui._thumbs, "最老的应该被淘汰")
        self.assertIn(str(webui.MAX_THUMBS + 4), webui._thumbs)


if __name__ == "__main__":
    unittest.main()
