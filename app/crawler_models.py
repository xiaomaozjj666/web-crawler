"""Shared data models for the web resource crawler.

Defines the two dataclasses used across the split crawler modules:

- :class:`Resource` — a resource discovered while scanning a page.
- :class:`ManifestRow` — one row of the downloadable-resource manifest.

Both classes were originally defined in :mod:`app.crawler`; they live here so
that the network/parsing module (:mod:`app.crawler_net`) and the report module
(:mod:`app.crawler_report`) can construct and annotate them without importing
``app.crawler`` (which would create a circular import).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    url: str
    found_in: str
    kind: str
    page_url: str


@dataclass
class ManifestRow:
    status: str
    url: str
    saved_path: str
    content_type: str
    bytes: int
    category: str
    found_in: str
    kind: str
    page_url: str
    page_title: str
    diagnostic: str
    sha256: str = ""
