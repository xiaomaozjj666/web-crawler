"""自适应抓取引擎：元素指纹 + 相似度匹配。

这是 Scrapling 的招牌特性。网站标记变化时，硬编码的 CSS/XPath 选择器会
失效。``adaptive=True`` 不需要重写抓取器，而是取出先前存储的元素指纹，
按结构相似度重新定位元素——完全不需要 AI。
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from lxml import etree

from .storage import AdaptiveStorage

# 把各字段相似度聚合为单一得分时使用的权重。
_WEIGHTS: dict[str, float] = {
    "tag": 2.0,
    "text_sample": 2.5,
    "attrs": 1.5,
    "class_tokens": 1.5,
    "child_tags": 1.2,
    "sibling_tags": 0.8,
    "path_signature": 1.5,
    "depth": 0.5,
}

_TEXT_RE = re.compile(r"\s+")


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _TEXT_RE.sub(" ", value).strip()[:200]


def _class_tokens(class_attr: str | None) -> list[str]:
    if not class_attr:
        return []
    return sorted({tok for tok in class_attr.split() if tok})


def _path_signature(element: etree._Element) -> str:
    """仅含标签的祖先路径，如 ``div>section>article``。"""
    parts: list[str] = []
    node: etree._Element | None = element
    while node is not None and isinstance(node.tag, str):
        parts.append(node.tag)
        node = node.getparent()
    parts.reverse()
    return ">".join(parts)


def compute_fingerprint(element: etree._Element) -> str:
    """为 ``element`` 计算可 JSON 序列化的结构指纹。"""
    attribs = {k: v for k, v in element.attrib.items()}
    class_attr = attribs.get("class", "")
    text_sample = _normalize_text("".join(element.itertext()))
    child_tags = [c.tag for c in element if isinstance(c.tag, str)]
    sibling_tags: list[str] = []
    parent = element.getparent()
    if parent is not None:
        sibling_tags = [c.tag for c in parent if isinstance(c.tag, str)]
    depth = 0
    node: etree._Element | None = element
    while node is not None and isinstance(node.tag, str):
        depth += 1
        node = node.getparent()

    fingerprint = {
        "tag": element.tag if isinstance(element.tag, str) else "",
        "text_sample": text_sample,
        "attrs": dict(sorted(attribs.items())),
        "class_tokens": _class_tokens(class_attr),
        "child_tags": child_tags,
        "sibling_tags": sibling_tags,
        "path_signature": _path_signature(element),
        "depth": depth,
    }
    return json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)


def _ratio(a: Any, b: Any) -> float:
    """字符串的 SequenceMatcher 比率；列表/字符串相等时为 1.0。"""
    if isinstance(a, str) and isinstance(b, str):
        if not a and not b:
            return 1.0
        return SequenceMatcher(None, a, b).ratio()
    if isinstance(a, list) and isinstance(b, list):
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a, b).ratio()
    return 1.0 if a == b else 0.0


def similarity_score(fp_a: str, fp_b: str) -> float:
    """返回两个指纹 JSON 字符串之间 0..1 的相似度得分。"""
    try:
        a = json.loads(fp_a)
        b = json.loads(fp_b)
    except (json.JSONDecodeError, TypeError):
        return 0.0

    total_weight = 0.0
    acc = 0.0
    for key, weight in _WEIGHTS.items():
        total_weight += weight
        va = a.get(key)
        vb = b.get(key)
        if key == "attrs":
            # 属性名与 class 分开比较，粒度更细。
            keys_a = sorted(a.get("attrs", {}).keys())
            keys_b = sorted(b.get("attrs", {}).keys())
            acc += weight * _ratio(keys_a, keys_b)
        elif key == "class_tokens":
            acc += weight * _ratio(va, vb)
        else:
            acc += weight * _ratio(va, vb)
    if total_weight == 0:  # pragma: no cover - _WEIGHTS 恒非空，total_weight 不可能为 0
        return 0.0
    return acc / total_weight


def best_match(
    candidates: list[etree._Element],
    stored_fp: str,
    threshold: float = 0.5,
) -> tuple[etree._Element | None, float]:
    """从 ``candidates`` 中返回 ``(最佳元素, 得分)``，无匹配时 ``(None, 0)``。"""
    best: etree._Element | None = None
    best_score = 0.0
    for cand in candidates:
        score = similarity_score(compute_fingerprint(cand), stored_fp)
        if score > best_score:
            best_score = score
            best = cand
    if best_score < threshold:
        return None, best_score
    return best, best_score


__all__ = [
    "AdaptiveStorage",
    "best_match",
    "compute_fingerprint",
    "similarity_score",
]
