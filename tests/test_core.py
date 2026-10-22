# -*- coding: utf-8 -*-
"""
不联网的单元测试：只覆盖纯函数（链接解析、命名、清晰度选择、格式化）。
需要网络和浏览器的部分（Browser / 下载）不在这里测。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402


def aweme_with_gears():
    """三档清晰度：720P H.264、1080P H.264、1080P H.265，外加带水印地址。"""
    return {
        "aweme_id": "7412345678901234567",
        "author": {"nickname": "川不慌"},
        "desc": "气头上说的话算不算数 #情感 #日常",
        "video": {
            "bit_rate": [
                {"is_h265": False,
                 "play_addr": {"width": 720, "height": 1280, "data_size": 1000,
                               "url_list": ["https://cdn/720.mp4"]}},
                {"is_h265": False,
                 "play_addr": {"width": 1080, "height": 1920, "data_size": 2000,
                               "url_list": ["https://cdn/1080.mp4"]}},
                {"is_h265": True,
                 "play_addr": {"width": 1080, "height": 1920, "data_size": 1500,
                               "url_list": ["https://cdn/1080_h265.mp4"]}},
            ],
            "download_addr": {"width": 720, "height": 1280, "data_size": 3000,
                              "url_list": ["https://cdn/wm.mp4"]},
        },
    }


class TestLinkParsing(unittest.TestCase):
    def test_extract_urls_from_share_text(self):
        text = ("7.43 复制打开抖音，看看【川不慌】的作品 "
                "https://v.douyin.com/iRabcDEF/ 复制此链接，打开Dou音搜索")
        self.assertEqual(core.extract_urls(text), ["https://v.douyin.com/iRabcDEF/"])

    def test_extract_urls_dedupes_and_filters(self):
        text = ("https://v.douyin.com/aaa/ https://v.douyin.com/aaa/ "
                "https://example.com/x https://www.douyin.com/video/7412345678901234567")
        self.assertEqual(core.extract_urls(text),
                         ["https://v.douyin.com/aaa/",
                          "https://www.douyin.com/video/7412345678901234567"])

    def test_extract_urls_empty(self):
        self.assertEqual(core.extract_urls(""), [])
        self.assertEqual(core.extract_urls(None), [])

    def test_extract_aweme_id(self):
        self.assertEqual(core.extract_aweme_id("https://www.douyin.com/video/7412345678901234567"),
                         "7412345678901234567")
        self.assertEqual(core.extract_aweme_id("https://www.douyin.com/discover?modal_id=7412345678901234567"),
                         "7412345678901234567")
        self.assertIsNone(core.extract_aweme_id("https://www.douyin.com/user/MS4wLjABAAAA"))
        self.assertIsNone(core.extract_aweme_id(""))

    def test_is_profile_url(self):
        self.assertTrue(core.is_profile_url("https://www.douyin.com/user/MS4wLjABAAAA"))
        self.assertFalse(core.is_profile_url("https://www.douyin.com/video/7412345678901234567"))

    def test_normalize_url(self):
        self.assertIsNone(core.normalize_url(""))
        self.assertIsNone(core.normalize_url(None))
        # 主页链接补协议头，其余不动
        self.assertEqual(core.normalize_url("www.douyin.com/user/MS4wLjABAAAA"),
                         "https://www.douyin.com/user/MS4wLjABAAAA")
        # 作品链接统一成 /video/ 或 /note/
        self.assertEqual(core.normalize_url("https://www.douyin.com/video/7412345678901234567?foo=1"),
                         "https://www.douyin.com/video/7412345678901234567")
        self.assertEqual(core.normalize_url("https://www.douyin.com/note/7412345678901234567"),
                         "https://www.douyin.com/note/7412345678901234567")
        self.assertEqual(core.normalize_url("https://www.douyin.com/slides/7412345678901234567"),
                         "https://www.douyin.com/note/7412345678901234567")


class TestNaming(unittest.TestCase):
    def test_safe_name_strips_illegal_chars(self):
        self.assertEqual(core.safe_name('a/b:c*d?e"f<g>h|i'), "a b c d e f g h i")
        self.assertEqual(core.safe_name("a\r\nb"), "a b")

    def test_safe_name_fallback(self):
        self.assertEqual(core.safe_name(""), "douyin")
        self.assertEqual(core.safe_name("   ...   "), "douyin")
        self.assertEqual(core.safe_name(None), "douyin")

    def test_safe_name_limit(self):
        self.assertEqual(core.safe_name("x" * 100, 10), "x" * 10)

    def test_short_desc_drops_topics(self):
        self.assertEqual(core.short_desc("今天心情不错 #日常 #vlog"), "今天心情不错")
        self.assertEqual(core.short_desc("@某人 你好"), "你好")

    def test_short_desc_falls_back_to_raw_desc(self):
        # 全是话题、清空后什么都不剩 → 退回原始文案，总比空文件名好
        self.assertEqual(core.short_desc("#只有话题"), "#只有话题")

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            first = core.unique_path(d, "片子", "mp4")
            self.assertEqual(os.path.basename(first), "片子.mp4")
            open(first, "w").close()
            self.assertEqual(os.path.basename(core.unique_path(d, "片子", "mp4")), "片子(1).mp4")
            open(os.path.join(d, "片子(1).mp4"), "w").close()
            self.assertEqual(os.path.basename(core.unique_path(d, "片子", "mp4")), "片子(2).mp4")


class TestPickMedia(unittest.TestCase):
    def test_nominal_uses_short_edge(self):
        self.assertEqual(core._nominal({"width": 1080, "height": 1920}), 1080)
        self.assertEqual(core._nominal({"width": 0, "height": 720}), 720)
        self.assertEqual(core._nominal({}), 0)

    def test_auto_picks_highest_h264(self):
        url, w, h, size, label = core.pick_media(aweme_with_gears())
        self.assertEqual(url, "https://cdn/1080.mp4")
        self.assertEqual(label, "1080P")
        self.assertEqual((w, h, size), (1080, 1920, 2000))

    def test_quality_cap(self):
        self.assertEqual(core.pick_media(aweme_with_gears(), quality="720")[0],
                         "https://cdn/720.mp4")
        # 要 540 但没有比它更低的档 → 给最低的一档，而不是报错
        self.assertEqual(core.pick_media(aweme_with_gears(), quality="540")[0],
                         "https://cdn/720.mp4")

    def test_watermark_uses_download_addr(self):
        url, _, _, _, label = core.pick_media(aweme_with_gears(), watermark=True)
        self.assertEqual(url, "https://cdn/wm.mp4")
        self.assertEqual(label, "官方带水印")

    def test_h265_only_when_no_h264(self):
        aweme = {"video": {"bit_rate": [
            {"is_h265": True,
             "play_addr": {"width": 1080, "height": 1920,
                           "url_list": ["https://cdn/1080_h265.mp4"]}}]}}
        label = core.pick_media(aweme)[4]
        self.assertEqual(label, "1080P H.265")

    def test_falls_back_to_play_addr(self):
        aweme = {"video": {"play_addr": {"width": 720, "height": 1280,
                                         "url_list": ["https://cdn/raw.mp4"]}}}
        self.assertEqual(core.pick_media(aweme)[0], "https://cdn/raw.mp4")

    def test_missing_media_raises(self):
        with self.assertRaises(core.BrowserError):
            core.pick_media({"video": {}})
        with self.assertRaises(core.BrowserError):
            core.pick_media({"video": {}}, watermark=True)


class TestDescribe(unittest.TestCase):
    def test_video_filename(self):
        info = core.describe(aweme_with_gears())
        self.assertEqual(info["kind"], "video")
        self.assertEqual(info["ext"], "mp4")
        self.assertEqual(info["base"], "川不慌-气头上说的话算不算数")
        self.assertEqual(info["filename"], "川不慌-气头上说的话算不算数[无水印]")
        self.assertEqual(info["page_url"], "https://www.douyin.com/video/7412345678901234567")

    def test_watermark_filename(self):
        self.assertTrue(core.describe(aweme_with_gears(), watermark=True)["filename"]
                        .endswith("[带水印]"))

    def test_gallery(self):
        aweme = aweme_with_gears()
        aweme["images"] = [{"url_list": ["https://cdn/1.jpg"]}, {"url_list": ["https://cdn/2.jpg"]}]
        info = core.describe(aweme)
        self.assertEqual(info["kind"], "gallery")
        self.assertEqual(info["ext"], "zip")
        self.assertEqual(info["filename"], "川不慌-气头上说的话算不算数[图集]")
        self.assertEqual(info["quality_label"], "2 张图")

    def test_missing_author_and_desc(self):
        info = core.describe({"aweme_id": "7412345678901234567",
                              "video": {"play_addr": {"url_list": ["https://cdn/x.mp4"]}}})
        self.assertEqual(info["author"], "未知作者")
        self.assertEqual(info["title"], "无标题作品")
        self.assertEqual(info["base"], "未知作者-douyin")


class TestFormatting(unittest.TestCase):
    def test_fmt_duration(self):
        self.assertEqual(core.fmt_duration(0), "")
        self.assertEqual(core.fmt_duration(5000), "0:05")
        self.assertEqual(core.fmt_duration(90000), "1:30")
        self.assertEqual(core.fmt_duration(3723000), "62:03")

    def test_fmt_count(self):
        self.assertEqual(core.fmt_count(0), "0")
        self.assertEqual(core.fmt_count(1234), "1234")
        self.assertEqual(core.fmt_count(12345), "1.2万")
        self.assertEqual(core.fmt_count(123456789), "1.2亿")


if __name__ == "__main__":
    unittest.main(verbosity=2)
