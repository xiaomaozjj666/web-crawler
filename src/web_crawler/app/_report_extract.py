"""离线 HTML 重写、遮罩层剥离与数据/正文抽取。

从 :mod:`app.crawler_report` 拆出：``rewrite_html`` 把页面里的资源 URL
改写为本地相对路径，``strip_page_overlays`` 剔除遮罩层/弹窗/付费墙，
``smart_extract`` / ``write_extracted_data`` 抽取并落盘结构化元数据，
``extract_readable_text`` 用简单启发式抽取正文。

本模块只依赖 :mod:`app.crawler_models` 与 :mod:`app.crawler_net`。
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any

from web_crawler.app._fsutil import atomic_text_writer, atomic_write_text
from web_crawler.app._offline import RewriteTable
from web_crawler.app.crawler_models import ManifestRow
from web_crawler.parser import Selector
from web_crawler.parser.rules import parse_html

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")


def rewrite_html(html: str, resources: list[ManifestRow], page_url: str, output_dir: Path) -> str:
    """单页重写便捷入口（保持既有签名）；整次爬取请构建 :class:`RewriteTable` 复用。"""
    return RewriteTable(resources, output_dir).apply(html, page_url)


# ── 遮罩层/弹窗/模态框剥离 ────────────────────────────────────────

OVERLAY_PATTERNS = [
    # 遮罩层/模态框/弹窗/付费墙的常见 class/关键词模式
    (
        re.compile(
            r'<div[^>]*\bclass\s*=\s*["\'][^"\']*'
            r"(?:\bmodal\b|\boverlay\b|\bmask\b|\bshade\b|\bdialog\b"
            r"|\bpopup\b|\bpop-up\b|\blightbox\b|\bmodal-\b"
            r"|\bpaywall\b|\bsubscribe\b|\bsubscription\b"
            r"|\bnewsletter\b|\bpromo\b|\badvertisement\b|\bfloat-ad\b"
            r"|\binterstitial\b|\bfullscreen-[adio]"
            r"|\bcookie-banner\b|\bcookie-consent\b|\bnotice-bar\b"
            r"|\bmember-|\bvip-|\bvip_"
            r"|\blogin-box\b|\blogin-modal\b|\breg-modal\b"
            r"|\bguide-mask\b|\bguide-layer\b|\btip-overlay\b"
            r"|\bdownload-app\b|\bapp-download\b"
            r"|\btoast\b|\bnotice\b|\balert-\b"
            r"|\bvideo-ad\b|\bad-layer\b"
            r'|\bwx-\w+-modal\b)[^"\']*["\'][^>]*>'
            r".*?</div>",
            re.IGNORECASE | re.DOTALL,
        ),
        "div overlay/modal/popup",
    ),
    (
        re.compile(
            r'<div[^>]*\b(id|class)\s*=\s*["\'][^"\']*'
            r"(?:mask|overlay|shadeBg|shadowBg|bgShadow|modalBg|dialogBg)"
            r'[^"\']*["\'][^>]*>'
            r".*?</div>",
            re.IGNORECASE | re.DOTALL,
        ),
        "div mask BG",
    ),
    # fixed/sticky 定位的遮罩 div
    (
        re.compile(
            r'<div[^>]*\bstyle\s*=\s*["\'][^"\']*'
            r"(?:position\s*:\s*fixed|position\s*:\s*sticky)"
            r'[^"\']*["\'][^>]*'
            r">(?:<div[^>]*>.*?</div>\s*)*?</div>",
            re.IGNORECASE | re.DOTALL,
        ),
        "fixed/sticky div (dangerous, wide match)",
    ),
]


def strip_page_overlays(html: str, aggressive: bool = False) -> str:
    """移除页面标记中的遮罩层/弹窗/付费墙 HTML 元素。

    返回剔除已知遮罩元素后的 HTML。
    """
    result = html

    # 按模式移除
    for pattern, label in OVERLAY_PATTERNS:
        if not aggressive and "fixed/sticky" in label:
            continue  # 非激进模式跳过匹配过宽的正则
        result = pattern.sub("", result)

    # 按已知 ID 移除常见遮罩容器 —— 每个属性（id= / class=）各用一条编译好的
    # 交替正则，每个页面只扫描两次，而不是 2 * len(ids) ≈ 60 次。
    overlay_ids_alt = (
        "mask|overlay|shadow|shade|dialog|lightbox|modal|popup|subscribe|"
        "subscribe-box|signin|login|paywall|vip|member|register|download-app|"
        "cookie-notice|cookieConsent|announce|notice-bar|guide|guideLayer|"
        "tips|toast|alertBox|videoAd|playerAd|adContainer|floatingAd"
    )
    result = re.sub(
        rf'<div[^>]*\bid\s*=\s*["\']({overlay_ids_alt})["\'][^>]*>.*?</div>',
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result = re.sub(
        rf'<div[^>]*\bclass\s*=\s*["\'][^"\']*({overlay_ids_alt})[^"\']*["\'][^>]*>.*?</div>',
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return result


# ── 智能数据抽取 ────────────────────────────────────────────────

EXTRACTED_DATA_FIELDS = [
    "page_url",
    "page_title",
    "og_title",
    "og_description",
    "og_image",
    "og_video",
    "meta_description",
    "meta_keywords",
    "h1_count",
    "h2_count",
    "h3_count",
    "link_count",
    "image_count",
    "video_count",
    "text_length",
    "has_canonical",
]


def _extraction_selector(source: str | Selector) -> Selector:
    return parse_html(source) if isinstance(source, str) else source


def _visible_text(selector: Selector) -> str:
    parts = selector.element.xpath(
        ".//text()[not(ancestor::script or ancestor::style or ancestor::template)]",
        smart_strings=False,
    )
    return " ".join(" ".join(parts).split())


def smart_extract(html: str | Selector, page_url: str) -> dict[str, object]:
    """从页面 DOM 提取元数据和内容计数。"""
    selector = _extraction_selector(html)
    title = selector.css_first("title")
    metadata: dict[str, str] = {}
    for meta in selector.css("meta[content]"):
        attribute = meta.attr("property") or meta.attr("name") or ""
        metadata.setdefault(attribute.lower(), meta.attr("content", ""))

    counts: dict[str, int] = dict.fromkeys(("h1", "h2", "h3", "a", "img", "video"), 0)
    for element in selector.element.iter():
        if element.tag in counts:
            counts[element.tag] += 1
    return {
        "page_url": page_url,
        "page_title": _visible_text(title) if title is not None else "",
        "og_title": metadata.get("og:title", ""),
        "og_description": metadata.get("og:description", ""),
        "og_image": metadata.get("og:image", ""),
        "og_video": metadata.get("og:video", ""),
        "meta_description": metadata.get("description", metadata.get("og:description", "")),
        "meta_keywords": metadata.get("keywords", ""),
        "h1_count": counts["h1"],
        "h2_count": counts["h2"],
        "h3_count": counts["h3"],
        "link_count": counts["a"],
        "image_count": counts["img"],
        "video_count": counts["video"],
        "text_length": len(_visible_text(selector)),
        "has_canonical": any(
            "canonical" in relation.lower().split()
            for relation in selector.css("link[href]::attr(rel)")
        ),
    }


def write_extracted_data(output_dir: Path, data: list[dict[str, object]]) -> None:
    """把抽取数据写入 JSON 与 CSV（两者都原子写）。"""
    json_path = output_dir / "extracted_data.json"
    atomic_write_text(
        json_path,
        json.dumps(data, ensure_ascii=False, indent=2),
    )
    csv_path = output_dir / "extracted_data.csv"
    if data:
        with atomic_text_writer(csv_path, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
    _log.info("extracted data: %s, %s", json_path, csv_path)


def extract_readable_text(html: str | Selector) -> str:
    """优先提取 article、main 或常见正文容器的可见文本。

    与单行的 :func:`_visible_text` 不同，本函数保留块级结构：段落/标题/
    列表项各占一行，行内标签（a / strong / code …）不打断所在段落；同时
    剥离 script / style / nav / footer 等非正文骨架并去掉连续重复行。
    供 LLM 或人阅读的正文输入用这个，计数字段仍用 :func:`_visible_text`。

    正文容器不再盲目取第一个 ``<article>``：多个候选（全部 article +
    main/常见内容容器 + body）按可见文本量取最长者——2026-11 竞品调研
    实测 julius.ai 产品页第一个 article 只有 111 字，main 有 1416 字，
    盲取首 article 会把正文截掉 92%。
    """
    selector = _extraction_selector(html)

    candidates: list[Selector] = []
    candidates.extend(selector.css("article"))
    candidates.extend(
        selector.css(
            "main, #content, #article, #main-content, #post-content, "
            ".content, .post-content, .article-content, .entry-content, .main-content"
        )
    )
    body = selector.css_first("body")
    if body is not None:
        candidates.append(body)
    if not candidates:
        candidates.append(selector)

    # 候选优先级：article > main/内容容器 > body（candidates 顺序即优先级）。
    # 在「近峰值」（>= 90% 最大可见文本量）的候选里取优先级最高者：
    # - julius.ai 产品页：首 article 111 字、main 1416 字 → 取 main（盲取
    #   首 article 会截掉 92% 正文）；
    # - 无显式 <body> 的迷你页面：lxml 可能把 <article> 归进 <head>（解析器
    #   工件），article 与 body 文本量接近时仍优先 article，避免丢正文。
    volumes = [len(_visible_text(node)) for node in candidates]
    peak = max(volumes, default=0)
    threshold = peak * 0.9
    root = candidates[-1]
    for node, volume in zip(candidates, volumes, strict=False):
        if volume >= threshold:
            root = node
            break
    visible = _render_blocks(root.element)
    if len(visible) >= _EMBEDDED_MIN_VISIBLE:
        return visible
    # 可见文本极少（JS 壳/SSR 数据页）：正文可能在 <script type="application/
    # json"> 状态块里（Next.js __NEXT_DATA__、Nuxt __NUXT__、字节系
    # __MODERN_SERVER_DATA__ 等）。2026-11 实测 docs.trae.ai 页面 542KB
    # 可见文本 0 字，正文全在 __MODERN_SERVER_DATA__ 里。
    embedded = _embedded_json_text(selector)
    if len(embedded) >= 40 and len(embedded) > len(visible) + 20:
        return embedded
    return visible


# 可见文本低于该长度时尝试从内嵌 JSON 状态块兜底提取正文
_EMBEDDED_MIN_VISIBLE = 200
# 单个 script 状态块的读取上限（超长直接跳过，避免解析巨型 blob 拖慢后处理）
_MAX_EMBEDDED_SCRIPT_CHARS = 4 * 1024 * 1024
# 单条候选文本长度区间：太短是配置/标签，太长是代码块或序列化数据
_MIN_EMBEDDED_TEXT = 20
_MAX_EMBEDDED_TEXT = 20000
# 内嵌文本总量上限（防止超长状态块把正文文件撑爆）
_MAX_EMBEDDED_TOTAL = 200 * 1024


def _looks_like_prose(value: str) -> bool:
    """判断 JSON 字符串值像不像正文（而非 URL / 代码 / 配置值）。"""
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "data:", "/", "./")):
        return False
    if any(marker in value for marker in ("{", "}", "function ", "=>", "<div", "var ")):
        return False
    if len(value.split()) < 4 and not any("\u4e00" <= ch <= "\u9fff" for ch in value):
        return False
    meaningful = sum(
        1
        for ch in value
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" or ch in " ，。！？、；：,.!?;:'\"（）()"
    )
    return meaningful >= len(value) * 0.7


def _collect_prose(node: object, out: list[str], seen: set[str], depth: int = 0) -> None:
    """递归收集 JSON 树中的正文候选字符串（保持文档顺序，跳过去重）。

    SSR 状态树常见 15+ 层嵌套（Trae 的正文在 loaderData/layout/docDetail/
    content/children…/props/value/t[0] 之下），深度上限取 24；再深的值
    多为配置/埋点，放弃收集。
    """
    if depth > 24:
        return
    if isinstance(node, str):
        value = node.strip()
        if not (_MIN_EMBEDDED_TEXT <= len(value) <= _MAX_EMBEDDED_TEXT):
            return
        # 双重编码的 JSON 再解析一层（SSR 状态里常见）
        if value.startswith(("{", "[")):
            try:
                _collect_prose(json.loads(value), out, seen, depth + 1)
            except (json.JSONDecodeError, ValueError):
                pass
            return
        if value not in seen and _looks_like_prose(value):
            seen.add(value)
            out.append(value)
        return
    if isinstance(node, dict):
        _collect_delta_text(node, out, seen)
        for value in node.values():
            _collect_prose(value, out, seen, depth + 1)
    elif isinstance(node, list):
        # 裸 ops 数组（Quill delta 直接序列化成列表，外层没有 {"ops": ...}）
        if node and all(isinstance(item, dict) and "insert" in item for item in node):
            _collect_delta_text({"ops": node}, out, seen)
        for value in node:
            _collect_prose(value, out, seen, depth + 1)


def _collect_delta_text(node: dict, out: list[str], seen: set[str]) -> None:
    """Quill delta（``{"ops":[{"insert":"..."}]}``）按顺序拼接成正文行。

    富文本编辑器（Trae 文档等）把正文拆成大量短 insert 片段，单条远低于
    ``_MIN_EMBEDDED_TEXT``，走普通字符串过滤会被整体丢弃。delta 的
    ``insert`` 本身带 ``\\n`` 分段，按行还原即可。
    """
    ops = node.get("ops")
    if not isinstance(ops, list):
        return
    parts = [
        str(op.get("insert"))
        for op in ops
        if isinstance(op, dict) and isinstance(op.get("insert"), str)
    ]
    text = "".join(parts)
    if len(text) < _MIN_EMBEDDED_TEXT:
        return
    for line in text.splitlines():
        value = " ".join(line.split())
        if len(value) >= 2 and value not in seen:
            seen.add(value)
            out.append(value)


def _embedded_json_text(selector: Selector) -> str:
    """从内嵌 JSON 状态块提取正文候选文本。

    覆盖两类常见形态：``<script type="application/json">``（Next.js
    ``__NEXT_DATA__``、Nuxt ``__NUXT__``、``__MODERN_SERVER_DATA__``）与
    ``window.__INITIAL_STATE__ = {...}`` 这类 JS 赋值（React Router
    ``window._ROUTER_DATA``）。后者是 2026-11 实测 docs.trae.ai 的真实载体。
    """
    collected: list[str] = []
    seen: set[str] = set()
    for script in selector.css("script"):
        element = script.element
        if element.get("src"):
            continue
        raw = element.text or ""
        if not raw or len(raw) > _MAX_EMBEDDED_SCRIPT_CHARS:
            continue
        payload = _parse_state_blob(raw, (element.get("type") or "").lower())
        if payload is None:
            continue
        _collect_prose(payload, collected, seen)
        if sum(len(item) for item in collected) >= _MAX_EMBEDDED_TOTAL:
            break
    text = "\n".join(collected)
    return text[:_MAX_EMBEDDED_TOTAL]


# window.__INITIAL_STATE__ = {...} / window._ROUTER_DATA = {...}; 这类赋值
_STATE_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:window\.|globalThis\.)?[\w.$]+\s*=\s*(\{.*\}|\[.*\])\s*;?\s*$", re.DOTALL
)


def _parse_state_blob(raw: str, kind: str) -> object | None:
    """把 script 内容解析成 JSON 对象；不是状态块时返回 None。"""
    stripped = raw.lstrip()
    if "json" in kind or stripped.startswith(("{", "[")):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    match = _STATE_ASSIGNMENT_RE.match(raw)
    if match is None:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


# 正文渲染时整棵剪掉的骨架标签：脚本/样式/表单控件，以及页面级导航、
# 页眉页脚、侧栏（boilerplate）。这些标签里的文本在模板页逐页重复、
# 与正文无关——2026-11 竞品调研实测中它们是提取结果的主要噪音源。
_STRUCTURE_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "form",
        "button",
        "select",
        # 文档头不进正文：候选根落到整个文档（页面没有 article/main/body
        # 容器）时避免 <title>/<head> 文案混入正文
        "head",
        "title",
        "meta",
        "link",
    }
)
_FORM_INPUT_TAGS = frozenset({"input", "textarea", "option", "label"})
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


#: 中日韩表意文字区间（含扩展 A/B、兼容区、全角标点区）。用于行内拼接时
#: 决定是否需要空格：两个 CJK 字符之间补空格是**错误**的（中文没有词间空格）。
_CJK_RE = re.compile(
    r"[\u2e80-\u2eff\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\uff00-\uffef\U00020000-\U0002ebef]"
)

#: 全角/CJK 标点：两侧都不补空格（「，。」「（」等）。
_CJK_PUNCT = frozenset("，。、；：！？…—～·「」『』（）【】《》〈〉“”‘’")


def _is_cjk(char: str) -> bool:
    return bool(char) and bool(_CJK_RE.match(char))


def _inline_separator(left: str, right: str) -> str:
    """两个相邻行内文本片段之间是否需要分隔空格。

    规则来自中英排版惯例（2026-11 实测：旧实现无条件补空格会把
    「请先<strong>安装</strong>依赖」渲染成「请先 安装 依赖」，中文正文
    被系统性污染；而英文相邻链接「Related pageSecond link」又必须补空格）：

    * 任一侧是空白 → 保留一个空格（源码空白浏览器也会渲染成空格）；
    * 两个 CJK 字符之间 → 不补空格；
    * CJK 标点两侧 → 不补空格；
    * CJK 与拉丁/数字交界 → 补空格（中文排版惯例）；
    * 两侧都是 ASCII 字母数字 → 补空格；其余（句读、括号等）不补。
    """
    if not left or not right:
        return ""
    lchar, rchar = left[-1], right[0]
    if lchar.isspace() or rchar.isspace():
        return " "
    if lchar in _CJK_PUNCT or rchar in _CJK_PUNCT:
        return ""
    l_cjk, r_cjk = _is_cjk(lchar), _is_cjk(rchar)
    if l_cjk and r_cjk:
        return ""
    if l_cjk or r_cjk:
        return " "
    return " " if (lchar.isalnum() and rchar.isalnum()) else ""


def _join_inline_parts(parts: list[str]) -> str:
    """按 :func:`_inline_separator` 的规则拼接相邻行内文本片段。"""
    out = ""
    pending_space = False
    for part in parts:
        if not part:
            continue
        if not part.strip():
            pending_space = pending_space or bool(out)
            continue
        if part[:1].isspace():
            pending_space = pending_space or bool(out)
        stripped = part.strip()
        if not out:
            out = stripped
        else:
            separator = " " if pending_space else _inline_separator(out, stripped)
            out += separator + stripped
        pending_space = part[-1:].isspace()
    return out


def _render_blocks(element: Any) -> str:
    """把 DOM 子树渲染成一行一块的正文文本（块级标签换行，行内不打断）。"""
    lines: list[str] = []

    def emit(value: object) -> None:
        text = " ".join(str(value or "").split())
        if text:
            lines.append(text)

    def has_block_child(node: Any) -> bool:
        return any(
            isinstance(child.tag, str) and child.tag.lower() in _BLOCK_TAGS for child in node
        )

    def leaf_text(node: Any) -> str:
        """叶子节点整棵子树的文本合并为一行；相邻行内元素之间按字符类补空格。

        ``"".join(itertext())`` 会把相邻行内元素（如连续 <a>）的文本无分隔
        拼在一起（实测 "Related pageSecond link"），但**无条件**补空格又会
        把中文正文拆开（实测「请先 安装 依赖 ，然后 运行 脚本。」）。这里按
        字符类决定分隔符，见 :func:`_inline_separator`。
        """
        parts: list[str] = []
        if node.text:
            parts.append(node.text)
        for child in node:
            if isinstance(child.tag, str) and child.tag.lower() in (
                _STRUCTURE_TAGS | _FORM_INPUT_TAGS | _CHROME_TAGS
            ):
                if child.tail:
                    parts.append(child.tail)
                continue
            parts.append(leaf_text(child))
            if child.tail:
                parts.append(child.tail)
        return _join_inline_parts(parts)

    def emit_pre(node: Any) -> None:
        """<pre> 保留换行与缩进（代码块/终端输出），只清行尾空白与首尾空行。"""
        raw = "".join(node.itertext())
        if not raw:
            return
        raw_lines = [line.rstrip() for line in raw.replace("\r\n", "\n").split("\n")]
        while raw_lines and not raw_lines[0].strip():
            raw_lines.pop(0)
        while raw_lines and not raw_lines[-1].strip():
            raw_lines.pop()
        if raw_lines:
            lines.append("\n".join(raw_lines))

    def emit_row(node: Any) -> None:
        """表格行：单元格文本用 " | " 连成一行，保留行列关系。"""
        cells: list[str] = []
        for cell in node:
            if not isinstance(cell.tag, str) or cell.tag.lower() not in ("td", "th"):
                continue
            text = " ".join("".join(cell.itertext()).split())
            if text:
                cells.append(text)
        row = " | ".join(cells)
        if row:
            lines.append(row)

    def walk(node: Any) -> None:
        tag = node.tag if isinstance(node.tag, str) else ""
        if not tag:
            return
        lowered = tag.lower()
        if lowered in _STRUCTURE_TAGS or lowered in _FORM_INPUT_TAGS or lowered in _CHROME_TAGS:
            return
        if lowered == "pre":
            emit_pre(node)
            return
        if lowered == "tr":
            emit_row(node)
            return
        if has_block_child(node):
            # 容器节点：块间散落的裸文本单独成行，再逐个下钻子节点。
            # 行内元素（lxml 自动包 body 后的 <a> 等）也可能是容器直接子节点。
            emit(node.text)
            for child in node:
                walk(child)
                emit(child.tail)
            return
        # 叶子节点（块级或行内）：整棵子树的文本合并为一行
        emit(leaf_text(node))

    walk(element)
    # 连续重复行去重：模板页的导航/版权文案会逐区块重复出现
    result: list[str] = []
    for line in lines:
        if line and result and result[-1] == line:
            continue
        result.append(line)
    return "\n".join(result)
