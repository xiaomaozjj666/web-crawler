"""Tests for the CamoufoxFetcher: graceful degrade when camoufox is absent."""

from __future__ import annotations

import pytest


def test_camoufox_fetcher_requires_camoufox() -> None:
    from web_crawler import CamoufoxFetcher
    from web_crawler.compat import HAS_CAMOUFOX

    if HAS_CAMOUFOX:
        # Only build launch kwargs; do not actually launch a browser in CI.
        f = CamoufoxFetcher(os="windows", humanize=True, geoip=True, block_webrtc=True)
        kwargs = f._launch_kwargs()
        assert kwargs["os"] == "windows"
        assert kwargs["humanize"] is True
        assert kwargs["geoip"] is True
        assert kwargs["block_webrtc"] is True
    else:
        with pytest.raises(ImportError, match="camoufox"):
            CamoufoxFetcher()
