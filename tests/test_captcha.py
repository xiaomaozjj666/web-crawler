"""验证码检测/处理模块测试（mock page，不启动浏览器）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from web_crawler.ai.captcha import (
    _HCAPTCHA_IFRAME,
    CaptchaDetector,
    CaptchaInfo,
    CaptchaManager,
    CaptchaSolver,
    CaptchaType,
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
