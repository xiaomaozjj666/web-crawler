"""验证码检测与处理模块。

检测页面上常见的几类验证码（hCaptcha / Cloudflare Turnstile / Google
reCAPTCHA v2 & v3 / 极验 GeeTest），并尝试以下两类方式处理：

1. **模拟正常用户交互**：入口点击、自动通过等待、滑块贝塞尔轨迹模拟
2. **图片验证码自动识别**（可选，需注入 :class:`ImageCaptchaSolver`）：
   - hCaptcha / reCAPTCHA v2 图片选择挑战：截图 iframe → Vision-LLM 识别
     点击坐标 → 模拟点击 → 等待 token
   - 极验拼图：截图面板 → Pillow/numpy 模板匹配 或 Vision-LLM 识别
     缺口 x 偏移 → 拖拽滑块
   - 文本字符 OCR（独立调用 :meth:`ImageCaptchaSolver.solve_text`）

未注入 ``image_solver`` 时，遇到图片挑战主动返回 ``False`` 表示需要人工
介入（兼容旧版行为）。

设计参考了 BrowserAct 的"卡住即移交人工"思路：本模块默认只覆盖能够通过
模拟正常用户交互完成的部分，其他场景一律交人工，不尝试任何绕过；
``image_solver`` 是可选增强，调用方按合规策略决定是否启用。

page 对象约定为 Playwright 的 sync_api.Page，但为了避免强依赖 playwright
（其作为可选依赖存在），类型标注使用 ``Any``，仅在 ``TYPE_CHECKING`` 下导入。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from typing import Any

    from .image_captcha import ImageCaptchaSolver


class CaptchaType(Enum):
    """已知的验证码类型。"""

    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    GEETEST = "geetest"
    UNKNOWN = "unknown"
    NONE = "none"


@dataclass
class CaptchaInfo:
    """一次验证码检测结果。"""

    type: CaptchaType
    iframe_url: str | None = None
    site_key: str | None = None
    container_selector: str | None = None
    detected_at: float = field(default_factory=time.time)


# 各类型在 DOM 上的探测特征
_HCAPTCHA_IFRAME = 'iframe[src*="hcaptcha"]'
_TURNSTILE_IFRAMES: tuple[str, ...] = (
    'iframe[src*="turnstile"]',
    'iframe[src*="challenges.cloudflare.com"]',
)
_RECAPTCHA_IFRAME = 'iframe[src*="recaptcha"]'

# 极验容器类名前缀（命中其一即视为极验）
_GEETEST_SELECTORS: tuple[str, ...] = (
    ".geetest_panel",
    ".geetest_widget",
    ".geetest_container",
)

# 各类型验证码通过后写入页面的 token 字段名
_TOKEN_FIELDS: dict[CaptchaType, tuple[str, ...]] = {
    CaptchaType.HCAPTCHA: ("h-captcha-response",),
    CaptchaType.TURNSTILE: ("cf-turnstile-response",),
    CaptchaType.RECAPTCHA_V2: ("g-recaptcha-response",),
    CaptchaType.RECAPTCHA_V3: ("g-recaptcha-response",),
}

# 极验/未知类型通过后的通用成功标记
_GEETEST_SUCCESS_SELECTORS: tuple[str, ...] = (
    ".geetest_success_radar_tip",
    ".geetest_commit_tip",
    ".geetest_result_box",
)


def _first_query(page: Any, selectors: tuple[str, ...]) -> Any | None:
    """按顺序查询多组选择器，返回首个命中的 ElementHandle。"""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            el = None
        if el is not None:
            return el
    return None


def _sitekey_from_url(url: str, *keys: str) -> str | None:
    """从 URL query 里取首个命中的 sitekey 风格参数。"""
    if not url:
        return None
    qs = parse_qs(urlparse(url).query)
    for k in keys:
        vals = qs.get(k)
        if vals and vals[0]:
            return vals[0]
    return None


class CaptchaDetector:
    """检测页面上的验证码类型与元信息。"""

    def detect(self, page: Any) -> CaptchaInfo | None:
        """对当前页面执行一次验证码探测，未命中返回 ``None``。"""
        for fn in (
            self._detect_hcaptcha,
            self._detect_turnstile,
            self._detect_recaptcha,
            self._detect_geetest,
        ):
            info = fn(page)
            if info is not None:
                return info
        return None

    # -- 各类型分支 --------------------------------------------------------
    def _detect_hcaptcha(self, page: Any) -> CaptchaInfo | None:
        try:
            iframe = page.query_selector(_HCAPTCHA_IFRAME)
        except Exception:
            iframe = None
        if iframe is None:
            return None
        src = iframe.get_attribute("src") or ""
        site_key = self._read_data_sitekey(page) or _sitekey_from_url(src, "sitekey")
        return CaptchaInfo(
            type=CaptchaType.HCAPTCHA,
            iframe_url=src or None,
            site_key=site_key,
            container_selector=_HCAPTCHA_IFRAME,
        )

    def _detect_turnstile(self, page: Any) -> CaptchaInfo | None:
        iframe = None
        matched_sel: str | None = None
        for sel in _TURNSTILE_IFRAMES:
            try:
                iframe = page.query_selector(sel)
            except Exception:
                iframe = None
            if iframe is not None:
                matched_sel = sel
                break
        if iframe is None:
            return None
        src = iframe.get_attribute("src") or ""
        site_key = self._read_data_sitekey(page) or _sitekey_from_url(src, "sitekey")
        return CaptchaInfo(
            type=CaptchaType.TURNSTILE,
            iframe_url=src or None,
            site_key=site_key,
            container_selector=matched_sel,
        )

    def _detect_recaptcha(self, page: Any) -> CaptchaInfo | None:
        try:
            iframe = page.query_selector(_RECAPTCHA_IFRAME)
        except Exception:
            iframe = None
        if iframe is None:
            return None
        src = iframe.get_attribute("src") or ""
        # v3 通常带 render=<sitekey> 或路径含 /enterprise/，且无 anchor；
        # v2 入口 iframe 路径含 /anchor。
        is_v3 = ("/enterprise/" in src and "/anchor" not in src) or (
            "render=" in src and "render=explicit" not in src
        )
        if is_v3:
            site_key = _sitekey_from_url(src, "render", "k")
            captcha_type = CaptchaType.RECAPTCHA_V3
        else:
            site_key = self._read_data_sitekey(page) or _sitekey_from_url(src, "k")
            captcha_type = CaptchaType.RECAPTCHA_V2
        return CaptchaInfo(
            type=captcha_type,
            iframe_url=src or None,
            site_key=site_key,
            container_selector=_RECAPTCHA_IFRAME,
        )

    def _detect_geetest(self, page: Any) -> CaptchaInfo | None:
        for sel in _GEETEST_SELECTORS:
            try:
                el = page.query_selector(sel)
            except Exception:
                el = None
            if el is not None:
                return CaptchaInfo(
                    type=CaptchaType.GEETEST,
                    container_selector=sel,
                )
        return None

    @staticmethod
    def _read_data_sitekey(page: Any) -> str | None:
        """从页面上任意带 data-sitekey 的元素读取 sitekey。"""
        try:
            el = page.query_selector("[data-sitekey]")
        except Exception:
            return None
        if el is None:
            return None
        return el.get_attribute("data-sitekey")


class CaptchaSolver:
    """验证码处理策略：入口点击 / 自动通过等待 / 滑块模拟 + 可选图片识别。

    未注入 ``image_solver`` 时，遇到图片选择类挑战直接返回 ``False`` 表示
    需要人工介入（兼容旧版行为）；注入后，会在常规路径失败后尝试用
    :class:`ImageCaptchaSolver` 截图识别 + 模拟点击/拖拽。
    """

    def __init__(
        self,
        max_wait: float = 30.0,
        humanize: bool = True,
        image_solver: ImageCaptchaSolver | None = None,
    ) -> None:
        self.max_wait = max_wait
        self.humanize = humanize
        self.image_solver = image_solver

    def solve(self, page: Any, info: CaptchaInfo) -> bool:
        """按类型分发处理策略，返回是否成功通过。"""
        if info.type is CaptchaType.NONE:
            return True
        dispatch = {
            CaptchaType.TURNSTILE: self._solve_turnstile,
            CaptchaType.HCAPTCHA: self._solve_hcaptcha,
            CaptchaType.RECAPTCHA_V2: self._solve_recaptcha_v2,
            CaptchaType.RECAPTCHA_V3: self._solve_recaptcha_v3,
            CaptchaType.GEETEST: self._solve_geetest,
            CaptchaType.UNKNOWN: self._solve_unknown,
        }
        handler = dispatch.get(info.type)
        if handler is None:  # pragma: no cover - 所有 CaptchaType 已在 dispatch 中覆盖
            return False
        try:
            return handler(page, info)
        except Exception:
            return False

    # -- 各类型策略 --------------------------------------------------------
    def _solve_turnstile(self, page: Any, info: CaptchaInfo) -> bool:
        # Turnstile 大多数场景会自动通过；先等待 token，再尝试点击复选框
        half = self.max_wait * 0.5
        if self._wait_for_token(page, info, timeout=half):
            return True
        checkbox = _first_query(
            page,
            (
                'input[type="checkbox"][name="cf-turnstile-response"]',
                ".cf-turnstile input[type='checkbox']",
                ".cf-turnstile",
            ),
        )
        if checkbox is not None:
            self._humanize_click(page, checkbox)
        return self._wait_for_token(page, info, timeout=half)

    def _solve_hcaptcha(self, page: Any, info: CaptchaInfo) -> bool:
        # 仅点击入口复选框；若出现图片选择挑战，尝试 image_solver
        checkbox = self._find_hcaptcha_checkbox(page)
        if checkbox is None:
            return False
        self._humanize_click(page, checkbox)
        # 先等半段时间看是否自动通过
        if self._wait_for_token(page, info, timeout=self.max_wait * 0.5):
            return True
        # 出现图片挑战：尝试 image_solver 截图识别 + 点击
        if self.image_solver is not None and self._solve_image_challenge(page, info):
            return self._wait_for_token(page, info, timeout=self.max_wait * 0.5)
        return False

    def _solve_recaptcha_v2(self, page: Any, info: CaptchaInfo) -> bool:
        anchor = self._find_recaptcha_anchor(page)
        if anchor is None:
            return False
        self._humanize_click(page, anchor)
        if self._wait_for_token(page, info, timeout=self.max_wait * 0.5):
            return True
        if self.image_solver is not None and self._solve_image_challenge(page, info):
            return self._wait_for_token(page, info, timeout=self.max_wait * 0.5)
        return False

    def _solve_recaptcha_v3(self, page: Any, info: CaptchaInfo) -> bool:
        # reCAPTCHA v3 是无感的，直接等 token
        return self._wait_for_token(page, info, timeout=self.max_wait)

    def _solve_geetest(self, page: Any, info: CaptchaInfo) -> bool:
        # 极验：无 image_solver 或缺口识别失败时不随机盲拖，直接交人工
        if self.image_solver is None:
            return False
        slider = _first_query(
            page,
            (
                ".geetest_slider_button",
                ".geetest_btn_slide",
                ".geetest_slider",
            ),
        )
        if slider is None:
            return False
        try:
            box = slider.bounding_box()
        except Exception:
            box = None
        if box is None:
            return False
        start = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        # 用 image_solver 识别缺口 x 偏移；识别失败交人工，不做盲拖
        offset = self._geetest_detect_offset(page)
        if offset is None:
            return False
        # 缺口坐标以背景图左边缘为原点，滑块按钮中心自带半宽偏移，需扣减
        distance = max(0.0, offset - box["width"] / 2.0)
        end = (start[0] + distance, start[1])
        self._humanize_drag(page, start, end)
        return self._wait_for_token(page, info, timeout=self.max_wait * 0.5)

    def _geetest_detect_offset(self, page: Any) -> float | None:
        """用 image_solver 识别极验拼图缺口 x 偏移；失败返回 None。"""
        if self.image_solver is None:
            return None
        panel = _first_query(page, _GEETEST_SELECTORS)
        if panel is None:
            return None
        try:
            bg_bytes = panel.screenshot()
        except Exception:
            return None
        slider_el = _first_query(
            page,
            (".geetest_slider_button", ".geetest_btn_slide", ".geetest_slider"),
        )
        if slider_el is None:
            return None
        try:
            slider_bytes = slider_el.screenshot()
        except Exception:
            return None
        try:
            sol = self.image_solver.solve_slider(bg_bytes, slider_bytes)
        except Exception:
            return None
        if sol is None:
            return None
        return float(sol.x)

    def _solve_image_challenge(self, page: Any, info: CaptchaInfo) -> bool:
        """处理图片选择挑战：截图 iframe → 识别点击坐标 → 模拟点击。

        成功识别并点击后返回 True；token 等待由调用方负责。
        失败原因（无 image_solver / 无 iframe / 识别失败 / 截图失败）
        都返回 False，调用方交人工兜底。
        """
        if self.image_solver is None:
            return False
        frame_handle = self._find_challenge_frame(page, info)
        if frame_handle is None:
            return False
        try:
            box = frame_handle.bounding_box()
        except Exception:
            box = None
        if box is None:
            return False
        offset_x = float(box.get("x", 0.0))
        offset_y = float(box.get("y", 0.0))
        try:
            img_bytes = frame_handle.screenshot()
        except Exception:
            return False
        prompt = self._read_challenge_prompt(page, info) or "请按提示点击对应元素"
        try:
            sol = self.image_solver.solve_click(img_bytes, prompt)
        except Exception:
            return False
        if sol is None or not sol.points:
            return False
        # LLM 可能幻觉出越界坐标，钳制到 iframe 可视范围内再点击
        box_w = float(box.get("width", 0.0) or 0.0)
        box_h = float(box.get("height", 0.0) or 0.0)
        for px, py in sol.points:
            px = int(min(max(float(px), 0.0), box_w - 1)) if box_w > 0 else int(px)
            py = int(min(max(float(py), 0.0), box_h - 1)) if box_h > 0 else int(py)
            try:
                page.mouse.click(offset_x + px, offset_y + py)
            except Exception:
                return False
            if self.humanize:
                time.sleep(random.uniform(0.3, 0.8))
        # 尝试点击提交按钮（hCaptcha 的 .button.submit / reCaptcha 的
        # rc-imageselect-submit），失败静默（很多验证码点完会自动提交）
        self._submit_challenge(page, info)
        return True

    @staticmethod
    def _find_challenge_frame(page: Any, info: CaptchaInfo) -> Any | None:
        """定位图片挑战 iframe 的 ElementHandle。"""
        if info.container_selector is None:
            return None
        try:
            return page.query_selector(info.container_selector)
        except Exception:
            return None

    @staticmethod
    def _read_challenge_prompt(page: Any, info: CaptchaInfo) -> str:
        """尽力读取挑战提示文字（hCaptcha/reCaptcha 的 .prompt-text 等）。"""
        selector_map: dict[CaptchaType, tuple[str, ...]] = {
            CaptchaType.HCAPTCHA: (
                ".prompt-text",
                ".h-captcha-challenge-prompt",
            ),
            CaptchaType.RECAPTCHA_V2: (
                ".rc-imageselect-desc",
                ".rc-imageselect-desc-no-canvas",
            ),
        }
        selectors = selector_map.get(info.type)
        if selectors is None:
            return ""
        # 通过 frame_locator 进入 iframe 内部读取
        if info.container_selector is None:
            return ""
        try:
            frame = page.frame_locator(info.container_selector)
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    text = loc.text_content(timeout=1500)
                except Exception:
                    text = None
                if text:
                    return str(text).strip()
        except Exception:
            return ""
        return ""

    @staticmethod
    def _submit_challenge(page: Any, info: CaptchaInfo) -> None:
        """尽力点击提交按钮；失败静默。"""
        selector_map: dict[CaptchaType, tuple[str, ...]] = {
            CaptchaType.HCAPTCHA: (
                ".button.submit",
                ".submit",
                'div[class*="button"]',
            ),
            CaptchaType.RECAPTCHA_V2: (
                "#recaptcha-verify-button",
                ".rc-button-submit",
            ),
        }
        selectors = selector_map.get(info.type)
        if selectors is None or info.container_selector is None:
            return
        try:
            frame = page.frame_locator(info.container_selector)
            for sel in selectors:
                try:
                    btn = frame.locator(sel).first
                    btn.click(timeout=2000)
                    return
                except Exception:
                    continue
        except Exception:
            return

    def _solve_unknown(self, page: Any, info: CaptchaInfo) -> bool:
        return False

    # -- iframe 内元素定位 -------------------------------------------------
    @staticmethod
    def _find_hcaptcha_checkbox(page: Any) -> Any | None:
        # 复选框在 hCaptcha 的 iframe 内，通过 frame_locator 取
        try:
            frame = page.frame_locator(_HCAPTCHA_IFRAME)
            return frame.locator("#checkbox").element_handle(timeout=2000)
        except Exception:
            return None

    @staticmethod
    def _find_recaptcha_anchor(page: Any) -> Any | None:
        try:
            frame = page.frame_locator(_RECAPTCHA_IFRAME)
            return frame.locator("#recaptcha-anchor").element_handle(timeout=2000)
        except Exception:
            return None

    # -- 人类化操作 --------------------------------------------------------
    def _humanize_click(self, page: Any, selector: Any) -> None:
        """人类化点击：贝塞尔曲线移动 + 随机偏移(±5px) + 随机延迟(0.5~2s)。"""
        try:
            box = selector.bounding_box()
        except Exception:
            box = None
        if box is None:
            # 取不到坐标时退化为元素原生点击
            try:
                selector.click()
            except Exception:
                return
            if self.humanize:
                time.sleep(random.uniform(0.5, 2.0))
            return
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        # 随机偏移 ±5px
        tx = cx + random.uniform(-5.0, 5.0)
        ty = cy + random.uniform(-5.0, 5.0)
        if self.humanize:
            self._bezier_move(page, tx, ty)
            time.sleep(random.uniform(0.1, 0.35))
        page.mouse.click(tx, ty)
        if self.humanize:
            # 操作间随机延迟 0.5~2s
            time.sleep(random.uniform(0.5, 2.0))

    def _humanize_drag(
        self,
        page: Any,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> None:
        """人类化拖拽：贝塞尔曲线轨迹，多个中间点。"""
        page.mouse.move(start[0], start[1])
        time.sleep(random.uniform(0.1, 0.3))
        page.mouse.down()
        time.sleep(random.uniform(0.1, 0.25))
        for px, py in self._bezier_points(start, end, steps=24):
            page.mouse.move(px, py, steps=1)
            # 每步 12~40ms 抖动，模拟手部不规则速率
            time.sleep(random.uniform(0.012, 0.04))
        time.sleep(random.uniform(0.1, 0.3))
        page.mouse.up()

    def _bezier_move(self, page: Any, x: float, y: float) -> None:
        """用贝塞尔曲线把鼠标移到 (x, y)。"""
        # 起点取屏幕左上区域内的随机位置，更接近真实鼠标轨迹
        start = (
            random.uniform(100.0, 400.0),
            random.uniform(100.0, 400.0),
        )
        for px, py in self._bezier_points(start, (x, y), steps=18):
            page.mouse.move(px, py, steps=1)
            time.sleep(random.uniform(0.008, 0.03))

    @staticmethod
    def _bezier_points(
        start: tuple[float, float],
        end: tuple[float, float],
        steps: int,
    ) -> list[tuple[float, float]]:
        """生成三次贝塞尔曲线的离散点（含起止两端）。"""
        mx = (start[0] + end[0]) / 2
        my = (start[1] + end[1]) / 2
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        # 垂直方向单位向量，用于把控制点偏移到连线两侧，造成弯曲
        nx = -dy
        ny = dx
        norm = math.hypot(nx, ny)
        if norm < 1e-6:
            ux, uy = 0.0, 0.0
        else:
            ux, uy = nx / norm, ny / norm
        c1 = (
            mx + ux * random.uniform(-80.0, 80.0),
            my + uy * random.uniform(-80.0, 80.0),
        )
        c2 = (
            mx + ux * random.uniform(-40.0, 40.0),
            my + uy * random.uniform(-40.0, 40.0),
        )
        points: list[tuple[float, float]] = []
        for i in range(steps + 1):
            t = i / steps
            mt = 1.0 - t
            x = (
                mt * mt * mt * start[0]
                + 3 * mt * mt * t * c1[0]
                + 3 * mt * t * t * c2[0]
                + t * t * t * end[0]
            )
            y = (
                mt * mt * mt * start[1]
                + 3 * mt * mt * t * c1[1]
                + 3 * mt * t * t * c2[1]
                + t * t * t * end[1]
            )
            points.append((x, y))
        return points

    # -- token 等待 --------------------------------------------------------
    def _wait_for_token(
        self,
        page: Any,
        info: CaptchaInfo,
        timeout: float,
    ) -> bool:
        """等待验证码 token 出现在页面上。"""
        deadline = time.monotonic() + max(0.0, timeout)
        fields = _TOKEN_FIELDS.get(info.type)
        if fields is None:
            # 极验/未知类型：用通用成功标记兜底
            while time.monotonic() < deadline:
                if _first_query(page, _GEETEST_SUCCESS_SELECTORS) is not None:
                    return True
                time.sleep(0.5)
            return False
        while time.monotonic() < deadline:
            for name in fields:
                # reCAPTCHA/hCaptcha/Turnstile 的标准 token 写入位置是
                # textarea/input[name=...]；动态值需读 .value 而非属性
                for tag in ("textarea", "input"):
                    try:
                        el = page.query_selector(f'{tag}[name="{name}"]')
                    except Exception:
                        el = None
                    if el is None:
                        continue
                    try:
                        val = el.evaluate("el => el && el.value || ''")
                    except Exception:
                        val = el.get_attribute("value") or ""
                    if val:
                        return True
            time.sleep(0.5)
        return False


class CaptchaManager:
    """组合检测 + 处理：一条龙服务。"""

    def __init__(
        self,
        solver: CaptchaSolver | None = None,
        *,
        image_solver: ImageCaptchaSolver | None = None,
    ) -> None:
        self.detector = CaptchaDetector()
        # image_solver 优先注入到现有 solver；若同时传 solver 则只接受 solver 内的
        if solver is not None:
            self.solver = solver
            # 显式注入 image_solver 到已传入的 solver（覆盖）
            if image_solver is not None:
                self.solver.image_solver = image_solver
        else:
            self.solver = CaptchaSolver(image_solver=image_solver)
        self.image_solver = image_solver

    def handle(self, page: Any) -> bool:
        """检测并处理当前页面上的验证码。

        返回 ``True`` 表示处理成功或无需处理；``False`` 表示需要人工介入。
        """
        info = self.detector.detect(page)
        if info is None or info.type is CaptchaType.NONE:
            return True
        return self.solver.solve(page, info)


__all__ = [
    "CaptchaDetector",
    "CaptchaInfo",
    "CaptchaManager",
    "CaptchaSolver",
    "CaptchaType",
]
