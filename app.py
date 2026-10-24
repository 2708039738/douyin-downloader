# -*- coding: utf-8 -*-
"""
抖音视频下载器（命令行 / 拖拽版）

用法：
    把链接或 txt 直接拖到 下载.bat 上
    或者：python app.py https://v.douyin.com/xxxx/

    python app.py --ui        打开网页版界面
    python app.py --checkenv  检查依赖是否齐全
"""

import argparse
import atexit
import os
import socket
import sys
import time
import urllib.request

import core
import spec

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = core.DEFAULT_CONFIG

QUALITY_CHOICES = {"1": "auto", "2": "1080", "3": "720", "4": "540"}

VERSIONS = {
    "1": [("nowm", False)],
    "2": [("wm", True)],
    "3": [("nowm", False), ("wm", True)],
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def c(text, code):
    """给终端一点点颜色，管道输出时自动关掉。"""
    if not sys.stdout.isatty():
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def line(char="─", width=54):
    print(c(char * width, "90"))


def human(n):
    n = float(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.0f KB" % (n / 1024)
    if n < 1073741824:
        return "%.1f MB" % (n / 1048576)
    return "%.2f GB" % (n / 1073741824)


def kill_stale_browsers():
    """兼容老调用：清掉上次残留的无头浏览器。"""
    try:
        core.kill_stale_browser()
    except Exception:
        pass


def pause_if_needed(args):
    if os.environ.get("DYDL_PAUSE") or (not args and sys.stdin.isatty()):
        try:
            input("\n按回车键关闭…")
        except (EOFError, KeyboardInterrupt):
            pass


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config():
    # 真正的读写在 core 里，网页版用的是同一个函数，免得两边行为漂移
    return core.load_config(CONFIG_PATH, wizard)


def save_config(cfg):
    core.save_config(CONFIG_PATH, cfg)


def ask(prompt, default="", valid=None):
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            if valid and default not in valid:
                return valid[0]
            return default
        value = raw or default
        if valid and value not in valid:
            print("  请输入 %s" % " / ".join(valid))
            continue
        return value


def wizard():
    # 不在终端里（比如被别的程序调用）就别傻等着输入
    if not sys.stdin.isatty():
        cfg = dict(DEFAULT_CONFIG)
        save_config(cfg)
        return cfg
    print()
    line("═")
    print("  第一次使用，先定几个默认值（以后想改，直接编辑 config.json）")
    line("═")
    print("  1) 最高画质   2) 1080P   3) 720P   4) 540P")
    q = ask("清晰度 [1]: ", "1", list(QUALITY_CHOICES))
    audio = ask("顺便提取 MP3？（需要有 ffmpeg）[y/N]: ", "n").lower().startswith("y")
    meta = not ask("顺便保存封面和文案？[Y/n]: ", "y").lower().startswith("n")
    limit = ask("主页链接最多抓多少个作品？[20]: ", "20")
    try:
        limit = max(1, int(limit))
    except ValueError:
        limit = 20

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "quality": QUALITY_CHOICES.get(q, "auto"),
        "save_audio": audio,
        "save_meta": meta,
        "profile_limit": limit,
    })
    save_config(cfg)
    print(c("\n设置已保存到 config.json。\n", "32"))
    return cfg


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #
def collect_inputs(args):
    """参数可以是链接、分享文案，也可以是装着链接的 txt 文件。"""
    texts = []
    for a in args:
        if os.path.isfile(a):
            try:
                with open(a, "r", encoding="utf-8", errors="ignore") as f:
                    texts.append(f.read())
                print("读取文件：%s" % os.path.basename(a))
                continue
            except Exception as e:  # noqa: BLE001
                print("读不了 %s：%s" % (a, e))
        texts.append(a)

    if not texts:
        print("把抖音链接或分享文案粘进来（可以多行，输入空行结束）：")
        while True:
            try:
                row = input("> ").strip()
            except EOFError:
                break
            if not row:
                break
            texts.append(row)

    urls = []
    for t in texts:
        for u in core.extract_urls(t):
            if u not in urls:
                urls.append(u)
        s = t.strip()
        if "douyin.com" not in s and s.startswith("http") and s not in urls:
            urls.append(s)
    return urls


# --------------------------------------------------------------------------- #
# 下载流程
# --------------------------------------------------------------------------- #
def ask_version():
    if not sys.stdin.isatty():
        print("  不在终端里，默认下无水印（要别的版本加 --watermark / --both）")
        return VERSIONS["1"]
    print()
    print("  下载哪个版本？")
    print("    1. 无水印（原片地址，画质最高）")
    print("    2. 带水印（抖音官方下载版）")
    print("    3. 两个都要")
    choice = ask("  请输入 1 / 2 / 3： ", "", list(VERSIONS))
    return VERSIONS[choice]


class Progress:
    def __init__(self, prefix):
        self.prefix = prefix
        self.last = 0.0
        self.start = time.time()

    def __call__(self, got, total):
        if not sys.stdout.isatty():
            return  # 输出被重定向时别刷屏
        now = time.time()
        finished = bool(total) and got >= total
        if not finished and now - self.last < 0.2:
            return
        self.last = now
        speed = got / max(now - self.start, 0.001)
        if total:
            pct = got * 100.0 / total
            filled = int(16 * pct / 100)
            bar = "█" * filled + "·" * (16 - filled)
            sys.stdout.write("\r  %s [%s] %5.1f%%  %s / %s  %s/s   " % (
                self.prefix, bar, pct, human(got), human(total), human(speed)))
        else:
            sys.stdout.write("\r  %s %s  %s/s   " % (self.prefix, human(got), human(speed)))
        sys.stdout.flush()
        if finished:
            sys.stdout.write("\n")


def build_versions(info, cfg, versions):
    """按版本把地址提前挑好，后面下载直接用。"""
    if info["kind"] == "gallery":
        info["filename"] = {"nowm": info["base"] + "[图集]", "wm": info["base"] + "[图集]"}
    else:
        info["filename"] = {
            "nowm": info["base"] + "[无水印]",
            "wm": info["base"] + "[带水印]",
        }
    out = {}
    for tag, wm in versions:
        if info["kind"] == "gallery":
            out[tag] = {"url": None, "label": "图集"}
            continue
        url, w, h, size, label = core.pick_media(
            info["aweme"], watermark=wm, quality=cfg.get("quality", "auto"),
            prefer_h264=cfg.get("prefer_h264", True))
        out[tag] = {"url": url, "label": label, "size": size}
    info["versions"] = out
    return info


def download_versions(info, versions, folder, ffmpeg, save_audio):
    saved = []
    if info["kind"] == "gallery":
        prog = Progress("图集")

        def on_fail(idx, url, err):
            print("  %s 第 %d 张没下到：%s" % (c("!", "33"), idx, str(err)[:60]))

        path = core.download_gallery(info["files"], folder, info["filename"]["nowm"],
                                     on_progress=prog, on_failure=on_fail)
        print("  %s %s" % (c("✓", "32"), os.path.basename(path)))
        return [path]

    first_video = None
    for tag, wm in versions:
        meta = info["versions"][tag]
        prog = Progress("带水印" if wm else "无水印")
        path = core.unique_path(folder, info["filename"][tag], "mp4")
        try:
            # 断网 / 半截响应自动重试，别让一次抖动毁掉整个任务
            core.download_with_retry(meta["url"], path, on_progress=prog)
        except Exception as e:  # noqa: BLE001
            print("\r  %s 下载失败：%s" % (c("✗", "31"), e))
            continue
        print("  %s %s  %s  %s" % (c("✓", "32"), os.path.basename(path),
                                   human(os.path.getsize(path)), meta["label"]))
        saved.append(path)
        if first_video is None:
            first_video = path

    if save_audio and first_video:
        saved.extend(handle_audio(info, first_video, folder, ffmpeg))
    return saved


def handle_audio(info, video_path, folder, ffmpeg):
    items = core.save_audio(info, video_path, folder, ffmpeg)
    for path, note in items:
        print("  %s %s（%s）" % (c("♪", "36"), os.path.basename(path), note))
    if not items:
        if ffmpeg:
            print("  %s MP3 提取失败，已跳过（检查 ffmpeg 或视频文件）" % c("!", "33"))
        else:
            print("  %s 没找到 ffmpeg，跳过 MP3" % c("!", "33"))
    return [path for path, _ in items]


def save_extras(info, folder, cfg):
    """存文案和封面。真正干活的是 core.save_extras，网页版用的是同一份。"""
    out = core.save_extras(info, folder, with_meta=cfg.get("save_meta", True))
    for path in out:
        print("  %s %s" % (c("✓", "32"), os.path.basename(path)))
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(args):
    cfg = load_config()
    if args.quality:
        cfg["quality"] = args.quality
    if args.h265:
        cfg["prefer_h264"] = False
    if args.audio:
        cfg["save_audio"] = True
    if args.no_meta:
        cfg["save_meta"] = False
    if args.meta:
        cfg["save_meta"] = True
    if args.max is not None:
        # 用 `is not None` 而不是真值判断：--max 0 以前会被悄悄吞掉
        cfg["profile_limit"] = max(1, args.max)
    folder = os.path.abspath(core.config_output_dir(BASE_DIR, cfg, args.out))
    os.makedirs(folder, exist_ok=True)

    inputs = collect_inputs(args.inputs)
    if not inputs:
        print("没识别到抖音链接。")
        return 1

    browser = core.Browser(log=lambda m: print(c("· " + m, "90")))
    atexit.register(browser.close)

    try:
        browser.start()

        targets = []
        for u in inputs:
            u = core.normalize_url(u)
            if core.is_profile_url(u):
                print("\n正在读主页：%s" % u)
                links = browser.profile_links(u, limit=cfg["profile_limit"])
                if not links:
                    print(c("  没读到作品，可能主页不可见或者需要登录。", "33"))
                    continue
                print("  取到 %d 个作品（上限 %d）" % (len(links), cfg["profile_limit"]))
                targets.extend(links)
            else:
                targets.append(u)

        seen = set()
        targets = [t for t in targets if not (t in seen or seen.add(t))]
        if not targets:
            print("没有可下载的作品。")
            return 1

        print("\n共 %d 个作品，开始解析…" % len(targets))
        items = []
        for idx, t in enumerate(targets, 1):
            try:
                aweme = browser.detail(t)
                info = core.describe(aweme, watermark=False, quality=cfg["quality"],
                                     prefer_h264=cfg["prefer_h264"])
                info["aweme"] = aweme
                items.append(info)
                print("  [%d/%d] %s %s  %s  %s" % (
                    idx, len(targets), c("✓", "32"),
                    core.short_desc(info["desc"], 22) or "无标题",
                    core.fmt_duration(info["duration"]),
                    info.get("quality_label", "")))
            except Exception as e:  # noqa: BLE001
                print("  [%d/%d] %s %s  %s" % (idx, len(targets), c("✗", "31"),
                                               t[-19:], str(e)[:60]))

        if not items:
            print("\n一个都没解析成功。")
            return 1

        if args.both:
            versions = VERSIONS["3"]
        elif args.watermark:
            versions = VERSIONS["2"]
        elif args.nowm:
            versions = VERSIONS["1"]
        else:
            versions = ask_version()

        ffmpeg = core.find_ffmpeg(args.ffmpeg) if cfg.get("save_audio") else None
        if cfg.get("save_audio") and not ffmpeg:
            print(c("\n  没找到 ffmpeg，MP3 会退化成下载抖音原声。", "33"))

        print()
        line()
        ok_files = []
        for idx, info in enumerate(items, 1):
            print("\n[%d/%d] %s" % (idx, len(items), core.short_desc(info["desc"], 28)))
            build_versions(info, cfg, versions)
            try:
                ok_files += download_versions(info, versions, folder, ffmpeg,
                                              cfg.get("save_audio"))
                ok_files += save_extras(info, folder, cfg)
            except Exception as e:  # noqa: BLE001
                print("  %s 出错：%s" % (c("✗", "31"), e))

        line()
        print("\n完成：%d 个文件，保存到" % len(ok_files))
        print("  %s" % folder)
        if ok_files and args.open_folder:
            if not core.open_in_file_manager(folder):
                print(c("  打不开文件管理器，请手动打开：%s" % folder, "33"))
        return 0

    finally:
        browser.close()


# --------------------------------------------------------------------------- #
# 环境检查
# --------------------------------------------------------------------------- #
def can_reach(host, port, timeout=4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def check_env(args):
    """逐项报告运行依赖：Python 版本、浏览器、ffmpeg、网络。"""
    print()
    line()
    print("  环境检查")
    line()

    issues = 0

    ver = "%d.%d.%d" % sys.version_info[:3]
    ok = sys.version_info >= (3, 9)
    print("  %s Python %s%s" % (
        c("[OK]  ", "92") if ok else c("[缺少]", "91"),
        ver,
        "" if ok else c("   需要 3.9 或更新", "93")))
    if not ok:
        issues += 1

    print("  %s pip 依赖：不需要，全部用标准库" % c("[OK]  ", "92"))

    browser = core.find_browser()
    if browser:
        print("  %s 浏览器：%s" % (c("[OK]  ", "92"), browser))
    else:
        print("  %s 浏览器：没找到 Edge / Chrome，拿不到视频地址"
              % c("[缺少]", "91"))
        issues += 1

    ffmpeg = core.find_ffmpeg(getattr(args, "ffmpeg", None))
    if ffmpeg:
        print("  %s ffmpeg：%s" % (c("[OK]  ", "92"), ffmpeg))
    else:
        print("  %s ffmpeg：没找到，只有「提取 MP3」用不了，其他功能正常"
              % c("[可选]", "93"))

    if can_reach("www.douyin.com", 443):
        print("  %s 网络：能连上 www.douyin.com" % c("[OK]  ", "92"))
    else:
        print("  %s 网络：连不上 www.douyin.com，检查网络或代理"
              % c("[可选]", "93"))

    line()
    if issues:
        print(c("  有 %d 项必需依赖没满足，先补齐再用。" % issues, "91"))
    else:
        print(c("  必需依赖齐全，可以直接用。", "92"))
    print()
    # 退出码要能反映结果：脚本 / CI 靠它判断，只有 0 和 1 两种
    return 1 if issues else 0


# --------------------------------------------------------------------------- #
# 端到端自检
# --------------------------------------------------------------------------- #
SELFTEST_ENV = "DYDL_SELFTEST_AWEME"


def self_test(args):
    """真跑一遍完整链路，确认「抖音这条路现在还能走」。

    退出码 0 = 全过，1 = 有失败。CI 的每日定时任务靠这个判断要不要告警。

    失败提示会指向 spec.py 里该改的常量 —— 抖音改版时改那里就够了，
    不用重新读一遍 core.py。
    """
    print()
    line()
    print("  端到端自检：真实访问抖音，验证接口契约还有效")
    line()

    cfg = load_config()
    sample = (args.selftest if isinstance(args.selftest, str) else "")
    sample = (sample or os.environ.get(SELFTEST_ENV)
              or cfg.get("selftest_aweme") or "").strip()

    failures = []

    def ok(msg, extra=""):
        print("  %s %s%s" % (c("[通过] ", "92"), msg, ("   " + extra) if extra else ""))

    def bad(msg, hint=""):
        print("  %s %s" % (c("[失败] ", "91"), msg))
        if hint:
            print("         %s" % c(hint, "90"))
        failures.append(msg)

    def skip(msg):
        print("  %s %s" % (c("[跳过] ", "93"), msg))

    browser_path = core.find_browser()
    if not browser_path:
        bad("没找到 Edge / Chrome", "先装浏览器；完整依赖检查见 --checkenv")
        line()
        return 1
    ok("浏览器", browser_path)

    if not sample:
        bad("还没设置自检样片",
            "找一条长期存在的公开视频，跑：python app.py --selftest <链接>\n"
            "         程序会把这条记进 config.json，以后直接 --selftest 就行。\n"
            "         也可以设环境变量 %s 给 CI 用。" % SELFTEST_ENV)
        line()
        return 1

    url = core.normalize_url(sample)
    aweme_id = core.extract_aweme_id(url)
    ok("样片", "%s" % url)

    browser = core.Browser(log=lambda m: None)
    atexit.register(browser.close)

    try:
        browser.start()
    except Exception as e:
        bad("无头浏览器起不来：%s" % e, "换个浏览器，或跑 --cleanup 清掉残留进程")
        line()
        return 1
    ok("无头浏览器启动，已预热拿到 ttwid")

    try:
        aweme = browser.detail(url)
    except core.BrowserError as e:
        bad("解析失败：%s" % e,
            "若提示「解析超时」，多半是改版了：\n"
            "         接口路径没截到 → spec.DETAIL_URL_RE\n"
            "         截到了但取不出数据 → spec.DETAIL_WRAPPER_KEYS")
        line()
        return 1

    got_id = spec.aweme_id_of(aweme)
    ok("详情接口截包成功", "aweme_id=%s" % (got_id or "未识别"))
    if aweme_id and got_id and aweme_id != got_id:
        bad("返回的作品和请求的不是同一条", "可能被风控改写了响应，或者样片已失效")

    media_url = None
    try:
        media_url, w, h, size, label = core.pick_media(aweme)
        ok("媒体字段", "%s  %sx%s" % (label, w or "?", h or "?"))
    except Exception as e:
        bad("媒体字段失配：%s" % e,
            "改 spec.BITRATE_LIST_KEYS / PLAY_ADDR_KEYS / URL_LIST_KEYS")

    try:
        wm_url = core.pick_media(aweme, watermark=True)[0]
        ok("带水印地址", wm_url.split("?")[0][-40:])
    except Exception:
        skip("带水印地址：这条样片没有（不影响无水印）")

    if media_url:
        try:
            req = urllib.request.Request(media_url,
                                         headers=dict(core.HEADERS, Range="bytes=0-1"))
            with urllib.request.urlopen(req, timeout=15) as r:
                code = getattr(r, "status", 200) or 200
            ok("视频地址可下载", "HTTP %d" % code)
        except Exception as e:
            bad("视频地址取不到：%s" % e, "地址过期，或 CDN 域名换了")

    canonical = ("https://www.douyin.com/video/%s" % got_id) if got_id else url
    if canonical != (cfg.get("selftest_aweme") or ""):
        cfg["selftest_aweme"] = canonical
        try:
            core.save_config(CONFIG_PATH, cfg)
            print("  %s 样片已记进 config.json：%s" % (c("[记录] ", "90"), canonical))
        except Exception:
            pass

    line()
    if failures:
        print(c("  自检失败 %d 项。上面标了该动 spec.py 哪一段。" % len(failures), "91"))
        return 1
    print(c("  全部通过：抖音这条路现在能用。", "92"))
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="抖音视频下载器：粘链接就能下，可选带/不带水印",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python app.py https://v.douyin.com/xxxx/ --nowm")
    parser.add_argument("inputs", nargs="*", help="链接、分享文案，或者装着链接的 txt")
    parser.add_argument("--nowm", action="store_true", help="只下无水印")
    parser.add_argument("--watermark", action="store_true", help="只下带水印")
    parser.add_argument("--both", action="store_true", help="两个版本都下")
    parser.add_argument("--quality", choices=["auto", "1080", "720", "540"], help="清晰度")
    parser.add_argument("--h265", action="store_true", help="允许 H.265（画质更高，兼容性差）")
    parser.add_argument("--audio", action="store_true", help="顺便提取 MP3")
    parser.add_argument("--meta", action="store_true", help="保存封面和文案")
    parser.add_argument("--no-meta", action="store_true", help="不保存封面和文案")
    parser.add_argument("--max", type=int, help="主页链接最多抓多少个作品")
    parser.add_argument("--out", help="保存目录，默认 ./downloads")
    parser.add_argument("--ffmpeg", help="ffmpeg 路径（找不到时手动指定）")
    parser.add_argument("--open-folder", action="store_true", help="结束后打开保存目录")
    parser.add_argument("--ui", action="store_true", help="打开网页版界面")
    parser.add_argument("--cleanup", action="store_true",
                        help="清理上次被强杀后残留的无头浏览器，然后退出")
    parser.add_argument("--checkenv", action="store_true",
                        help="检查运行依赖（Python / 浏览器 / ffmpeg / 网络），然后退出")
    parser.add_argument("--selftest", nargs="?", const=True, metavar="链接",
                        help="端到端自检：用一条真实作品验证抖音接口还能不能用，"
                             "首次可带上链接，之后会自动记住")
    args = parser.parse_args()

    if args.checkenv:
        return check_env(args)

    if args.selftest:
        return self_test(args)

    if args.cleanup:
        core.kill_stale_browser()
        print("已清理残留的无头浏览器。")
        return 0

    if args.ui:
        import webui
        webui.serve()
        return 0

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    finally:
        pause_if_needed(args.inputs)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
