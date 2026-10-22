# -*- coding: utf-8 -*-
"""app.py 的单元测试：配置读写、输入整理、环境检查、仓库约定。"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_load_config_merges_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"quality": "720"}, f)
            with mock.patch.object(app, "CONFIG_PATH", path):
                cfg = app.load_config()
            self.assertEqual(cfg["quality"], "720")
            self.assertTrue(cfg["prefer_h264"])  # 没写的字段用默认值补上

    def test_first_run_creates_config_without_waiting_for_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with mock.patch.object(app, "CONFIG_PATH", path), \
                    mock.patch.object(sys.stdin, "isatty", return_value=False):
                cfg = app.load_config()
            self.assertEqual(cfg["quality"], "auto")
            self.assertEqual(cfg["profile_limit"], 20)
            self.assertTrue(os.path.isfile(path))

    def test_broken_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{这不是 JSON")
            with mock.patch.object(app, "CONFIG_PATH", path), \
                    mock.patch.object(sys.stdin, "isatty", return_value=False):
                cfg = app.load_config()
            self.assertTrue(cfg["prefer_h264"])


class InputTests(unittest.TestCase):
    def test_collect_inputs_reads_txt_and_dedupes(self):
        link = "https://www.douyin.com/video/7123456789012345678"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8",
                                         delete=False) as f:
            f.write(link + "\n" + link + "\n")
            name = f.name
        self.addCleanup(os.remove, name)
        with redirect_stdout(io.StringIO()):
            urls = app.collect_inputs([name])
        self.assertEqual(urls, [link])

    def test_collect_inputs_accepts_share_text_argument(self):
        text = "复制打开抖音 https://v.douyin.com/iRNBho6u/ 看看"
        with redirect_stdout(io.StringIO()):
            urls = app.collect_inputs([text])
        self.assertEqual(urls, ["https://v.douyin.com/iRNBho6u/"])

    def test_collect_inputs_returns_empty_for_junk(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(app.collect_inputs(["没有链接"]), [])


class CheckEnvTests(unittest.TestCase):
    def test_all_good_returns_zero(self):
        out = io.StringIO()
        with mock.patch.object(app.core, "find_browser", return_value="/x/chrome"), \
                mock.patch.object(app.core, "find_ffmpeg", return_value="/x/ffmpeg"), \
                mock.patch.object(app, "can_reach", return_value=True), \
                redirect_stdout(out):
            code = app.check_env(mock.Mock(ffmpeg=None))
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("[OK]", text)
        self.assertIn("浏览器", text)

    def test_missing_browser_returns_one(self):
        out = io.StringIO()
        with mock.patch.object(app.core, "find_browser", return_value=None), \
                mock.patch.object(app.core, "find_ffmpeg", return_value=None), \
                mock.patch.object(app, "can_reach", return_value=False), \
                redirect_stdout(out):
            code = app.check_env(mock.Mock(ffmpeg=None))
        self.assertEqual(code, 1)
        self.assertIn("[缺少]", out.getvalue())


class RepoTests(unittest.TestCase):
    """这些约定靠人记不住，让测试帮忙记住。"""

    def test_bat_files_use_crlf(self):
        # cmd.exe 对 LF 换行的 .bat 会解析错位，.gitattributes 里已经写了规则
        for name in ("下载.bat", "环境检查.bat", "网页版.bat"):
            path = os.path.join(app.BASE_DIR, name)
            with open(path, "rb") as f:
                data = f.read()
            self.assertIn(b"\r\n", data, name)
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), name)

    def test_config_json_is_valid(self):
        path = os.path.join(app.BASE_DIR, "config.json")
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn(cfg["quality"], ("auto", "1080", "720", "540"))

    def test_web_page_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(app.BASE_DIR, "web", "index.html")))


if __name__ == "__main__":
    unittest.main()
