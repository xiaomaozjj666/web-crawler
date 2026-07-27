"""验证码检测/处理模块测试（mock page，不启动浏览器）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from web_crawler.ai.captcha import (
    _HCAPTCHA_IFRAME,
    CaptchaDetector,
    CaptchaInfo,
    CaptchaManager,
    CaptchaType,
)


def test_captcha_type_values() -> None:
    expected = {
        "hcaptcha",
        "turnstile",
        "recaptcha_v2",
        "recaptcha_v3",
        "geetest",
        "unknown",
        "none",
    }
    assert {t.value for t in CaptchaType} == expected


def test_captcha_info_defaults() -> None:
    info = CaptchaInfo(type=CaptchaType.NONE)
    assert info.type is CaptchaType.NONE
    assert info.iframe_url is None
    assert info.site_key is None
    assert info.container_selector is None
    assert info.detected_at > 0


def _make_page(selector_map: dict) -> MagicMock:
    """构造按 selector 返回预设元素（或 None）的 mock page。"""
    page = MagicMock()

    def query(sel: str):
        return selector_map.get(sel)

    page.query_selector.side_effect = query
    return page


def test_detector_no_captcha() -> None:
    # 页面上没有任何验证码特征元素
    page = _make_page({})
    assert CaptchaDetector().detect(page) is None


def test_detector_hcaptcha() -> None:
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://hcaptcha.com/?sitekey=abc123"
    page = _make_page({_HCAPTCHA_IFRAME: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.HCAPTCHA
    assert info.iframe_url == "https://hcaptcha.com/?sitekey=abc123"
    assert info.site_key == "abc123"
    assert info.container_selector == _HCAPTCHA_IFRAME


def test_captcha_manager_no_captcha() -> None:
    # 无验证码时 manager.handle 直接返回 True
    page = _make_page({})
    assert CaptchaManager().handle(page) is True
