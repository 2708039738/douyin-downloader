# -*- coding: utf-8 -*-
"""
抖音接口契约 —— 唯一真相源。

抖音是黑盒，我们依赖它的三样东西：接口路径、响应结构、页面选择器。
这三样会变，但它们只应该在这一个文件里出现。**改版时只改这里，不要动 core.py。**

每个常量都刻意写得宽容一些，让「不破坏结构」的改版自动兼容：

- 接口路径用正则，抖音加版本号、换前缀也能命中
- 响应体不认固定 key，递归找「像作品」的 dict
- 字段名给一串候选，谁有值用谁

怎么知道改版了：``python app.py --selftest``。
它会告诉你失配的是哪一类，然后回来改对应的常量。
"""

import re

# --------------------------------------------------------------------------- #
# 1. 详情接口 —— 截包时认哪个 URL
# --------------------------------------------------------------------------- #
# 旧写法是精确包含 "aweme/v1/web/aweme/detail"。
# 现在只要求「aweme 开头的一段 + detail」，抖音把 v1 改成 v2、
# 前面加 /web/、换成 /aweme/detail/ 都还能中。
DETAIL_URL_RE = re.compile(r"aweme[^?]*detail", re.I)

# 图集 / 图文走另一套接口，路径里没有 detail
GALLERY_URL_RE = re.compile(r"/web/(?:slides|note|image)", re.I)


def is_detail_url(url):
    """这个响应的 body 里可能有作品数据吗？"""
    url = url or ""
    return bool(DETAIL_URL_RE.search(url) or GALLERY_URL_RE.search(url))


# --------------------------------------------------------------------------- #
# 2. 响应体结构 —— 数据藏在哪一层
# --------------------------------------------------------------------------- #
# 旧写法直接取 data["aweme_detail"]。现在先试这些已知的包装 key，
# 都对不上就递归搜索（见 find_aweme）。抖音把值改名、外面再包一层 data、
# 塞进 list 里，都不用改代码。
DETAIL_WRAPPER_KEYS = ("aweme_detail", "awemeDetail", "aweme_info", "awemeInfo")

# 作品的唯一标识：递归识别作品本体时，必须命中其中之一
AWEME_ID_KEYS = ("aweme_id", "awemeId", "item_id", "itemId")

# 作品必须带一个媒体容器，否则只是个普通 dict（比如作者、统计信息）
MEDIA_KEYS = ("video", "images", "image_post_info", "imagePostInfo")

# 失败时抖音会把原因写在这几个位置，用来给用户一句人话
BLOCK_WRAPPER_KEYS = ("filter_detail", "filterDetail")
BLOCK_MESSAGE_KEYS = ("detail_msg", "detailMsg", "notice", "message")


# --------------------------------------------------------------------------- #
# 3. 媒体字段 —— 地址在哪个 key 下面
# --------------------------------------------------------------------------- #
# 无水印的多档清晰度列表。按顺序试，第一个非空列表生效。
BITRATE_LIST_KEYS = ("bit_rate", "bitRateList", "bit_rate_list", "gear_list")

# 每一档里的播放地址
PLAY_ADDR_KEYS = ("play_addr", "playAddr", "play_addr_265", "playApi")

# 带水印的官方下载地址
DOWNLOAD_ADDR_KEYS = ("download_addr", "downloadAddr", "download_api", "downloadApi")

# 一档地址里真正的 url 列表
URL_LIST_KEYS = ("url_list", "urlList", "urls", "url")

# 判断这一档是不是 H.265（兼容性差，默认不优先）
H265_FLAGS = ("is_h265", "is_bytevc1", "isH265", "isBytevc1", "is_hevc")

# 封面 / 音乐，同理会改名
COVER_KEYS = ("cover", "origin_cover", "originCover", "dynamic_cover")
MUSIC_PLAY_KEYS = ("play_url", "playUrl")
MUSIC_URL_KEYS = ("url_list", "urlList", "urls")


# --------------------------------------------------------------------------- #
# 4. 主页 —— 收藏作品链接靠什么找
# --------------------------------------------------------------------------- #
# 旧写法写死 a[href*="/video/"]。主页改版时改这里。
PROFILE_LINK_SELECTOR = 'a[href*="/video/"], a[href*="/note/"], a[href*="/slides/"]'

# 从 href 里抠作品 id（第一组是路径类型，第二组是 id）
PROFILE_ID_RE = re.compile(r"/(video|note|slides)/(\d{15,25})")


# --------------------------------------------------------------------------- #
# 5. 反爬指纹 —— 让无头浏览器看起来像正常访客
# --------------------------------------------------------------------------- #
# 实测：headless 的 UA 里带 "HeadlessChrome/155.0.0.0"，
# 抖音靠这个标志直接判定无头，返回「验证码中间页」，连 ttwid 都不发，
# 之后所有详情请求全被拦。只抹 navigator.webdriver 无效，必须改 UA。
# 换浏览器 / 抖音换识别方式时，改这里。
HEADLESS_UA_MARKS = (
    ("HeadlessChrome", "Chrome"),
    ("HeadlessEdg", "Edg"),
    ("Headless", ""),
)

STEALTH_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
)


def clean_user_agent(ua):
    """把无头浏览器 UA 的 Headless 标识抹掉，其余保持原样（版本号不能改，
    否则和 userAgentData、sec-ch-ua 头对不上，反而更像机器人）。"""
    ua = ua or ""
    for bad, good in HEADLESS_UA_MARKS:
        ua = ua.replace(bad, good)
    return ua


def is_headless_ua(ua):
    return any(bad in (ua or "") for bad, _ in HEADLESS_UA_MARKS)


# --------------------------------------------------------------------------- #
# 6. 取值小工具（core.py 用这些，不直接用字段名）
# --------------------------------------------------------------------------- #
def first_value(obj, keys, default=None):
    """按优先级取第一个有值的键。

    空字符串、空列表、空字典都算「没有」，继续试下一个候选名。
    """
    if not isinstance(obj, dict):
        return default
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def url_list(addr):
    """从一个地址 dict 里取出 url 列表，统一成 list[str]。"""
    v = first_value(addr, URL_LIST_KEYS)
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        return [u for u in v if isinstance(u, str) and u]
    return []


def has_h265(gear):
    if not isinstance(gear, dict):
        return False
    return any(gear.get(k) for k in H265_FLAGS)


def looks_like_aweme(node):
    """这个 dict 像不像一个作品本体？

    条件是「有 id」+「有媒体容器」。作者对象有 id 但没有 video，
    统计对象两者都没有，所以不会误判。
    """
    if not isinstance(node, dict):
        return False
    if not any(node.get(k) for k in AWEME_ID_KEYS):
        return False
    return any(isinstance(node.get(k), (dict, list)) and node.get(k) for k in MEDIA_KEYS)


def aweme_id_of(node):
    if not isinstance(node, dict):
        return ""
    for k in AWEME_ID_KEYS:
        v = node.get(k)
        if v:
            return str(v)
    return ""


def iter_aweme_candidates(root, max_depth=8, max_nodes=20000):
    """广度优先遍历响应体，产出所有像作品本体的 dict。

    限制深度和节点数，免得一个畸形/超大 JSON 把解析拖死。
    浅层的先出，所以正常的 aweme_detail 还是第一个被拿到。
    """
    queue = [(root, 0)]
    seen = 0
    while queue:
        node, depth = queue.pop(0)
        if depth > max_depth or seen > max_nodes:
            return
        seen += 1
        if isinstance(node, dict):
            if depth and looks_like_aweme(node):
                yield node
            for v in node.values():
                if isinstance(v, (dict, list)):
                    queue.append((v, depth + 1))
        elif isinstance(node, list):
            for v in node[:200]:
                if isinstance(v, (dict, list)):
                    queue.append((v, depth + 1))


def find_aweme(data, target_id=""):
    """在响应里找作品本体，优先返回 target_id 匹配的那个。

    先看已知包装 key（快且准），没有再递归搜索（宽容）。
    找不到返回 None —— 交给调用方判断是「还没加载完」还是「被拦了」。
    """
    target_id = str(target_id or "")

    if isinstance(data, dict):
        for k in DETAIL_WRAPPER_KEYS:
            v = data.get(k)
            if looks_like_aweme(v):
                if not target_id or aweme_id_of(v) == target_id:
                    return v

    fallback = None
    for cand in iter_aweme_candidates(data):
        if not target_id or aweme_id_of(cand) == target_id:
            return cand
        if fallback is None:
            fallback = cand
    return fallback


def find_block_reason(data):
    """抖音拒绝返回作品时，把它的原话找出来。找不到返回空串。"""
    if not isinstance(data, dict):
        return ""
    for k in BLOCK_WRAPPER_KEYS:
        box = data.get(k)
        if isinstance(box, dict):
            for mk in BLOCK_MESSAGE_KEYS:
                v = box.get(mk)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def body_might_contain_aweme(body):
    """便宜的预筛：body 里连 id 字样都没有，就不值得 json.loads。"""
    if not body:
        return False
    return any(k in body for k in AWEME_ID_KEYS)
