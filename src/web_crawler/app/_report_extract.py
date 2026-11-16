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
    """把抽取数据写入 JSON 与 CSV。"""
    json_path = output_dir / "extracted_data.json"
    json_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    csv_path = output_dir / "extracted_data.csv"
    if data:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)
    _log.info("extracted data: %s, %s", json_path, csv_path)


def extract_readable_text(html: str | Selector) -> str:
    """优先提取 article、main 或常见正文容器的可见文本。"""
    selector = _extraction_selector(html)
    article = selector.css_first("article")
    main = selector.css_first(
        "main, #content, #article, #main-content, #post-content, "
        ".content, .post-content, .article-content, .entry-content, .main-content"
    )
    body = selector.css_first("body")
    return _visible_text(article or main or body or selector)
