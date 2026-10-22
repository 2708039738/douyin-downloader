# -*- coding: utf-8 -*-
"""core.py 的单元测试：不联网、不打开浏览器，几秒钟跑完。"""

import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402
from tests.helpers import LocalServer  # noqa: E402


def make_aweme():
    """一份最小的 aweme_detail，字段形状照抄抖音真实返回。"""
    return {
        "aweme_id": "7123456789012345678",
        "desc": "今天的晚霞 #治愈 #风景 @小王",
        "create_time": 1700000000,
        "author": {"nickname": "小明"},
        "statistics": {"digg_count": 12345, "comment_count": 67,
                       "collect_count": 890, "share_count": 12},
        "music": {"title": "原声", "play_url": {"url_list": ["https://x/music"]}},
        "video": {
            "duration": 65000,
            "cover": {"url_list": ["https://x/cover.jpg"]},
            "play_addr": {"url_list": ["https://x/orig"],
                          "width": 1080, "height": 1920, "data_size": 999},
            "download_addr": {"url_list": ["https://x/wm"],
                              "width": 720, "height": 1280, "data_size": 500},
            "bit_rate": [
                {"is_h265": 1, "play_addr": {"url_list": ["https://x/1080h265"],
                                             "width": 1080, "height": 1920,
                                             "data_size": 300}},
                {"is_h265": 0, "play_addr": {"url_list": ["https://x/1080h264"],
                                             "width": 1080, "height": 1920,
                                             "data_size": 400}},
                {"is_h265": 0, "play_addr": {"url_list": ["https://x/720h264"],
                                             "width": 720, "height": 1280,
                                             "data_size": 200}},
            ],
        },
    }


class LinkTests(unittest.TestCase):
    def test_extract_urls_from_share_text(self):
        text = ("7.93 复制打开抖音，看看【小明的作品】 "
                "https://v.douyin.com/iRNBho6u/ 复制此链接，打开Dou音搜索！")
        self.assertEqual(core.extract_urls(text), ["https://v.douyin.com/iRNBho6u/"])

    def test_extract_urls_ignores_other_sites(self):
        self.assertEqual(core.extract_urls("https://example.com/a"), [])
        self.assertEqual(core.extract_urls(""), [])

    def test_extract_urls_dedupes(self):
        text = "https://www.douyin.com/video/7123456789012345678 同一个链接"
        text += " https://www.douyin.com/video/7123456789012345678"
        self.assertEqual(len(core.extract_urls(text)), 1)

    def test_extract_aweme_id_variants(self):
        cases = {
            "https://www.douyin.com/video/7123456789012345678": "7123456789012345678",
            "https://www.douyin.com/note/7123456789012345678": "7123456789012345678",
            "https://www.iesdouyin.com/share/video/7123456789012345678/?region=CN":
                "7123456789012345678",
            "https://www.douyin.com/discover?modal_id=7123456789012345678":
                "7123456789012345678",
            "https://www.douyin.com/slides/7123456789012345678": "7123456789012345678",
            "https://www.douyin.com/user/MS4wLjABAAAA": None,
        }
        for url, expected in cases.items():
            self.assertEqual(core.extract_aweme_id(url), expected, url)

    def test_is_profile_url(self):
        self.assertTrue(core.is_profile_url("https://www.douyin.com/user/MS4wLjABAAAA"))
        self.assertFalse(core.is_profile_url("https://www.douyin.com/video/7123456789012345678"))

    def test_normalize_url(self):
        self.assertEqual(core.normalize_url("https://www.douyin.com/video/7123456789012345678"),
                         "https://www.douyin.com/video/7123456789012345678")
        self.assertEqual(core.normalize_url("https://www.douyin.com/slides/7123456789012345678"),
                         "https://www.douyin.com/note/7123456789012345678")
        self.assertEqual(core.normalize_url("https://www.douyin.com/discover?modal_id=7123456789012345678"),
                         "https://www.douyin.com/video/7123456789012345678")
        self.assertIsNone(core.normalize_url(""))

    def test_normalize_short_link_uses_redirect(self):
        # 把网络请求换成固定返回值，测试就不需要联网了
        final = "https://www.douyin.com/video/7123456789012345678"
        with mock.patch.object(core, "_follow_redirect", return_value=final):
            self.assertEqual(core.normalize_url("https://v.douyin.com/abc/"), final)


class NameTests(unittest.TestCase):
    def test_safe_name_strips_windows_illegal_chars(self):
        self.assertEqual(core.safe_name('a/b:c*?"<>|d'), "a b c d")

    def test_safe_name_falls_back(self):
        self.assertEqual(core.safe_name(""), "douyin")
        self.assertEqual(core.safe_name(" .. "), "douyin")

    def test_safe_name_limits_length(self):
        self.assertEqual(len(core.safe_name("x" * 100)), 70)

    def test_short_desc_removes_tags_and_mentions(self):
        self.assertEqual(core.short_desc("今天的晚霞 #治愈 @小王  "), "今天的晚霞")

    def test_short_desc_keeps_original_when_all_tags(self):
        self.assertTrue(core.short_desc("#只有话题"))


class PickMediaTests(unittest.TestCase):
    def test_default_picks_highest_h264(self):
        url, _, _, _, label = core.pick_media(make_aweme())
        self.assertEqual(url, "https://x/1080h264")
        self.assertEqual(label, "1080P")

    def test_quality_cap(self):
        url, _, _, _, label = core.pick_media(make_aweme(), quality="720")
        self.assertEqual(url, "https://x/720h264")
        self.assertEqual(label, "720P")

    def test_quality_below_everything_falls_back_to_lowest(self):
        url, _, _, _, label = core.pick_media(make_aweme(), quality="480")
        self.assertEqual(url, "https://x/720h264")

    def test_h265_allowed_when_no_h264(self):
        aweme = {"video": {"bit_rate": [
            {"is_h265": 1, "play_addr": {"url_list": ["https://x/h265"],
                                         "width": 1080, "height": 1920}},
        ]}}
        self.assertEqual(core.pick_media(aweme)[0], "https://x/h265")

    def test_watermark_uses_download_addr(self):
        url, w, h, size, label = core.pick_media(make_aweme(), watermark=True)
        self.assertEqual(url, "https://x/wm")
        self.assertEqual((w, h, size, label), (720, 1280, 500, "官方带水印"))

    def test_no_bitrate_falls_back_to_play_addr(self):
        aweme = {"video": {"play_addr": {"url_list": ["https://x/orig"]}}}
        self.assertEqual(core.pick_media(aweme)[0], "https://x/orig")

    def test_no_video_url_raises(self):
        with self.assertRaises(core.BrowserError):
            core.pick_media({"video": {}})

    def test_watermark_missing_raises(self):
        with self.assertRaises(core.BrowserError):
            core.pick_media({"video": {}}, watermark=True)


class DescribeTests(unittest.TestCase):
    def test_video_info(self):
        info = core.describe(make_aweme())
        self.assertEqual(info["kind"], "video")
        self.assertEqual(info["ext"], "mp4")
        self.assertEqual(info["author"], "小明")
        self.assertEqual(info["duration"], 65000)
        self.assertEqual(info["stats"]["digg"], 12345)
        self.assertEqual(info["page_url"],
                         "https://www.douyin.com/video/7123456789012345678")
        self.assertIn("无水印", info["filename"])

    def test_gallery_info(self):
        aweme = {"aweme_id": "1", "desc": "图集", "author": {"nickname": "作者"},
                 "images": [{"url_list": ["a"]}, {"url_list": ["b"]}]}
        info = core.describe(aweme)
        self.assertEqual(info["kind"], "gallery")
        self.assertEqual(info["ext"], "zip")
        self.assertEqual(info["quality_label"], "2 张图")
        self.assertEqual(info["files"], ["a", "b"])

    def test_formatting_helpers(self):
        self.assertEqual(core.fmt_count(12345), "1.2万")
        self.assertEqual(core.fmt_count(123456789), "1.2亿")
        self.assertEqual(core.fmt_count(7), "7")
        self.assertEqual(core.fmt_duration(65000), "1:05")
        self.assertEqual(core.fmt_duration(0), "")


class DownloadTests(unittest.TestCase):
    def test_download_stream_writes_whole_file(self):
        payload = b"video-bytes" * 1000
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/v.mp4": payload}) as base:
            path = os.path.join(tmp, "v.mp4")
            seen = []
            got = core.download_stream(base + "/v.mp4", path, on_progress=lambda a, b: seen.append(a))
            self.assertEqual(got, len(payload))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), payload)
            self.assertTrue(seen)

    def test_download_stream_works_without_content_length(self):
        # 服务器用分块传输、不发 Content-Length 时也要能下完
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/v.mp4": b"abc" * 10}, send_length=False) as base:
            path = os.path.join(tmp, "v.mp4")
            self.assertEqual(core.download_stream(base + "/v.mp4", path), 30)
            self.assertTrue(os.path.exists(path))

    def test_download_gallery_packs_zip_and_skips_broken_images(self):
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/1.jpg": b"one", "/2.jpg": b"two"}) as base:
            path = core.download_gallery(
                [base + "/1.jpg", base + "/missing.jpg", base + "/2.jpg"],
                tmp, "图集")
            self.assertTrue(path.endswith(".zip"))
            with zipfile.ZipFile(path) as zf:
                self.assertEqual(sorted(zf.namelist()), ["01.jpg", "03.jpg"])
                self.assertEqual(zf.read("01.jpg"), b"one")

    def test_download_gallery_raises_when_nothing_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({}) as base:
            with self.assertRaises(core.BrowserError):
                core.download_gallery([base + "/missing.jpg"], tmp, "图集")

    def test_unique_path_avoids_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = core.unique_path(tmp, "a", "mp4")
            open(first, "wb").close()
            second = core.unique_path(tmp, "a", "mp4")
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith("a(1).mp4"))

    def test_save_meta_writes_readable_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = core.save_meta(core.describe(make_aweme()), tmp)
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            self.assertIn("作者：@小明", text)
            self.assertIn("点赞 1.2万", text)
            self.assertIn("今天的晚霞", text)

    def test_save_cover_writes_image(self):
        with tempfile.TemporaryDirectory() as tmp, \
                LocalServer({"/cover.jpg": b"fake-jpg"}) as base:
            info = core.describe(make_aweme())
            info["cover_url"] = base + "/cover.jpg"
            path = core.save_cover(info, tmp)
            self.assertTrue(path.endswith("[封面].jpg"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"fake-jpg")

    def test_save_cover_returns_none_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = core.describe(make_aweme())
            info["cover_url"] = "http://127.0.0.1:1/nope.jpg"
            self.assertIsNone(core.save_cover(info, tmp))


class FfmpegTests(unittest.TestCase):
    def test_find_ffmpeg_honours_env_var(self):
        with tempfile.NamedTemporaryFile(suffix=".exe") as f:
            with mock.patch.dict(os.environ, {"FFMPEG_PATH": f.name}):
                self.assertEqual(core.find_ffmpeg(), f.name)

    def test_extract_mp3_reports_failure(self):
        failed = mock.Mock(returncode=1, stderr=b"boom")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(core.subprocess, "run", return_value=failed):
            with self.assertRaises(core.BrowserError):
                core.extract_mp3(os.path.join(tmp, "v.mp4"),
                                 os.path.join(tmp, "a.mp3"), "ffmpeg")

    def test_extract_mp3_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "a.mp3")

            def fake_run(*args, **kwargs):
                open(out, "wb").close()
                return mock.Mock(returncode=0, stderr=b"")

            with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
                self.assertEqual(core.extract_mp3(os.path.join(tmp, "v.mp4"), out, "ffmpeg"), out)


if __name__ == "__main__":
    unittest.main()
