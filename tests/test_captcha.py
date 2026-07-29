"""验证码检测/处理模块测试（mock page，不启动浏览器）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from web_crawler.ai.captcha import (
    _GEETEST_SELECTORS,
    _HCAPTCHA_IFRAME,
    _RECAPTCHA_IFRAME,
    _TURNSTILE_IFRAMES,
    CaptchaDetector,
    CaptchaInfo,
    CaptchaManager,
    CaptchaSolver,
    CaptchaType,
    _first_query,
    _sitekey_from_url,
)
from web_crawler.ai.image_captcha import ClickSolution, ImageCaptchaSolver


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


# ---------------------------------------------------------------------------
# ImageCaptchaSolver 集成
# ---------------------------------------------------------------------------


def test_captcha_solver_accepts_image_solver() -> None:
    img_solver = ImageCaptchaSolver(provider=None)
    solver = CaptchaSolver(image_solver=img_solver)
    assert solver.image_solver is img_solver


def test_captcha_solver_default_no_image_solver() -> None:
    solver = CaptchaSolver()
    assert solver.image_solver is None


def test_captcha_manager_injects_image_solver_to_default_solver() -> None:
    img_solver = ImageCaptchaSolver(provider=None)
    manager = CaptchaManager(image_solver=img_solver)
    assert manager.solver.image_solver is img_solver
    assert manager.image_solver is img_solver


def test_captcha_manager_injects_image_solver_to_custom_solver() -> None:
    img_solver = ImageCaptchaSolver(provider=None)
    custom_solver = CaptchaSolver(max_wait=10.0)
    manager = CaptchaManager(solver=custom_solver, image_solver=img_solver)
    assert manager.solver is custom_solver
    assert custom_solver.image_solver is img_solver


def test_captcha_manager_no_image_solver_default() -> None:
    manager = CaptchaManager()
    assert manager.solver.image_solver is None
    assert manager.image_solver is None


def test_solve_image_challenge_no_image_solver_returns_false() -> None:
    solver = CaptchaSolver(image_solver=None)
    page = MagicMock()
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_no_iframe_returns_false() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    page = _make_page({})  # 没有 iframe
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_no_bounding_box_returns_false() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    iframe = MagicMock()
    iframe.bounding_box.return_value = None
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_screenshot_failure_returns_false() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 0, "y": 0, "width": 100, "height": 100}
    iframe.screenshot.side_effect = RuntimeError("snapshot failed")
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_solve_click_returns_none_returns_false() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_click.return_value = None
    solver = CaptchaSolver(image_solver=img_solver)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 50, "y": 50, "width": 300, "height": 300}
    iframe.screenshot.return_value = b"fake-png"
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_solve_click_exception_returns_false() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_click.side_effect = RuntimeError("llm error")
    solver = CaptchaSolver(image_solver=img_solver)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 0, "y": 0, "width": 100, "height": 100}
    iframe.screenshot.return_value = b"fake-png"
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_clicks_points_and_returns_true() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_click.return_value = ClickSolution(
        points=[(10, 20), (30, 40)],
        labels=["A", "B"],
        method="llm",
    )
    solver = CaptchaSolver(image_solver=img_solver, humanize=False)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 100, "y": 200, "width": 300, "height": 300}
    iframe.screenshot.return_value = b"fake-png"
    # frame_locator 调用链用于 _read_challenge_prompt / _submit_challenge
    frame_locator = MagicMock()
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    page.frame_locator.return_value = frame_locator
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)

    result = solver._solve_image_challenge(page, info)

    assert result is True
    # 验证 solve_click 被调用，参数包含截图 bytes 和提示文字
    assert img_solver.solve_click.called
    call_args = img_solver.solve_click.call_args
    assert call_args.args[0] == b"fake-png"
    # 验证点击坐标按 LLM 给出的顺序触发，且加上了 iframe 在视口中的偏移
    expected_clicks = [(110, 220), (130, 240)]  # offset (100,200) + point
    actual_clicks = [call.args for call in page.mouse.click.call_args_list]
    assert actual_clicks == expected_clicks


def test_solve_image_challenge_humanize_adds_delay() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_click.return_value = ClickSolution(points=[(5, 5)])
    # humanize=True 会触发 time.sleep，但不应影响结果
    solver = CaptchaSolver(image_solver=img_solver, humanize=True)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 0, "y": 0, "width": 100, "height": 100}
    iframe.screenshot.return_value = b"png"
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    with patch("web_crawler.ai.captcha.time.sleep"):  # 不真实等待
        result = solver._solve_image_challenge(page, info)
    assert result is True


def test_geetest_detect_offset_no_image_solver_returns_none() -> None:
    solver = CaptchaSolver(image_solver=None)
    page = _make_page({})
    assert solver._geetest_detect_offset(page) is None


def test_geetest_detect_offset_panel_screenshot_failure_returns_none() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    panel = MagicMock()
    panel.screenshot.side_effect = RuntimeError("snap fail")
    page = _make_page({".geetest_panel": panel})
    assert solver._geetest_detect_offset(page) is None


def test_geetest_detect_offset_no_slider_element_returns_none() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    panel = MagicMock()
    panel.screenshot.return_value = b"bg"
    page = _make_page({".geetest_panel": panel})  # 缺 slider_button
    assert solver._geetest_detect_offset(page) is None


def test_geetest_detect_offset_returns_solver_x() -> None:
    from web_crawler.ai.image_captcha import SliderSolution

    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_slider.return_value = SliderSolution(x=150, method="pillow")
    solver = CaptchaSolver(image_solver=img_solver)
    panel = MagicMock()
    panel.screenshot.return_value = b"bg"
    slider_el = MagicMock()
    slider_el.screenshot.return_value = b"slider"
    page = _make_page(
        {
            ".geetest_panel": panel,
            ".geetest_slider_button": slider_el,
        }
    )
    result = solver._geetest_detect_offset(page)
    assert result == 150.0


def test_geetest_detect_offset_solver_returns_none() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_slider.return_value = None
    solver = CaptchaSolver(image_solver=img_solver)
    panel = MagicMock()
    panel.screenshot.return_value = b"bg"
    slider_el = MagicMock()
    slider_el.screenshot.return_value = b"slider"
    page = _make_page(
        {
            ".geetest_panel": panel,
            ".geetest_slider_button": slider_el,
        }
    )
    assert solver._geetest_detect_offset(page) is None


# ===========================================================================
# 扩展：辅助函数 _first_query / _sitekey_from_url
# ===========================================================================


def test_first_query_returns_first_hit() -> None:
    """命中第一个选择器即返回。"""
    el = MagicMock()
    page = _make_page({"a": el, "b": MagicMock()})
    assert _first_query(page, ("a", "b")) is el


def test_first_query_skips_none_and_returns_later_hit() -> None:
    """前序选择器返回 None 时继续向后查询。"""
    el = MagicMock()
    page = _make_page({"a": None, "b": el})
    assert _first_query(page, ("a", "b")) is el


def test_first_query_all_miss_returns_none() -> None:
    page = _make_page({})
    assert _first_query(page, ("a", "b")) is None


def test_first_query_exception_treated_as_none() -> None:
    """query_selector 抛错时视为 None，继续后续选择器。"""
    el = MagicMock()
    page = MagicMock()

    def query(sel: str):
        if sel == "a":
            raise RuntimeError("boom")
        return el

    page.query_selector.side_effect = query
    assert _first_query(page, ("a", "b")) is el


def test_first_query_all_raise_returns_none() -> None:
    page = MagicMock()
    page.query_selector.side_effect = RuntimeError("boom")
    assert _first_query(page, ("a", "b")) is None


def test_sitekey_from_url_empty_returns_none() -> None:
    assert _sitekey_from_url("", "sitekey") is None


def test_sitekey_from_url_none_url_returns_none() -> None:
    assert _sitekey_from_url(None, "sitekey") is None  # type: ignore[arg-type]


def test_sitekey_from_url_finds_key() -> None:
    url = "https://hcaptcha.com/?sitekey=abc123&other=1"
    assert _sitekey_from_url(url, "sitekey") == "abc123"


def test_sitekey_from_url_falls_through_multiple_keys() -> None:
    """首个 key 不存在时继续尝试后续 key。"""
    url = "https://recaptcha.net/?foo=bar&k=SITEKEY_X"
    assert _sitekey_from_url(url, "sitekey", "k") == "SITEKEY_X"


def test_sitekey_from_url_no_match_returns_none() -> None:
    url = "https://example.com/?foo=bar"
    assert _sitekey_from_url(url, "sitekey") is None


def test_sitekey_from_url_empty_value_skipped() -> None:
    """空值参数被跳过。"""
    url = "https://x.com/?sitekey=&k=REAL"
    assert _sitekey_from_url(url, "sitekey", "k") == "REAL"


# ===========================================================================
# 扩展：CaptchaDetector 各分支
# ===========================================================================


def test_detector_hcaptcha_with_data_sitekey_element() -> None:
    """data-sitekey 元素优先于 URL 解析。"""
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://hcaptcha.com/?sitekey=url_key"
    sitekey_el = MagicMock()
    sitekey_el.get_attribute.return_value = "dom_key"
    page = _make_page({_HCAPTCHA_IFRAME: iframe, "[data-sitekey]": sitekey_el})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.HCAPTCHA
    assert info.site_key == "dom_key"


def test_detector_hcaptcha_empty_src_yields_none_iframe_url() -> None:
    """iframe src 为空时 iframe_url 应为 None。"""
    iframe = MagicMock()
    iframe.get_attribute.return_value = ""
    page = _make_page({_HCAPTCHA_IFRAME: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.iframe_url is None
    assert info.site_key is None


def test_detector_turnstile_first_selector() -> None:
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://challenges.cloudflare.com/?x=1"
    page = _make_page({_TURNSTILE_IFRAMES[0]: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.TURNSTILE
    assert info.container_selector == _TURNSTILE_IFRAMES[0]
    assert info.iframe_url == "https://challenges.cloudflare.com/?x=1"


def test_detector_turnstile_second_selector() -> None:
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://turnstile.example/?sitekey=ts_key"
    page = _make_page({_TURNSTILE_IFRAMES[1]: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.TURNSTILE
    assert info.container_selector == _TURNSTILE_IFRAMES[1]
    assert info.site_key == "ts_key"


def test_detector_turnstile_query_exception_falls_through() -> None:
    """第一个选择器查询抛错时应继续尝试第二个。"""
    iframe = MagicMock()
    iframe.get_attribute.return_value = ""
    page = MagicMock()

    def query(sel: str):
        if sel == _TURNSTILE_IFRAMES[0]:
            raise RuntimeError("boom")
        if sel == _TURNSTILE_IFRAMES[1]:
            return iframe
        return None

    page.query_selector.side_effect = query

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.TURNSTILE


def test_detector_recaptcha_v2_anchor_path() -> None:
    """含 /anchor 的 iframe → reCAPTCHA v2。"""
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://recaptcha.google.com/recaptcha/api2/anchor?k=v2key"
    page = _make_page({_RECAPTCHA_IFRAME: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.RECAPTCHA_V2
    assert info.site_key == "v2key"


def test_detector_recaptcha_v2_with_data_sitekey() -> None:
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://recaptcha.google.com/anchor"
    sitekey_el = MagicMock()
    sitekey_el.get_attribute.return_value = "dom_v2"
    page = _make_page({_RECAPTCHA_IFRAME: iframe, "[data-sitekey]": sitekey_el})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.RECAPTCHA_V2
    assert info.site_key == "dom_v2"


def test_detector_recaptcha_v3_enterprise_path() -> None:
    """含 /enterprise/ 且不含 /anchor → reCAPTCHA v3。"""
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://recaptcha.net/recaptcha/enterprise.js?render=v3key"
    page = _make_page({_RECAPTCHA_IFRAME: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.RECAPTCHA_V3
    assert info.site_key == "v3key"


def test_detector_recaptcha_v3_render_param() -> None:
    """render=<key>（非 explicit）→ reCAPTCHA v3。"""
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://google.com/recaptcha/api.js?render=v3render"
    page = _make_page({_RECAPTCHA_IFRAME: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.RECAPTCHA_V3
    assert info.site_key == "v3render"


def test_detector_recaptcha_v3_render_explicit_is_v2() -> None:
    """render=explicit 不视为 v3（走 v2 路径）。"""
    iframe = MagicMock()
    iframe.get_attribute.return_value = "https://google.com/recaptcha/api.js?render=explicit"
    page = _make_page({_RECAPTCHA_IFRAME: iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.RECAPTCHA_V2


def test_detector_geetest_each_selector() -> None:
    """三种极验选择器任一命中即识别为 GEETEST。"""
    for sel in _GEETEST_SELECTORS:
        el = MagicMock()
        page = _make_page({sel: el})
        info = CaptchaDetector().detect(page)
        assert info is not None
        assert info.type is CaptchaType.GEETEST
        assert info.container_selector == sel


def test_detector_geetest_query_exception_falls_through() -> None:
    """极验首个选择器抛错时继续后续选择器。"""
    el = MagicMock()
    page = MagicMock()

    def query(sel: str):
        if sel == _GEETEST_SELECTORS[0]:
            raise RuntimeError("boom")
        if sel == _GEETEST_SELECTORS[1]:
            return el
        return None

    page.query_selector.side_effect = query
    info = CaptchaDetector().detect(page)
    assert info is not None
    assert info.type is CaptchaType.GEETEST


def test_detector_read_data_sitekey_no_element() -> None:
    page = _make_page({})
    assert CaptchaDetector._read_data_sitekey(page) is None


def test_detector_read_data_sitekey_exception_returns_none() -> None:
    page = MagicMock()
    page.query_selector.side_effect = RuntimeError("boom")
    assert CaptchaDetector._read_data_sitekey(page) is None


def test_detector_priority_hcaptcha_before_turnstile() -> None:
    """同时存在 hcaptcha 与 turnstile 时优先返回 hcaptcha。"""
    hcaptcha_iframe = MagicMock()
    hcaptcha_iframe.get_attribute.return_value = "https://hcaptcha.com/?sitekey=h"
    turnstile_iframe = MagicMock()
    turnstile_iframe.get_attribute.return_value = "https://challenges.cloudflare.com/"
    page = _make_page({_HCAPTCHA_IFRAME: hcaptcha_iframe, _TURNSTILE_IFRAMES[0]: turnstile_iframe})

    info = CaptchaDetector().detect(page)

    assert info is not None
    assert info.type is CaptchaType.HCAPTCHA


# ===========================================================================
# 扩展：CaptchaSolver.solve 调度
# ===========================================================================


def test_solve_none_type_returns_true() -> None:
    solver = CaptchaSolver()
    info = CaptchaInfo(type=CaptchaType.NONE)
    assert solver.solve(MagicMock(), info) is True


def test_solve_unknown_type_returns_false() -> None:
    solver = CaptchaSolver()
    info = CaptchaInfo(type=CaptchaType.UNKNOWN)
    assert solver.solve(MagicMock(), info) is False


def test_solve_handler_exception_returns_false() -> None:
    """处理器抛错时 solve 捕获并返回 False。"""
    solver = CaptchaSolver()
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    with patch.object(solver, "_solve_turnstile", side_effect=RuntimeError("boom")):
        assert solver.solve(MagicMock(), info) is False


def test_solve_dispatches_turnstile() -> None:
    solver = CaptchaSolver()
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    with patch.object(solver, "_solve_turnstile", return_value=True) as mock_fn:
        assert solver.solve(MagicMock(), info) is True
    mock_fn.assert_called_once()


# ===========================================================================
# 扩展：_solve_turnstile
# ===========================================================================


def test_solve_turnstile_auto_token_pass() -> None:
    """首次等待 token 即通过，无需点击。"""
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    with patch.object(solver, "_wait_for_token", return_value=True) as mock_wait:
        result = solver._solve_turnstile(MagicMock(), info)
    assert result is True
    assert mock_wait.call_count == 1


def test_solve_turnstile_click_then_token_pass() -> None:
    """首次失败 → 点击复选框 → 二次等待通过。"""
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    checkbox = MagicMock()
    checkbox.bounding_box.return_value = {"x": 0, "y": 0, "width": 30, "height": 30}
    page = _make_page({'input[type="checkbox"][name="cf-turnstile-response"]': checkbox})
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    with patch.object(solver, "_wait_for_token", side_effect=[False, True]):
        result = solver._solve_turnstile(page, info)
    assert result is True
    page.mouse.click.assert_called_once()


def test_solve_turnstile_no_checkbox_token_fail_returns_false() -> None:
    """无复选框 + token 失败 → False。"""
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    page = _make_page({})  # 无 checkbox
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    with patch.object(solver, "_wait_for_token", return_value=False):
        result = solver._solve_turnstile(page, info)
    assert result is False


def test_solve_turnstile_click_but_token_fail_returns_false() -> None:
    """点击复选框后 token 仍失败 → False。"""
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    checkbox = MagicMock()
    checkbox.bounding_box.return_value = None
    page = _make_page({".cf-turnstile": checkbox})
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    with (
        patch.object(solver, "_wait_for_token", side_effect=[False, False]),
        patch("web_crawler.ai.captcha.time.sleep"),
    ):
        result = solver._solve_turnstile(page, info)
    assert result is False


# ===========================================================================
# 扩展：_solve_hcaptcha / _solve_recaptcha_v2 / _solve_recaptcha_v3
# ===========================================================================


def test_solve_hcaptcha_no_checkbox_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    page = MagicMock()
    page.frame_locator.side_effect = RuntimeError("no frame")  # _find_hcaptcha_checkbox → None
    assert solver._solve_hcaptcha(page, info) is False


def test_solve_hcaptcha_auto_token_pass() -> None:
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    page = MagicMock()
    # _find_hcaptcha_checkbox 返回元素
    checkbox = MagicMock()
    checkbox.bounding_box.return_value = None
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = checkbox
    with (
        patch.object(solver, "_wait_for_token", return_value=True),
        patch("web_crawler.ai.captcha.time.sleep"),
    ):
        result = solver._solve_hcaptcha(page, info)
    assert result is True


def test_solve_hcaptcha_image_challenge_path_success() -> None:
    """token 失败但有 image_solver，图片挑战成功 → 二次 token 通过。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(max_wait=10.0, humanize=False, image_solver=img_solver)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    page = MagicMock()
    checkbox = MagicMock()
    checkbox.bounding_box.return_value = None
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = checkbox
    with (
        patch.object(solver, "_wait_for_token", side_effect=[False, True]),
        patch.object(solver, "_solve_image_challenge", return_value=True) as mock_img,
        patch("web_crawler.ai.captcha.time.sleep"),
    ):
        result = solver._solve_hcaptcha(page, info)
    assert result is True
    mock_img.assert_called_once()


def test_solve_hcaptcha_token_fail_no_image_solver_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    page = MagicMock()
    checkbox = MagicMock()
    checkbox.bounding_box.return_value = None
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = checkbox
    with (
        patch.object(solver, "_wait_for_token", return_value=False),
        patch("web_crawler.ai.captcha.time.sleep"),
    ):
        result = solver._solve_hcaptcha(page, info)
    assert result is False


def test_solve_recaptcha_v2_no_anchor_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V2)
    page = MagicMock()
    page.frame_locator.side_effect = RuntimeError("no frame")  # _find_recaptcha_anchor → None
    assert solver._solve_recaptcha_v2(page, info) is False


def test_solve_recaptcha_v2_auto_token_pass() -> None:
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V2)
    page = MagicMock()
    anchor = MagicMock()
    anchor.bounding_box.return_value = None
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = anchor
    with (
        patch.object(solver, "_wait_for_token", return_value=True),
        patch("web_crawler.ai.captcha.time.sleep"),
    ):
        result = solver._solve_recaptcha_v2(page, info)
    assert result is True


def test_solve_recaptcha_v2_image_challenge_path_success() -> None:
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(max_wait=10.0, humanize=False, image_solver=img_solver)
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V2, container_selector=_RECAPTCHA_IFRAME)
    page = MagicMock()
    anchor = MagicMock()
    anchor.bounding_box.return_value = None
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = anchor
    with (
        patch.object(solver, "_wait_for_token", side_effect=[False, True]),
        patch.object(solver, "_solve_image_challenge", return_value=True),
        patch("web_crawler.ai.captcha.time.sleep"),
    ):
        result = solver._solve_recaptcha_v2(page, info)
    assert result is True


def test_solve_recaptcha_v2_token_fail_no_image_solver_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V2)
    page = MagicMock()
    anchor = MagicMock()
    anchor.bounding_box.return_value = None
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = anchor
    with (
        patch.object(solver, "_wait_for_token", return_value=False),
        patch("web_crawler.ai.captcha.time.sleep"),
    ):
        result = solver._solve_recaptcha_v2(page, info)
    assert result is False


def test_solve_recaptcha_v3_waits_for_token() -> None:
    """v3 是无感验证，直接等 token。"""
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V3)
    with patch.object(solver, "_wait_for_token", return_value=True) as mock_wait:
        result = solver._solve_recaptcha_v3(MagicMock(), info)
    assert result is True
    # v3 用全部 max_wait 等待
    assert mock_wait.call_args.kwargs["timeout"] == 10.0


def test_solve_recaptcha_v3_token_timeout_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V3)
    with patch.object(solver, "_wait_for_token", return_value=False):
        assert solver._solve_recaptcha_v3(MagicMock(), info) is False


# ===========================================================================
# 扩展：_solve_geetest
# ===========================================================================


def test_solve_geetest_no_slider_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    page = _make_page({})  # 无滑块
    with patch.object(solver, "_wait_for_token", return_value=False):
        assert solver._solve_geetest(page, info) is False


def test_solve_geetest_no_bounding_box_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    slider = MagicMock()
    slider.bounding_box.return_value = None
    page = _make_page({".geetest_slider_button": slider})
    with patch.object(solver, "_wait_for_token", return_value=False):
        assert solver._solve_geetest(page, info) is False


def test_solve_geetest_bounding_box_exception_returns_false() -> None:
    """bounding_box 抛错 → box=None → False。"""
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    slider = MagicMock()
    slider.bounding_box.side_effect = RuntimeError("snap fail")
    page = _make_page({".geetest_slider_button": slider})
    with patch.object(solver, "_wait_for_token", return_value=False):
        assert solver._solve_geetest(page, info) is False


def test_solve_geetest_with_image_solver_offset_drags_and_passes() -> None:
    """image_solver 识别缺口偏移 → 拖拽 → token 通过。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(max_wait=10.0, humanize=False, image_solver=img_solver)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    slider = MagicMock()
    slider.bounding_box.return_value = {"x": 10, "y": 20, "width": 40, "height": 40}
    page = _make_page({".geetest_slider_button": slider})
    with (
        patch.object(solver, "_geetest_detect_offset", return_value=250.0),
        patch.object(solver, "_humanize_drag") as mock_drag,
        patch.object(solver, "_wait_for_token", return_value=True),
    ):
        result = solver._solve_geetest(page, info)
    assert result is True
    mock_drag.assert_called_once()
    # 验证拖拽终点 x = start_x + offset
    start, end = mock_drag.call_args.args[1:]
    assert end[0] == start[0] + 250.0


def test_solve_geetest_random_offset_when_no_image_solver() -> None:
    """无 image_solver 时用随机偏移兜底。"""
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    slider = MagicMock()
    slider.bounding_box.return_value = {"x": 0, "y": 0, "width": 50, "height": 50}
    page = _make_page({".geetest_slider_button": slider})
    with (
        patch.object(solver, "_geetest_detect_offset", return_value=None),
        patch.object(solver, "_humanize_drag") as mock_drag,
        patch.object(solver, "_wait_for_token", return_value=True),
        patch("web_crawler.ai.captcha.random.uniform", return_value=280.0),
    ):
        result = solver._solve_geetest(page, info)
    assert result is True
    _start, end = mock_drag.call_args.args[1:]
    # start_x = 0 + 50/2 = 25, offset=280 → end_x = 305
    assert end[0] == 25.0 + 280.0


def test_solve_geetest_token_fail_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0, humanize=False)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    slider = MagicMock()
    slider.bounding_box.return_value = {"x": 0, "y": 0, "width": 50, "height": 50}
    page = _make_page({".geetest_slider_button": slider})
    with (
        patch.object(solver, "_geetest_detect_offset", return_value=None),
        patch.object(solver, "_humanize_drag"),
        patch.object(solver, "_wait_for_token", return_value=False),
        patch("web_crawler.ai.captcha.random.uniform", return_value=280.0),
    ):
        result = solver._solve_geetest(page, info)
    assert result is False


def test_geetest_detect_offset_solver_exception_returns_none() -> None:
    """solve_slider 抛错 → 返回 None。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_slider.side_effect = RuntimeError("llm fail")
    solver = CaptchaSolver(image_solver=img_solver)
    panel = MagicMock()
    panel.screenshot.return_value = b"bg"
    slider_el = MagicMock()
    slider_el.screenshot.return_value = b"slider"
    page = _make_page({".geetest_panel": panel, ".geetest_slider_button": slider_el})
    assert solver._geetest_detect_offset(page) is None


def test_geetest_detect_offset_slider_screenshot_exception_returns_none() -> None:
    """slider 截图抛错 → 返回 None。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    panel = MagicMock()
    panel.screenshot.return_value = b"bg"
    slider_el = MagicMock()
    slider_el.screenshot.side_effect = RuntimeError("snap fail")
    page = _make_page({".geetest_panel": panel, ".geetest_slider_button": slider_el})
    assert solver._geetest_detect_offset(page) is None


# ===========================================================================
# 扩展：_wait_for_token
# ===========================================================================


def _make_token_element(value: str = "token123") -> MagicMock:
    """构造一个 evaluate 返回 token 值的元素。"""
    el = MagicMock()
    el.evaluate.return_value = value
    return el


def test_wait_for_token_found_in_textarea() -> None:
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    el = _make_token_element("h-captcha-token")
    page = _make_page({'textarea[name="h-captcha-response"]': el})
    assert solver._wait_for_token(page, info, timeout=5.0) is True


def test_wait_for_token_found_in_input() -> None:
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    el = _make_token_element("cf-token")
    page = _make_page({'input[name="cf-turnstile-response"]': el})
    assert solver._wait_for_token(page, info, timeout=5.0) is True


def test_wait_for_token_evaluate_exception_falls_back_to_attribute() -> None:
    """evaluate 抛错时用 get_attribute 兜底。"""
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V2)
    el = MagicMock()
    el.evaluate.side_effect = RuntimeError("no context")
    el.get_attribute.return_value = "attr-token"
    page = _make_page({'textarea[name="g-recaptcha-response"]': el})
    assert solver._wait_for_token(page, info, timeout=5.0) is True


def test_wait_for_token_empty_value_times_out_returns_false() -> None:
    """token 值为空 → 超时返回 False（timeout=0 避免真实等待）。"""
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    el = _make_token_element("")  # 空值
    page = _make_page({'textarea[name="h-captcha-response"]': el})
    with patch("web_crawler.ai.captcha.time.sleep"):
        assert solver._wait_for_token(page, info, timeout=0.0) is False


def test_wait_for_token_no_element_times_out_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    page = _make_page({})  # 无元素
    with patch("web_crawler.ai.captcha.time.sleep"):
        assert solver._wait_for_token(page, info, timeout=0.0) is False


def test_wait_for_token_query_exception_times_out_returns_false() -> None:
    """query_selector 抛错时视为 None，超时返回 False。"""
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    page = MagicMock()
    page.query_selector.side_effect = RuntimeError("boom")
    with patch("web_crawler.ai.captcha.time.sleep"):
        assert solver._wait_for_token(page, info, timeout=0.0) is False


def test_wait_for_token_geetest_success_selector_returns_true() -> None:
    """极验类型用通用成功标记，命中即返回 True。"""
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    el = MagicMock()
    page = _make_page({".geetest_success_radar_tip": el})
    assert solver._wait_for_token(page, info, timeout=5.0) is True


def test_wait_for_token_geetest_no_success_times_out_returns_false() -> None:
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.GEETEST)
    page = _make_page({})
    with patch("web_crawler.ai.captcha.time.sleep"):
        assert solver._wait_for_token(page, info, timeout=0.0) is False


def test_wait_for_token_unknown_type_uses_geetest_path() -> None:
    """UNKNOWN 类型无 token 字段，走通用成功标记路径。"""
    solver = CaptchaSolver(max_wait=10.0)
    info = CaptchaInfo(type=CaptchaType.UNKNOWN)
    el = MagicMock()
    page = _make_page({".geetest_commit_tip": el})
    assert solver._wait_for_token(page, info, timeout=5.0) is True


# ===========================================================================
# 扩展：_humanize_click / _humanize_drag / _bezier_points
# ===========================================================================


def test_humanize_click_with_bounding_box_humanize_off() -> None:
    """humanize=False + 有 bounding_box → 直接 mouse.click。"""
    solver = CaptchaSolver(humanize=False)
    selector = MagicMock()
    selector.bounding_box.return_value = {"x": 100, "y": 100, "width": 20, "height": 20}
    page = MagicMock()
    with patch("web_crawler.ai.captcha.random.uniform", return_value=0.0):
        solver._humanize_click(page, selector)
    page.mouse.click.assert_called_once()
    # 中心点 110, 110 + 偏移 0
    call_args = page.mouse.click.call_args.args
    assert call_args == (110.0, 110.0)


def test_humanize_click_with_bounding_box_humanize_on() -> None:
    """humanize=True + 有 bounding_box → 贝塞尔移动 + 点击 + sleep。"""
    solver = CaptchaSolver(humanize=True)
    selector = MagicMock()
    selector.bounding_box.return_value = {"x": 0, "y": 0, "width": 10, "height": 10}
    page = MagicMock()
    with (
        patch.object(solver, "_bezier_move") as mock_bezier,
        patch("web_crawler.ai.captcha.time.sleep"),
        patch("web_crawler.ai.captcha.random.uniform", return_value=2.0),
    ):
        solver._humanize_click(page, selector)
    mock_bezier.assert_called_once()
    page.mouse.click.assert_called_once()


def test_humanize_click_no_bounding_box_falls_back_to_element_click() -> None:
    """无 bounding_box → 退化为元素原生 click。"""
    solver = CaptchaSolver(humanize=False)
    selector = MagicMock()
    selector.bounding_box.return_value = None
    page = MagicMock()
    solver._humanize_click(page, selector)
    selector.click.assert_called_once()


def test_humanize_click_no_bounding_box_humanize_on_sleeps() -> None:
    """无 bounding_box + humanize=True → 元素 click + sleep。"""
    solver = CaptchaSolver(humanize=True)
    selector = MagicMock()
    selector.bounding_box.return_value = None
    page = MagicMock()
    with patch("web_crawler.ai.captcha.time.sleep"):
        solver._humanize_click(page, selector)
    selector.click.assert_called_once()


def test_humanize_click_no_bounding_box_element_click_exception_swallows() -> None:
    """元素 click 抛错时静默返回。"""
    solver = CaptchaSolver(humanize=False)
    selector = MagicMock()
    selector.bounding_box.return_value = None
    selector.click.side_effect = RuntimeError("detached")
    page = MagicMock()
    # 不应抛错
    solver._humanize_click(page, selector)


def test_humanize_drag_calls_mouse_sequence() -> None:
    """_humanize_drag 应依次调用 move/down/move.../up。"""
    solver = CaptchaSolver(humanize=True)
    page = MagicMock()
    with (
        patch("web_crawler.ai.captcha.time.sleep"),
        patch("web_crawler.ai.captcha.random.uniform", return_value=0.1),
    ):
        solver._humanize_drag(page, (0.0, 0.0), (100.0, 0.0))
    page.mouse.move.assert_called()
    page.mouse.down.assert_called_once()
    page.mouse.up.assert_called_once()


def test_bezier_points_returns_steps_plus_one_points() -> None:
    """贝塞尔曲线应返回 steps+1 个点（含起止）。"""
    points = CaptchaSolver._bezier_points((0.0, 0.0), (100.0, 0.0), steps=10)
    assert len(points) == 11
    # 起点
    assert points[0] == (0.0, 0.0)
    # 终点
    assert points[-1] == (100.0, 0.0)


def test_bezier_points_zero_distance_handles_degenerate_norm() -> None:
    """起止重合时 norm<1e-6，控制点偏移为 0，不抛错。"""
    points = CaptchaSolver._bezier_points((50.0, 50.0), (50.0, 50.0), steps=5)
    assert len(points) == 6


def test_bezier_move_calls_mouse_move_repeatedly() -> None:
    """_bezier_move 应多次调用 page.mouse.move。"""
    solver = CaptchaSolver(humanize=True)
    page = MagicMock()
    with (
        patch("web_crawler.ai.captcha.time.sleep"),
        patch("web_crawler.ai.captcha.random.uniform", return_value=0.01),
    ):
        solver._bezier_move(page, 200.0, 200.0)
    assert page.mouse.move.call_count >= 1


# ===========================================================================
# 扩展：_find_hcaptcha_checkbox / _find_recaptcha_anchor
# ===========================================================================


def test_find_hcaptcha_checkbox_returns_element() -> None:
    page = MagicMock()
    el = MagicMock()
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = el
    assert CaptchaSolver._find_hcaptcha_checkbox(page) is el


def test_find_hcaptcha_checkbox_exception_returns_none() -> None:
    page = MagicMock()
    page.frame_locator.side_effect = RuntimeError("no frame")
    assert CaptchaSolver._find_hcaptcha_checkbox(page) is None


def test_find_recaptcha_anchor_returns_element() -> None:
    page = MagicMock()
    el = MagicMock()
    page.frame_locator.return_value.locator.return_value.element_handle.return_value = el
    assert CaptchaSolver._find_recaptcha_anchor(page) is el


def test_find_recaptcha_anchor_exception_returns_none() -> None:
    page = MagicMock()
    page.frame_locator.return_value.locator.side_effect = RuntimeError("no frame")
    assert CaptchaSolver._find_recaptcha_anchor(page) is None


# ===========================================================================
# 扩展：_read_challenge_prompt / _submit_challenge / _find_challenge_frame
# ===========================================================================


def test_read_challenge_prompt_hcaptcha_returns_text() -> None:
    page = MagicMock()
    frame = MagicMock()
    loc = MagicMock()
    loc.text_content.return_value = "请点击所有自行车"
    frame.locator.return_value.first = loc
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert CaptchaSolver._read_challenge_prompt(page, info) == "请点击所有自行车"


def test_read_challenge_prompt_recaptcha_v2_returns_text() -> None:
    page = MagicMock()
    frame = MagicMock()
    loc = MagicMock()
    loc.text_content.return_value = "select all traffic lights"
    frame.locator.return_value.first = loc
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V2, container_selector=_RECAPTCHA_IFRAME)
    assert CaptchaSolver._read_challenge_prompt(page, info) == "select all traffic lights"


def test_read_challenge_prompt_no_selector_map_returns_empty() -> None:
    """不在 selector_map 中的类型（如 TURNSTILE）返回空串。"""
    page = MagicMock()
    info = CaptchaInfo(type=CaptchaType.TURNSTILE, container_selector=_TURNSTILE_IFRAMES[0])
    assert CaptchaSolver._read_challenge_prompt(page, info) == ""


def test_read_challenge_prompt_no_container_selector_returns_empty() -> None:
    page = MagicMock()
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=None)
    assert CaptchaSolver._read_challenge_prompt(page, info) == ""


def test_read_challenge_prompt_frame_locator_exception_returns_empty() -> None:
    page = MagicMock()
    page.frame_locator.side_effect = RuntimeError("no frame")
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert CaptchaSolver._read_challenge_prompt(page, info) == ""


def test_read_challenge_prompt_all_text_empty_returns_empty() -> None:
    """所有选择器 text_content 为空时返回空串。"""
    page = MagicMock()
    frame = MagicMock()
    loc = MagicMock()
    loc.text_content.return_value = ""  # 空
    frame.locator.return_value.first = loc
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert CaptchaSolver._read_challenge_prompt(page, info) == ""


def test_submit_challenge_hcaptcha_clicks_button() -> None:
    page = MagicMock()
    frame = MagicMock()
    btn = MagicMock()
    frame.locator.return_value.first = btn
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    CaptchaSolver._submit_challenge(page, info)
    btn.click.assert_called_once()


def test_submit_challenge_recaptcha_v2_clicks_button() -> None:
    page = MagicMock()
    frame = MagicMock()
    btn = MagicMock()
    frame.locator.return_value.first = btn
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.RECAPTCHA_V2, container_selector=_RECAPTCHA_IFRAME)
    CaptchaSolver._submit_challenge(page, info)
    btn.click.assert_called_once()


def test_submit_challenge_no_selector_map_does_nothing() -> None:
    """不在 selector_map 的类型不做任何操作。"""
    page = MagicMock()
    info = CaptchaInfo(type=CaptchaType.TURNSTILE, container_selector=_TURNSTILE_IFRAMES[0])
    CaptchaSolver._submit_challenge(page, info)
    page.frame_locator.assert_not_called()


def test_submit_challenge_no_container_selector_does_nothing() -> None:
    page = MagicMock()
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=None)
    CaptchaSolver._submit_challenge(page, info)
    page.frame_locator.assert_not_called()


def test_submit_challenge_frame_locator_exception_swallows() -> None:
    page = MagicMock()
    page.frame_locator.side_effect = RuntimeError("no frame")
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    # 不应抛错
    CaptchaSolver._submit_challenge(page, info)


def test_submit_challenge_all_buttons_fail_silently() -> None:
    """所有提交按钮 click 都抛错时静默。"""
    page = MagicMock()
    frame = MagicMock()
    btn = MagicMock()
    btn.click.side_effect = RuntimeError("detached")
    frame.locator.return_value.first = btn
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    CaptchaSolver._submit_challenge(page, info)  # 不抛错


def test_find_challenge_frame_returns_element() -> None:
    page = MagicMock()
    el = MagicMock()
    page.query_selector.return_value = el
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert CaptchaSolver._find_challenge_frame(page, info) is el


def test_find_challenge_frame_no_container_returns_none() -> None:
    page = MagicMock()
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=None)
    assert CaptchaSolver._find_challenge_frame(page, info) is None


def test_find_challenge_frame_exception_returns_none() -> None:
    page = MagicMock()
    page.query_selector.side_effect = RuntimeError("boom")
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert CaptchaSolver._find_challenge_frame(page, info) is None


def test_solve_image_challenge_bounding_box_exception_returns_false() -> None:
    """bounding_box 抛错 → box=None → False。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    iframe = MagicMock()
    iframe.bounding_box.side_effect = RuntimeError("detached")
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_mouse_click_exception_returns_false() -> None:
    """点击坐标抛错 → 返回 False。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_click.return_value = ClickSolution(points=[(10, 20)])
    solver = CaptchaSolver(image_solver=img_solver, humanize=False)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 0, "y": 0, "width": 100, "height": 100}
    iframe.screenshot.return_value = b"png"
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    page.mouse.click.side_effect = RuntimeError("detached")
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_empty_points_returns_false() -> None:
    """solve_click 返回空 points → False。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_click.return_value = ClickSolution(points=[])
    solver = CaptchaSolver(image_solver=img_solver, humanize=False)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 0, "y": 0, "width": 100, "height": 100}
    iframe.screenshot.return_value = b"png"
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    assert solver._solve_image_challenge(page, info) is False


def test_solve_image_challenge_reads_prompt_from_page() -> None:
    """_read_challenge_prompt 命中提示文字时传给 solve_click。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_click.return_value = ClickSolution(points=[(1, 2)])
    solver = CaptchaSolver(image_solver=img_solver, humanize=False)
    iframe = MagicMock()
    iframe.bounding_box.return_value = {"x": 0, "y": 0, "width": 100, "height": 100}
    iframe.screenshot.return_value = b"png"
    frame = MagicMock()
    loc = MagicMock()
    loc.text_content.return_value = "点击所有猫"
    frame.locator.return_value.first = loc
    page = _make_page({_HCAPTCHA_IFRAME: iframe})
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    result = solver._solve_image_challenge(page, info)
    assert result is True
    # solve_click 第二个参数应是提示文字
    assert img_solver.solve_click.call_args.args[1] == "点击所有猫"


# ===========================================================================
# 扩展：CaptchaManager.handle（检测到验证码的分支）
# ===========================================================================


def test_captcha_manager_handle_with_captcha_calls_solver() -> None:
    """检测到验证码时调用 solver.solve。"""
    manager = CaptchaManager()
    info = CaptchaInfo(type=CaptchaType.TURNSTILE)
    page = MagicMock()
    with (
        patch.object(manager.detector, "detect", return_value=info),
        patch.object(manager.solver, "solve", return_value=True) as mock_solve,
    ):
        result = manager.handle(page)
    assert result is True
    mock_solve.assert_called_once_with(page, info)


def test_captcha_manager_handle_solver_returns_false() -> None:
    """solver 返回 False 时 manager.handle 返回 False。"""
    manager = CaptchaManager()
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA)
    page = MagicMock()
    with (
        patch.object(manager.detector, "detect", return_value=info),
        patch.object(manager.solver, "solve", return_value=False),
    ):
        result = manager.handle(page)
    assert result is False


def test_captcha_manager_handle_none_info_returns_true() -> None:
    """未检测到验证码（info=None）时返回 True。"""
    manager = CaptchaManager()
    page = MagicMock()
    with patch.object(manager.detector, "detect", return_value=None):
        assert manager.handle(page) is True


def test_captcha_manager_handle_none_type_returns_true() -> None:
    """检测到 NONE 类型时返回 True（无需处理）。"""
    manager = CaptchaManager()
    page = MagicMock()
    info = CaptchaInfo(type=CaptchaType.NONE)
    with (
        patch.object(manager.detector, "detect", return_value=info),
        patch.object(manager.solver, "solve") as mock_solve,
    ):
        assert manager.handle(page) is True
    mock_solve.assert_not_called()


# ===========================================================================
# 扩展：补充边界分支（提升覆盖率）
# ===========================================================================


def test_geetest_detect_offset_image_solver_but_no_panel_returns_none() -> None:
    """image_solver 已注入但页面上无极验面板 → panel None → 返回 None。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    solver = CaptchaSolver(image_solver=img_solver)
    page = _make_page({})  # 无 geetest 面板
    assert solver._geetest_detect_offset(page) is None


def test_read_challenge_prompt_text_content_exception_returns_empty() -> None:
    """text_content 抛错时该选择器视为无文本，继续后续选择器。"""
    page = MagicMock()
    frame = MagicMock()
    # 第一次 locator().first.text_content 抛错，第二次返回文本
    failing_loc = MagicMock()
    failing_loc.first.text_content.side_effect = RuntimeError("timeout")
    good_loc = MagicMock()
    good_loc.first.text_content.return_value = "最终提示"
    frame.locator.side_effect = [failing_loc, good_loc]
    page.frame_locator.return_value = frame
    info = CaptchaInfo(type=CaptchaType.HCAPTCHA, container_selector=_HCAPTCHA_IFRAME)
    result = CaptchaSolver._read_challenge_prompt(page, info)
    assert result == "最终提示"


def test_humanize_click_bounding_box_exception_falls_back_to_none() -> None:
    """bounding_box() 抛错时 box=None，退化为元素 click 路径。"""
    solver = CaptchaSolver(humanize=False)
    selector = MagicMock()
    selector.bounding_box.side_effect = RuntimeError("detached")
    page = MagicMock()
    solver._humanize_click(page, selector)
    # box=None 分支 → 调用元素原生 click
    selector.click.assert_called_once()


def test_humanize_click_bounding_box_exception_humanize_on_sleeps() -> None:
    """bounding_box 抛错 + humanize=True → 元素 click + sleep。"""
    solver = CaptchaSolver(humanize=True)
    selector = MagicMock()
    selector.bounding_box.side_effect = RuntimeError("detached")
    page = MagicMock()
    with patch("web_crawler.ai.captcha.time.sleep"):
        solver._humanize_click(page, selector)
    selector.click.assert_called_once()
