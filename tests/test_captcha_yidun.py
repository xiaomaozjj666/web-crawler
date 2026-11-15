"""易盾（YIDUN）检测/求解 + solve_url 统一入口测试。"""

from __future__ import annotations

import base64
import struct
import zlib
from unittest.mock import MagicMock, patch

from web_crawler.ai.captcha import CaptchaInfo, CaptchaManager, CaptchaType
from web_crawler.ai.image_captcha import ImageCaptchaSolver, SliderSolution


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _tiny_png_bytes(w: int = 8, h: int = 4, color: int = 128) -> bytes:
    raw = b"".join(b"\x00" + bytes([color]) * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _make_yidun_page(geo: dict, states: list[dict]) -> MagicMock:
    """mock page：非 yidun 选择器返回 None（避免 hCaptcha 抢先命中），
    evaluate 按 _solve_yidun 的调用顺序返回 [geo, bg_dataurl, piece_dataurl,
    scroll, handle_box, *states]。"""
    page = MagicMock()
    page.query_selector.side_effect = lambda sel: MagicMock() if "yidun" in sel else None

    def dataurl(b64: str) -> str:
        return f"data:image/png;base64,{b64}"

    png_b64 = base64.b64encode(_tiny_png_bytes()).decode()
    page.evaluate.side_effect = [
        geo,
        dataurl(png_b64),
        dataurl(png_b64),
        1,
        {"x": geo.get("hx", 648.0), "y": geo.get("hy", 360.0)},
    ] + list(states)
    return page


def test_detect_yidun_returns_yidun_info() -> None:
    """bg 图命中 → CaptchaInfo(YIDUN)。"""
    from web_crawler.ai.captcha import CaptchaDetector

    detector = CaptchaDetector()
    page = MagicMock()
    page.query_selector.side_effect = lambda sel: MagicMock() if "yidun" in sel else None
    info = detector.detect(page)
    assert info is not None
    assert info.type is CaptchaType.YIDUN


def test_detect_yidun_absent_returns_none() -> None:
    from web_crawler.ai.captcha import CaptchaDetector

    detector = CaptchaDetector()
    page = MagicMock()
    page.query_selector.return_value = None
    assert detector.detect(page) is None


def test_solve_yidun_success_flow() -> None:
    """易盾求解全流程：URL 抓图 → 定位 → 拖拽 → tips「成功」→ True。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_slider.return_value = SliderSolution(x=150, method="pillow")
    manager = CaptchaManager(image_solver=img_solver)
    manager.solver.max_wait = 2.0

    geo = {"bg": "https://x/bg.jpg", "piece": "https://x/p.png", "hx": 648.0, "hy": 360.0}
    states = [{"tips": "验证成功", "bg": "bg.jpg"}]
    page = _make_yidun_page(geo, states)

    info = CaptchaInfo(type=CaptchaType.YIDUN, container_selector="img.yidun_bg-img")
    assert manager.solver.solve(page, info) is True
    # solve_slider 收到的是解码后的 PNG 原图字节（非 dataURL）
    args = img_solver.solve_slider.call_args[0][0]
    assert args[:8] == b"\x89PNG\r\n\x1a\n"


def test_solve_yidun_fail_refresh_flow() -> None:
    """校验失败自动换图（bg src 变化）→ False。"""
    img_solver = MagicMock(spec=ImageCaptchaSolver)
    img_solver.solve_slider.return_value = SliderSolution(x=150, method="pillow")
    manager = CaptchaManager(image_solver=img_solver)
    manager.solver.max_wait = 2.0

    geo = {"bg": "https://x/old.jpg", "piece": "https://x/p.png", "hx": 648.0, "hy": 360.0}
    states = [
        {"tips": "验证中...", "bg": "old.jpg"},
        {"tips": "向右拖动滑块填充拼图", "bg": "new.jpg"},
    ]
    page = _make_yidun_page(geo, states)

    info = CaptchaInfo(type=CaptchaType.YIDUN, container_selector="img.yidun_bg-img")
    assert manager.solver.solve(page, info) is False


def test_solve_yidun_no_image_solver_returns_false() -> None:
    """无 image_solver 时易盾求解直接返回 False（不盲拖）。"""
    manager = CaptchaManager()  # 无 image_solver
    page = _make_yidun_page({"bg": "x", "piece": "x", "hx": 1.0, "hy": 1.0}, [])
    info = CaptchaInfo(type=CaptchaType.YIDUN, container_selector="img.yidun_bg-img")
    assert manager.solver.solve(page, info) is False


def test_solve_url_smoke_none_page() -> None:
    """solve_url 冒烟：detect 无验证码 → type=none / solved=None。"""
    manager = CaptchaManager()

    class _FakeFetcher:
        last_kwargs: dict = {}

        def __init__(self, **kwargs: object) -> None:
            _FakeFetcher.last_kwargs = dict(kwargs)

        def __enter__(self) -> _FakeFetcher:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def fetch(self, url: str) -> None:
            empty_page = MagicMock()
            empty_page.query_selector.return_value = None  # 页面无验证码
            if self.last_kwargs.get("page_action"):
                self.last_kwargs["page_action"](empty_page)

    # solve_url 先过 require_camoufox 守卫再取 fetcher：冒烟用例只关心路由，
    # 默认依赖档没有 camoufox，守卫一并放行
    with (
        patch("web_crawler.compat.require_camoufox", lambda: None),
        patch("web_crawler.fetchers.camoufox.CamoufoxFetcher", _FakeFetcher),
    ):
        result = manager.solve_url("https://example.com", headless=True)
    assert result["type"] == "none"
    assert result["solved"] is None
    assert result["url"] == "https://example.com"
    # 构造参数落到 fetcher：humanize/headless 透传
    assert _FakeFetcher.last_kwargs["headless"] is True
    assert _FakeFetcher.last_kwargs["humanize"] is True
