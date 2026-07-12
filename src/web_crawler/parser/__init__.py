"""Scrapling-style adaptive parser subpackage."""

from __future__ import annotations

from .adaptive import AdaptiveStorage, compute_fingerprint, similarity_score
from .selector import Adaptor, Adaptors, Selector

__all__ = [
    "Selector",
    "Adaptor",
    "Adaptors",
    "AdaptiveStorage",
    "compute_fingerprint",
    "similarity_score",
]
