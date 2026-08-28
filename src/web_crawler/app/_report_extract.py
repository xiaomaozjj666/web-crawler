"""离线 HTML 重写、遮罩层剥离与数据/正文抽取。

从 :mod:`app.crawler_report` 拆出：``rewrite_html`` 把页面里的资源 URL
改写为本地相对路径，``strip_page_overlays`` 剔除遮罩层/弹窗/付费墙，
``smart_extract`` / ``write_extracted_data`` 抽取并落盘结构化元数据，
``extract_readable_text`` 用简单启发式抽取正文。

本模块只依赖 :mod:`app.crawler_models` 与 :mod:`app.crawler_net`。
"""

from __future__ import annotations

import csv
import html as html_lib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from web_crawler.app.crawler_models import ManifestRow
from web_crawler.app.crawler_net import extract_title, same_domain

# 与 app.crawler 共用同一个 logger：UI 通过 attach_log_handler 挂到
# "crawler" logger 的 handler 对所有模块日志生效，行为与拆分前一致。
_log = logging.getLogger("crawler")


def rewrite_html(html: str, resources: list[ManifestRow], page_url: str, output_dir: Path) -> str:
    replacements = {
        row.url: Path(row.saved_path).resolve().relative_to(output_dir).as_posix()
        for row in resources
        if row.status == "ok" and row.saved_path
    }
    rewritten = html
    for original_url, local_path in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        rewritten = rewritten.replace(original_url, local_path)
        parsed = urlparse(original_url)
        if same_domain(original_url, page_url):
            rewritten = rewritten.replace(parsed.path, local_path)
    return rewritten


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


def smart_extract(html: str, page_url: str) -> dict[str, object]:
    """自动从页面抽取结构化数据。

    返回含常见元数据字段的 dict；无需配置即可用于任意 HTML 页面。
    """
    result: dict[str, object] = {
        "page_url": page_url,
        "page_title": extract_title(html),
    }

    # Open Graph / Twitter / Meta 标签
    og_patterns = {
        "og_title": (r'<meta\s+property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']', False),
        "og_description": (
            r'<meta\s+property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']',
            False,
        ),
        "og_image": (r'<meta\s+property=["\']og:image["\'][^>]*content=["\']([^"\']*)["\']', False),
        "og_video": (
            r'<meta\s+property=["\']og:video["\'](?:[^>]*content=["\']([^"\']*)["\'])',
            False,
        ),
    }
    for key, (pattern, _) in og_patterns.items():
        match = re.search(pattern, html, re.IGNORECASE)
        result[key] = match.group(1) if match else ""

    # Meta description（回退：og:description）
    md = re.search(
        r'<meta\s+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']', html, re.IGNORECASE
    )
    result["meta_description"] = md.group(1) if md else result.get("og_description", "")

    # Meta 关键词
    mk = re.search(
        r'<meta\s+name=["\']keywords["\'][^>]*content=["\']([^"\']*)["\']', html, re.IGNORECASE
    )
    result["meta_keywords"] = mk.group(1) if mk else ""

    # 标题计数
    result["h1_count"] = len(re.findall(r"<h1[>\s]", html, re.IGNORECASE))
    result["h2_count"] = len(re.findall(r"<h2[>\s]", html, re.IGNORECASE))
    result["h3_count"] = len(re.findall(r"<h3[>\s]", html, re.IGNORECASE))

    # 链接/图片/视频计数
    result["link_count"] = len(re.findall(r"<a\s+", html, re.IGNORECASE))
    result["image_count"] = len(re.findall(r"<img\s+", html, re.IGNORECASE))
    result["video_count"] = len(re.findall(r"<video\s+", html, re.IGNORECASE))

    # 文本长度（近似值）
    text_stripped = re.sub(r"<[^>]+>", "", html)
    text_stripped = re.sub(r"\s+", " ", text_stripped).strip()
    result["text_length"] = len(text_stripped)

    # 规范链接（canonical URL）
    canonical = re.search(
        r'<link\s+rel=["\']canonical["\'][^>]*href=["\']([^"\']*)["\']', html, re.IGNORECASE
    )
    result["has_canonical"] = bool(canonical)

    return result


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


def extract_readable_text(html: str) -> str:
    """用简单启发式从 HTML 抽取可读的正文/主要文本。"""
    # 优先尝试 <article>
    m = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL | re.IGNORECASE)
    if m:
        html = m.group(1)
    else:
        # 尝试常见内容容器
        for selector in (
            'id="content"',
            'id="article"',
            'id="main-content"',
            'id="post-content"',
            'class="content"',
            'class="post-content"',
            'class="article-content"',
            'class="entry-content"',
            'class="main-content"',
        ):
            pattern = rf"<div[^>]*{selector}[^>]*>(.*?)</div>"
            m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if m:
                html = m.group(1)
                break
        else:
            # 兜底：<body>
            m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
            if m:
                html = m.group(1)

    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # 去掉过短的行（多为导航/模板文字）
    lines = [line.strip() for line in text.split("\n") if len(line.strip()) > 30]
    return "\n".join(lines) if lines else text
