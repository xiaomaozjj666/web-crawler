"""Scrapling 风格的自适应解析子包。"""

from __future__ import annotations

from .adaptive import AdaptiveStorage, compute_fingerprint, similarity_score
from .selector import Adaptor, Adaptors, Selector

__all__ = [
    "AdaptiveStorage",
    "Adaptor",
    "Adaptors",
    "Selector",
    "compute_fingerprint",
    "similarity_score",
]
