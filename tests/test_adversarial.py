# -*- coding: utf-8 -*-
"""对抗测试：站在攻击者 / 恶劣网络环境的角度，试着把修好的地方重新打穿。

四组：

1. 来源校验 —— Host / Origin 的花式绕过写法（issue #2）
2. 下载完整性 —— 少发、多发、空响应、错误页、断线（issue #4）
3. 清理进程 —— PID 被复用时可别误杀别人的程序（issue #3）
4. 网页版落盘 —— 封面 / 文案 / MP3 / 配置 / 主页批量（issue #5）

全部不联网、不装第三方库，Windows / macOS / Linux 都能跑。
"""

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
import webui  # noqa: E402
from tests.helpers import LocalServer  # noqa: E402

MISSING = object()


def start_webui_server():
    httpd = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def reset_state():
    with webui._jobs_lock:
        webui._jobs.clear()
        webui._job_order.clear()
    with webui._queue_lock:
        webui._queue.clear()
    webui._thumbs.clear()
    webui._seq = 0
    with webui._cfg_lock:
        webui._cfg_cache = (None, None)


class RawClient:
    """能自定义任意请求头（包括重复头、缺失 Host）的裸 HTTP 客户端。"""

    def __init__(self, port):
        self.port = port

    def request(self, method, path, host=MISSING, headers=None, body=b"", http10=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        if http10:
            conn._http_vsn = 10
            conn._http_vsn_str = "HTTP/1.0"
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not MISSING:
            conn.putheader("Host", host)
        for name, value in (headers or []):
            conn.putheader(name, value)
        if body:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data


# --------------------------------------------------------------------------- #
# 1. 来源校验
# --------------------------------------------------------------------------- #
class OriginGuardTests(unittest.TestCase):
    """issue #2：本地服务谁都能调。这里把所有想得到的绕过写法都试一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = start_webui_server()
        cls.port = cls.httpd.server_address[1]
        cls.client = RawClient(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        reset_state()

    def test_forged_host_headers_are_rejected(self):
        """伪造 Host：前缀伪装、userinfo、末尾点、多段端口……都不能过。"""
        for host in ("attacker.example", "127.0.0.1.evil.com", "localhost.evil.com",
                     "127.0.0.1@evil.com", "evil.com:%d" % self.port,
                     "127.0.0.1:%d:extra" % self.port, "127.0.0.1.",
                     "0.0.0.0", "192.168.1.10:%d" % self.port, ""):
            with self.subTest(host=host):
                status, _ = self.client.request("GET", "/api/jobs", host=host)
                self.assertEqual(status, 403, "Host=%r 居然放行了" % host)

    def test_missing_host_header_is_rejected(self):
        """HTTP/1.0 那种不带 Host 的请求也不行。"""
        status, _ = self.client.request("GET", "/api/jobs", host=MISSING, http10=True)
        self.assertEqual(status, 403)

    def test_local_hosts_still_work(self):
        """挡归挡，自己人的写法不能被误伤。"""
        for host in ("127.0.0.1", "127.0.0.1:%d" % self.port, "localhost",
                     "localhost:%d" % self.port, "LOCALHOST:%d" % self.port,
                     "[::1]:%d" % self.port):
            with self.subTest(host=host):
                status, body = self.client.request("GET", "/api/jobs", host=host)
                self.assertEqual(status, 200, "Host=%r 被误伤了" % host)
                self.assertIn("jobs", json.loads(body.decode("utf-8")))

    def test_foreign_origins_are_rejected(self):
        """伪造 Origin：域名伪装、null、file://、https 本地地址都不能过。"""
        for origin in ("https://attacker.example", "http://127.0.0.1.evil.com",
                       "http://localhost.evil.com", "http://127.0.0.1@evil.com",
                       "https://127.0.0.1:%d" % self.port, "null", "file://"):
            with self.subTest(origin=origin):
                status, _ = self.client.request(
                    "GET", "/api/jobs", host="127.0.0.1:%d" % self.port,
                    headers=[("Origin", origin)])
                self.assertEqual(status, 403, "Origin=%r 居然放行了" % origin)

    def test_duplicate_origin_headers_are_rejected(self):
        """一个请求塞两个 Origin（一真一假）也不能混过去。"""
        status, _ = self.client.request(
            "GET", "/api/jobs", host="127.0.0.1:%d" % self.port,
            headers=[("Origin", "http://127.0.0.1:%d" % self.port),
                     ("Origin", "https://attacker.example")])
        self.assertEqual(status, 403)

    def test_local_origins_are_allowed(self):
        for origin in ("http://127.0.0.1:%d" % self.port,
                       "http://localhost:%d" % self.port,
                       "http://[::1]:%d" % self.port):
            with self.subTest(origin=origin):
                status, _ = self.client.request(
                    "GET", "/api/jobs", host="127.0.0.1:%d" % self.port,
                    headers=[("Origin", origin)])
                self.assertEqual(status, 200)

    def test_issue2_repro_cannot_create_job(self):
        """issue #2 里那条 curl 原样重放：不能塞进任务，也不能删掉文件。"""
        payload = json.dumps({"text": "https://www.douyin.com/video/7123456789012345678"},
                             ensure_ascii=False).encode("utf-8")
        status, body = self.client.request(
            "POST", "/api/parse", host="127.0.0.1:%d" % self.port,
            headers=[("Origin", "https://example.com"),
                     ("Content-Type", "text/plain")], body=payload)
        self.assertEqual(status, 403)
        self.assertIn("error", json.loads(body.decode("utf-8")))
        with webui._jobs_lock:
            self.assertEqual(webui._jobs, {})

    def test_foreign_remove_cannot_delete_file(self):
        """最要命的一条：别的网站不能通过 /api/remove 删你的下载。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "视频.mp4")
            with open(path, "wb") as f:
                f.write(b"payload")
            job = webui._new_job("https://www.douyin.com/video/7123456789012345678", False, "auto")
            webui._update(job["id"], status="done", path=path, filename="视频.mp4")
            status, _ = self.client.request(
                "POST", "/api/remove", host="127.0.0.1:%d" % self.port,
                headers=[("Origin", "https://attacker.example"),
                         ("Content-Type", "text/plain")],
                body=json.dumps({"id": job["id"]}).encode("utf-8"))
            self.assertEqual(status, 403)
            self.assertTrue(os.path.exists(path), "文件被跨站请求删掉了")
            with webui._jobs_lock:
                self.assertIn(job["id"], webui._jobs)

    def test_foreign_cannot_read_downloaded_file(self):
        """别的网站也不能借 /api/file 把你的视频读走。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "视频.mp4")
            with open(path, "wb") as f:
                f.write(b"payload")
            job = webui._new_job("https://www.douyin.com/video/7123456789012345678", False, "auto")
            webui._update(job["id"], status="done", path=path, filename="视频.mp4")
            status, body = self.client.request(
                "GET", "/api/file?id=" + job["id"], host="attacker.example")
            self.assertEqual(status, 403)
            self.assertNotIn(b"payload", body)


# --------------------------------------------------------------------------- #
# 2. 下载完整性
# --------------------------------------------------------------------------- #
class DownloadIntegrityTests(unittest.TestCase):
    """issue #4：说好 1000 字节只给 100，不能当成成功。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "v.mp4")

    def test_truncated_download_raises_and_cleans_up(self):
        with LocalServer({"/v.mp4": b"x" * 1000}, truncate_at=100) as base:
            with self.assertRaises(core.BrowserError):
                core.download_stream(base + "/v.mp4", self.path)
        self.assertFalse(os.path.exists(self.path), "半截文件没删掉")

    def test_server_sends_less_than_declared(self):
        """服务器发完 100 字节就正常关闭，但声称有 1000 —— 同样算失败。"""
        with LocalServer({"/v.mp4": b"x" * 100}, declared_length=1000) as base:
            with self.assertRaises(core.BrowserError):
                core.download_stream(base + "/v.mp4", self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_server_sends_more_than_declared(self):
        """反过来：声明 10 实发 100，不能卡死，也不能把多余字节写进文件。"""
        with LocalServer({"/v.mp4": b"x" * 100}, declared_length=10) as base:
            got = core.download_stream(base + "/v.mp4", self.path)
        self.assertEqual(got, 10)
        self.assertEqual(os.path.getsize(self.path), 10)

    def test_empty_200_is_treated_as_failure(self):
        """回 200 但一个字节都不给，也算失败。"""
        with LocalServer({"/v.mp4": b""}) as base:
            with self.assertRaises(core.BrowserError):
                core.download_stream(base + "/v.mp4", self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_404_leaves_no_file(self):
        with LocalServer({"/v.mp4": b"x" * 100}) as base:
            with self.assertRaises(urllib.error.HTTPError):
                core.download_stream(base + "/missing.mp4", self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_failed_download_does_not_touch_other_files(self):
        keep = os.path.join(self.tmp.name, "别人的文件.mp4")
        with open(keep, "wb") as f:
            f.write(b"keep me")
        with LocalServer({"/v.mp4": b"x" * 1000}, truncate_at=10) as base:
            with self.assertRaises(core.BrowserError):
                core.download_stream(base + "/v.mp4", self.path)
        self.assertTrue(os.path.exists(keep))
        with open(keep, "rb") as f:
            self.assertEqual(f.read(), b"keep me")

    def test_gallery_with_all_images_broken_still_fails(self):
        """图集：一张都拿不到时必须报错，不能吐个空 zip。"""
        with LocalServer({"/1.jpg": b"a" * 100}, truncate_at=10) as base:
            with self.assertRaises(core.BrowserError):
                core.download_gallery([base + "/1.jpg"], self.tmp.name, "图集")


# --------------------------------------------------------------------------- #
# 3. 清理进程
# --------------------------------------------------------------------------- #
class ProcessGuardTests(unittest.TestCase):
    """issue #3：非 Windows 清理残留进程前，先确认这真的是浏览器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pid_file = os.path.join(self.tmp.name, "dydl_browser.pid")

    def _write_pid(self, pid="4242", age=0):
        with open(self.pid_file, "w") as f:
            f.write(str(pid))
        if age:
            old = time.time() - age
            os.utime(self.pid_file, (old, old))

    def _run(self, name):
        with mock.patch.object(core, "_pid_file", return_value=self.pid_file), \
                mock.patch.object(core, "_process_name", return_value=name), \
                mock.patch.object(core, "_kill_pid") as killer:
            core.kill_stale_browser()
        return killer

    def test_kills_when_pid_really_is_a_browser(self):
        self._write_pid()
        killer = self._run("google chrome")
        killer.assert_called_once_with(4242)

    def test_does_not_kill_when_pid_was_reused(self):
        """PID 被复用给别的程序了，绝不能杀。"""
        self._write_pid()
        for name in ("python3.13", "notepad.exe", "explorer.exe", "code"):
            with self.subTest(name=name):
                self._write_pid()
                killer = self._run(name)
                killer.assert_not_called()

    def test_does_not_kill_when_name_unknown(self):
        """ps 拿不到名字（比如命令不存在），保守起见不杀。"""
        self._write_pid()
        killer = self._run("")
        killer.assert_not_called()

    def test_expired_pid_file_is_ignored(self):
        self._write_pid(age=3600)
        killer = self._run("chrome")
        killer.assert_not_called()
        self.assertFalse(os.path.exists(self.pid_file))

    def test_garbage_pid_file_does_not_crash(self):
        self._write_pid(pid="not-a-pid")
        killer = self._run("chrome")
        killer.assert_not_called()

    def test_parse_tasklist_matches_exact_pid(self):
        text = ("msedge.exe                   4242 Console                    1    100,000 K\r\n"
                "chrome.exe                   42420 Console                    1     50,000 K\r\n")
        self.assertEqual(core._parse_tasklist(text, 4242), "msedge.exe")
        self.assertEqual(core._parse_tasklist(text, 42420), "chrome.exe")

    def test_parse_tasklist_ignores_memory_field(self):
        """内存占用里出现同样的数字，不能被当成 PID 匹配上。"""
        text = "python.exe                   1111 Console                    1      4,242 K\r\n"
        self.assertEqual(core._parse_tasklist(text, 4242), "")

    def test_parse_tasklist_no_match_message(self):
        self.assertEqual(core._parse_tasklist("信息: 没有运行的任务匹配指定标准。", 4242), "")

    def test_browser_name_detection(self):
        for name in ("msedge.exe", "chrome",
                     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "chromium-browser", "Microsoft Edge"):
            self.assertTrue(core._looks_like_browser(name), name)
        for name in ("", "python", "cmd.exe", "code", "notepad.exe"):
            self.assertFalse(core._looks_like_browser(name), name)


# --------------------------------------------------------------------------- #
# 4. 网页版落盘行为（issue #5）
# --------------------------------------------------------------------------- #
def make_info(base_url, *, music=None, cover=True):
    """构造一个 describe() 形状的 info，只有需要的字段。"""
    return {
        "kind": "video",
        "filename": "作者-文案[无水印]",
        "base": "作者-文案",
        "url": base_url + "/v.mp4",
        "cover_url": base_url + "/cover.jpg" if cover else None,
        "music": music,
        "music_title": "",
        "title": "标题",
        "desc": "文案内容 #话题",
        "author": "作者",
        "duration": 30000,
        "aweme_id": "7123456789012345678",
        "page_url": "https://www.douyin.com/video/7123456789012345678",
        "create_time": "2026-10-22 10:00",
        "quality_label": "720P",
        "stats": {"digg": 1, "comment": 2, "collect": 3, "share": 4},
    }


class WebUIExtrasTests(unittest.TestCase):
    """issue #5：网页版也得存封面、文案、MP3，并且认 config.json。"""

    def setUp(self):
        reset_state()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run_job(self, info, cfg):
        job = webui._new_job(info["url"], False, "auto")
        with mock.patch.object(webui, "DOWNLOAD_DIR", self.tmp.name), \
                redirect_stdout(io.StringIO()):
            webui._download_job(job["id"], info, cfg)
        with webui._jobs_lock:
            return job["id"], dict(webui._jobs[job["id"]])

    def test_video_job_saves_cover_and_meta_like_cli(self):
        files = {"/v.mp4": b"mp4-bytes", "/cover.jpg": b"jpg-bytes"}
        with LocalServer(files) as base:
            job_id, job = self._run_job(make_info(base), {"save_meta": True})
        names = sorted(os.listdir(self.tmp.name))
        self.assertIn("作者-文案[无水印].mp4", names)
        self.assertIn("作者-文案[信息].txt", names)
        self.assertIn("作者-文案[封面].jpg", names)
        # 不只是「文件在」，内容也要和命令行那份一致
        with open(os.path.join(self.tmp.name, "作者-文案[信息].txt"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("作者：@作者", text)
        self.assertIn("点赞 1", text)
        self.assertIn("文案内容", text)
        with open(os.path.join(self.tmp.name, "作者-文案[封面].jpg"), "rb") as f:
            self.assertEqual(f.read(), b"jpg-bytes")
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(job["extras"]), 2)
        self.assertEqual(job["saved_to"], os.path.basename(self.tmp.name))

    def test_save_meta_false_skips_extras(self):
        with LocalServer({"/v.mp4": b"mp4-bytes", "/cover.jpg": b"jpg-bytes"}) as base:
            _, job = self._run_job(make_info(base), {"save_meta": False})
        self.assertEqual(os.listdir(self.tmp.name), ["作者-文案[无水印].mp4"])
        self.assertEqual(job["extras"], [])

    def test_broken_cover_does_not_fail_the_job(self):
        """封面 404 不能把已经下好的视频判死。"""
        with LocalServer({"/v.mp4": b"mp4-bytes"}) as base:
            _, job = self._run_job(make_info(base, cover=True), {"save_meta": True})
        self.assertEqual(job["status"], "done")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "作者-文案[无水印].mp4")))

    def test_audio_falls_back_to_music_url(self):
        files = {"/v.mp4": b"mp4-bytes", "/m.m4a": b"music-bytes"}
        with LocalServer(files) as base, \
                mock.patch.object(core, "find_ffmpeg", return_value=None):
            _, job = self._run_job(make_info(base, music=base + "/m.m4a"),
                                   {"save_meta": False, "save_audio": True})
        self.assertIn("作者-文案[原声].m4a", os.listdir(self.tmp.name))
        self.assertEqual(job["status"], "done")

    def test_config_out_dir_is_honoured(self):
        with tempfile.TemporaryDirectory() as out:
            cfg_path = os.path.join(self.tmp.name, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"out": out, "quality": "720"}, f)
            with mock.patch.object(webui, "CONFIG_PATH", cfg_path):
                reset_state()
                self.assertEqual(webui._download_dir(), out)
                self.assertEqual(webui._config()["quality"], "720")
        reset_state()

    def test_broken_config_falls_back_to_defaults(self):
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("{这不是 JSON")
        with mock.patch.object(webui, "CONFIG_PATH", cfg_path):
            reset_state()
            self.assertEqual(webui._config()["quality"], "auto")
        reset_state()

    def test_parse_uses_config_quality_when_client_omits_it(self):
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"quality": "540", "prefer_h264": False}, f)
        httpd = start_webui_server()
        try:
            with mock.patch.object(webui, "CONFIG_PATH", cfg_path):
                reset_state()
                client = RawClient(httpd.server_address[1])
                payload = json.dumps(
                    {"text": "https://www.douyin.com/video/7123456789012345678"}).encode()
                status, body = client.request(
                    "POST", "/api/parse", host="127.0.0.1:%d" % httpd.server_address[1],
                    headers=[("Content-Type", "application/json")], body=payload)
                self.assertEqual(status, 200)
                job_id = json.loads(body.decode("utf-8"))["created"][0]
                with webui._jobs_lock:
                    self.assertEqual(webui._jobs[job_id]["quality"], "540")
                    self.assertFalse(webui._jobs[job_id]["prefer_h264"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            reset_state()

    def test_api_config_exposes_defaults_to_frontend(self):
        """前端要能拿到 config.json 的默认值，否则下拉框永远是最初那个 auto。"""
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"quality": "720", "prefer_h264": False, "profile_limit": 7}, f)
        httpd = start_webui_server()
        try:
            with mock.patch.object(webui, "CONFIG_PATH", cfg_path):
                reset_state()
                client = RawClient(httpd.server_address[1])
                status, body = client.request(
                    "GET", "/api/config", host="127.0.0.1:%d" % httpd.server_address[1])
                self.assertEqual(status, 200)
                data = json.loads(body.decode("utf-8"))
                self.assertEqual(data["quality"], "720")
                self.assertFalse(data["prefer_h264"])
                self.assertEqual(data["profile_limit"], 7)
                self.assertNotIn("out", data)   # 别把本机路径漏给页面
        finally:
            httpd.shutdown()
            httpd.server_close()
            reset_state()

    def test_profile_url_expands_into_jobs(self):
        """issue #5 备注：主页链接要先取列表再逐个入队，不能直接当作品解析。"""
        job = webui._new_job("https://www.douyin.com/user/MS4wLjABAAAA", False, "auto")
        links = ["https://www.douyin.com/video/7123456789012345678",
                 "https://www.douyin.com/video/7123456789012345679"]
        with mock.patch.object(webui.BROWSER, "profile_links", return_value=links) as fn:
            webui._expand_profile(job["id"], "https://www.douyin.com/user/x", job,
                                  {"profile_limit": 5})
        fn.assert_called_once()
        with webui._jobs_lock:
            parent = webui._jobs[job["id"]]
            self.assertEqual(parent["status"], "done")
            self.assertEqual(parent["batch_count"], 2)
            children = [j for j in webui._jobs.values() if j["id"] != job["id"]]
        self.assertEqual(len(children), 2)
        with webui._queue_lock:
            self.assertEqual(len(webui._queue), 2)

    def test_profile_without_links_reports_error(self):
        job = webui._new_job("https://www.douyin.com/user/MS4wLjABAAAA", False, "auto")
        with mock.patch.object(webui.BROWSER, "profile_links", return_value=[]):
            webui._expand_profile(job["id"], "https://www.douyin.com/user/x", job, {})
        with webui._jobs_lock:
            self.assertEqual(webui._jobs[job["id"]]["status"], "error")
            self.assertIn("主页", webui._jobs[job["id"]]["error"])

    def test_profile_failure_is_reported_not_raised(self):
        job = webui._new_job("https://www.douyin.com/user/MS4wLjABAAAA", False, "auto")
        with mock.patch.object(webui.BROWSER, "profile_links", side_effect=RuntimeError("超时")):
            webui._expand_profile(job["id"], "https://www.douyin.com/user/x", job, {})
        with webui._jobs_lock:
            self.assertEqual(webui._jobs[job["id"]]["status"], "error")
            self.assertIn("超时", webui._jobs[job["id"]]["error"])


# --------------------------------------------------------------------------- #
# 5. 请求处理本身的抗畸形能力
# --------------------------------------------------------------------------- #
class HandlerHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = start_webui_server()
        cls.port = cls.httpd.server_address[1]
        cls.host = "127.0.0.1:%d" % cls.port
        cls.client = RawClient(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        reset_state()

    def test_garbage_content_length_is_400(self):
        status, _ = self.client.request(
            "POST", "/api/parse", host=self.host,
            headers=[("Content-Length", "not-a-number")])
        self.assertEqual(status, 400)

    def test_negative_content_length_is_400(self):
        status, _ = self.client.request(
            "POST", "/api/parse", host=self.host,
            headers=[("Content-Length", "-5")])
        self.assertEqual(status, 400)

    def test_oversized_content_length_is_refused_without_hanging(self):
        """声称 5 MB 却一个字节都不发：不能把服务拖住。"""
        started = time.time()
        status, _ = self.client.request(
            "POST", "/api/parse", host=self.host,
            headers=[("Content-Length", str(5 * 1024 * 1024))])
        self.assertEqual(status, 400)
        self.assertLess(time.time() - started, 3)

    def test_path_traversal_in_job_id_is_404(self):
        for path in ("/api/file?id=../../config.json",
                     "/api/file?id=..%2F..%2Fconfig.json",
                     "/api/thumb?id=../../config.json"):
            with self.subTest(path=path):
                status, body = self.client.request("GET", path, host=self.host)
                self.assertEqual(status, 404)
                self.assertNotIn(b"quality", body)

    def test_remove_unknown_job_is_harmless(self):
        status, body = self.client.request(
            "POST", "/api/remove", host=self.host,
            headers=[("Content-Type", "application/json")],
            body=json.dumps({"id": "99999"}).encode())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body.decode("utf-8"))["ok"])

    def test_concurrent_parse_gets_unique_job_ids(self):
        """issue 之外的隐患：id 自增不在锁里，并发时可能撞号。"""
        total = 12
        results = []
        lock = threading.Lock()

        def fire():
            payload = json.dumps(
                {"text": "https://www.douyin.com/video/7123456789012345678"}).encode()
            try:
                code, body = self.client.request(
                    "POST", "/api/parse", host=self.host,
                    headers=[("Content-Type", "application/json")], body=payload)
                with lock:
                    results.append((code, json.loads(body.decode("utf-8"))["created"][0]))
            except Exception as e:  # noqa: BLE001
                with lock:
                    results.append((-1, str(e)))

        threads = [threading.Thread(target=fire) for _ in range(total)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), total)
        self.assertTrue(all(code == 200 for code, _ in results), results)
        ids = [jid for _, jid in results]
        self.assertEqual(len(set(ids)), total, "有任务 id 撞号：%s" % ids)
        with webui._jobs_lock:
            self.assertEqual(len(webui._jobs), total)

    def test_server_survives_garbage_and_keeps_serving(self):
        self.client.request("POST", "/api/parse", host=self.host,
                            headers=[("Content-Length", "garbage")])
        self.client.request("GET", "/api/jobs", host="attacker.example")
        status, body = self.client.request("GET", "/api/jobs", host=self.host)
        self.assertEqual(status, 200)
        self.assertIn("jobs", json.loads(body.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
