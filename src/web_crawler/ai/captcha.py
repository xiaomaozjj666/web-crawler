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

import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from typing import Any

    from .image_captcha import ImageCaptchaSolver

logger = logging.getLogger(__name__)


class CaptchaType(Enum):
    """已知的验证码类型。"""

    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    GEETEST = "geetest"
    YIDUN = "yidun"
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

# 易盾容器探测：bg 图/滑块手柄命中其一即视为易盾
_YIDUN_SELECTORS: tuple[str, ...] = (
    "img.yidun_bg-img",
    ".yidun_slider",
    '[class*="yidun_bg-img"]',
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
        except Exception as exc:
            logger.debug("selector probe failed for %r: %s", sel, exc)
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
            self._detect_yidun,
        ):
            info = fn(page)
            if info is not None:
                return info
        return None

    # -- 各类型分支 --------------------------------------------------------
    def _detect_hcaptcha(self, page: Any) -> CaptchaInfo | None:
        try:
            iframe = page.query_selector(_HCAPTCHA_IFRAME)
        except Exception as exc:
            logger.debug("hcaptcha iframe probe failed: %s", exc)
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
            except Exception as exc:
                logger.debug("turnstile iframe probe failed for %r: %s", sel, exc)
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
        except Exception as exc:
            logger.debug("recaptcha iframe probe failed: %s", exc)
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
            except Exception as exc:
                logger.debug("geetest container probe failed for %r: %s", sel, exc)
                el = None
            if el is not None:
                return CaptchaInfo(
                    type=CaptchaType.GEETEST,
                    container_selector=sel,
                )
        return None

    def _detect_yidun(self, page: Any) -> CaptchaInfo | None:
        """易盾（网易智企）：bg 图/滑块手柄命中即判定。"""
        for sel in _YIDUN_SELECTORS:
            try:
                el = page.query_selector(sel)
            except Exception as exc:
                logger.debug("yidun container probe failed for %r: %s", sel, exc)
                el = None
            if el is not None:
                return CaptchaInfo(
                    type=CaptchaType.YIDUN,
                    container_selector=sel,
                )
        return None

    async def _detect_yidun_async(self, page: Any) -> CaptchaInfo | None:
        """易盾检测异步版（镜像 _query_async 模式）。"""
        for sel in _YIDUN_SELECTORS:
            el = await self._query_async(page, sel, what="yidun container")
            if el is not None:
                return CaptchaInfo(
                    type=CaptchaType.YIDUN,
                    container_selector=sel,
                )
        return None

    @staticmethod
    def _read_data_sitekey(page: Any) -> str | None:
        """从页面上任意带 data-sitekey 的元素读取 sitekey。"""
        try:
            el = page.query_selector("[data-sitekey]")
        except Exception as exc:
            logger.debug("data-sitekey element probe failed: %s", exc)
            return None
        if el is None:
            return None
        return el.get_attribute("data-sitekey")

    # -- 异步页面版本（Playwright async API） --------------------------------
    # 两套 API 只差一个 await，但无法共用同一份实现：把同步分支喂给异步 page
    # 时 page.query_selector() 返回的是协程，随后 el.get_attribute(...) 必然抛
    # AttributeError（Nightly e2e 实测每步 observe 都以 "'coroutine' object has
    # no attribute 'get_attribute'" 失败）。故并列一份全程 await 的实现，
    # 语义与同步版逐条对应。

    async def detect_async(self, page: Any) -> CaptchaInfo | None:
        """异步页面版 :meth:`detect`：判定顺序与返回语义完全一致。"""
        for fn in (
            self._detect_hcaptcha_async,
            self._detect_turnstile_async,
            self._detect_recaptcha_async,
            self._detect_geetest_async,
            self._detect_yidun_async,
        ):
            info = await fn(page)
            if info is not None:
                return info
        return None

    @staticmethod
    async def _query_async(page: Any, selector: str, *, what: str) -> Any | None:
        """``page.query_selector`` 的 await 版；探测失败按未命中处理。"""
        try:
            return await page.query_selector(selector)
        except Exception as exc:
            logger.debug("%s probe failed for %r: %s", what, selector, exc)
            return None

    async def _read_data_sitekey_async(self, page: Any) -> str | None:
        el = await self._query_async(page, "[data-sitekey]", what="data-sitekey element")
        if el is None:
            return None
        value: str | None = await el.get_attribute("data-sitekey")
        return value

    async def _detect_hcaptcha_async(self, page: Any) -> CaptchaInfo | None:
        iframe = await self._query_async(page, _HCAPTCHA_IFRAME, what="hcaptcha iframe")
        if iframe is None:
            return None
        src: str = await iframe.get_attribute("src") or ""
        site_key = await self._read_data_sitekey_async(page) or _sitekey_from_url(src, "sitekey")
        return CaptchaInfo(
            type=CaptchaType.HCAPTCHA,
            iframe_url=src or None,
            site_key=site_key,
            container_selector=_HCAPTCHA_IFRAME,
        )

    async def _detect_turnstile_async(self, page: Any) -> CaptchaInfo | None:
        iframe = None
        matched_sel: str | None = None
        for sel in _TURNSTILE_IFRAMES:
            iframe = await self._query_async(page, sel, what="turnstile iframe")
            if iframe is not None:
                matched_sel = sel
                break
        if iframe is None:
            return None
        src: str = await iframe.get_attribute("src") or ""
        site_key = await self._read_data_sitekey_async(page) or _sitekey_from_url(src, "sitekey")
        return CaptchaInfo(
            type=CaptchaType.TURNSTILE,
            iframe_url=src or None,
            site_key=site_key,
            container_selector=matched_sel,
        )

    async def _detect_recaptcha_async(self, page: Any) -> CaptchaInfo | None:
        iframe = await self._query_async(page, _RECAPTCHA_IFRAME, what="recaptcha iframe")
        if iframe is None:
            return None
        src: str = await iframe.get_attribute("src") or ""
        is_v3 = ("/enterprise/" in src and "/anchor" not in src) or (
            "render=" in src and "render=explicit" not in src
        )
        if is_v3:
            site_key = _sitekey_from_url(src, "render", "k")
            captcha_type = CaptchaType.RECAPTCHA_V3
        else:
            site_key = await self._read_data_sitekey_async(page) or _sitekey_from_url(src, "k")
            captcha_type = CaptchaType.RECAPTCHA_V2
        return CaptchaInfo(
            type=captcha_type,
            iframe_url=src or None,
            site_key=site_key,
            container_selector=_RECAPTCHA_IFRAME,
        )

    async def _detect_geetest_async(self, page: Any) -> CaptchaInfo | None:
        for sel in _GEETEST_SELECTORS:
            el = await self._query_async(page, sel, what="geetest container")
            if el is not None:
                return CaptchaInfo(type=CaptchaType.GEETEST, container_selector=sel)
        return None


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
            CaptchaType.YIDUN: self._solve_yidun,
            CaptchaType.UNKNOWN: self._solve_unknown,
        }
        handler = dispatch.get(info.type)
        if handler is None:  # pragma: no cover - 所有 CaptchaType 已在 dispatch 中覆盖
            return False
        try:
            return handler(page, info)
        except Exception as exc:
            logger.warning("captcha solver failed for %s: %s", info.type.value, exc)
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
        # 2026-11-14 Google 官方 demo 实测：风控拦截时挑战 iframe 永远不进
        # 可视区（spinner 挂起、bframe 停在屏外 y=-9999），图片挑战流程无从
        # 触达。此时提前显式失败并注明原因，不再空跑图片挑战等待。
        if not self._recaptcha_challenge_visible(page):
            logger.warning(
                "reCAPTCHA challenge iframe never became visible after the "
                "checkbox click (risk-gated?); skipping image challenge, "
                "falling back to human"
            )
            return False
        if self.image_solver is not None and self._solve_image_challenge(page, info):
            return self._wait_for_token(page, info, timeout=self.max_wait * 0.5)
        return False

    def _recaptcha_challenge_visible(self, page: Any) -> bool:
        """bframe 挑战 iframe 是否出现在可视区（停在屏外 = 风控未放行）。"""
        frame_el = _first_query(page, ('iframe[src*="bframe"]', "iframe[title*='验证']"))
        if frame_el is None:
            return False
        try:
            box = frame_el.bounding_box()
            if box is None:
                return False
            return float(box.get("y", -9999.0)) > -100.0 and float(box.get("width", 0.0)) > 100.0
        except Exception as exc:
            logger.debug("recaptcha bframe bounding_box failed: %s", exc)
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
        except Exception as exc:
            logger.warning("geetest slider bounding_box failed: %s", exc)
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

    def _solve_yidun(self, page: Any, info: CaptchaInfo) -> bool:
        """易盾滑块：URL 抓原图（带 alpha）→ 定位 → 拖拽（1:1）→ 读判定。

        2026-11-14 Camoufox/IAB 双栈实测要点：
        - 元素截图是合成像素（透明区透出背景），会毁掉件的 alpha 掩码——
          必须按 img.src 抓 PNG 原图（FileReader 在页面内转 dataURL）。
        - 页面可能有触发式/嵌入式/弹出式多个实例，bg/handle 全部按
          "可见矩形"筛选，拖拽前先 scrollIntoView（折叠线下鼠标事件
          会被视口裁掉）。
        - 判定读容器 tips：成功 / 换图（失败自动刷新）均可在 max_wait
          内观测。
        """
        if self.image_solver is None:
            return False
        # 1) 定位可见实例的 bg/piece/handle
        geo = page.evaluate(
            "(() => { const b=[...document.querySelectorAll('img.yidun_bg-img')].find(b=>{const r=b.getBoundingClientRect(); return r.width>50;}); if (!b) return null; const w=b.closest('.yidun'); const p=w && w.querySelector('img.yidun_jigsaw'); const h=w && w.querySelector('.yidun_slider'); if (!p || !h) return null; const hr=h.getBoundingClientRect(); return { bg: b.src, piece: p.src, hx: hr.x + hr.width/2, hy: hr.y + hr.height/2 }; })()"
        )
        if not geo:
            return False
        # 2) 按按 URL 抓原图（FileReader 页面内转 dataURL，绕开 Xray 限制）
        import base64 as b64mod

        data_url = page.evaluate(
            "async (u) => { const r = await fetch(u); const blob = await r.blob(); return await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob); }); }",
            geo["bg"],
        )
        piece_url = page.evaluate(
            "async (u) => { const r = await fetch(u); const blob = await r.blob(); return await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob); }); }",
            geo["piece"],
        )
        bg_bytes = b64mod.b64decode(data_url.split(",", 1)[1])
        piece_bytes = b64mod.b64decode(piece_url.split(",", 1)[1])

        # 3) 定位
        try:
            sol = self.image_solver.solve_slider(bg_bytes, piece_bytes)
        except Exception as exc:
            logger.warning("yidun slider recognition failed: %s", exc)
            return False
        if sol is None:
            return False

        # 4) 滚进视口 + 拖拽（件左缘 = 拖距，1:1）
        page.evaluate(
            "(() => { const b=[...document.querySelectorAll('img.yidun_bg-img')].find(b=>{const r=b.getBoundingClientRect(); return r.width>50;}); if (b) b.scrollIntoView({block: 'center'}); return 1; })()"
        )
        handle_box = page.evaluate(
            "(() => { const w=[...document.querySelectorAll('.yidun')].find(w=>w.querySelector('img.yidun_bg-img')); const h=w && w.querySelector('.yidun_slider'); if (!h) return null; const r=h.getBoundingClientRect(); return { x: r.x + r.width/2, y: r.y + r.height/2 }; })()"
        )
        if not handle_box:
            return False
        start = (float(handle_box["x"]), float(handle_box["y"]))
        self._humanize_drag(page, start, (start[0] + sol.x, start[1]))

        # 5) 判定：tips 出现「成功」→ True；换图/回到「向右拖动」→ False
        deadline = time.monotonic() + self.max_wait
        bg_before = geo["bg"]
        while time.monotonic() < deadline:
            state = page.evaluate(
                "(() => { const w=[...document.querySelectorAll('.yidun')].find(w=>w.querySelector('img.yidun_bg-img')); const t=w && w.querySelector('.yidun_tips__text'); const b=w && w.querySelector('img.yidun_bg-img'); return { tips: t ? t.textContent.trim() : '', bg: b ? b.src.slice(-24) : '' }; })()"
            )
            if state:
                if "成功" in state["tips"]:
                    return True
                if state["bg"] and state["bg"] != bg_before[-24:]:
                    return False  # 校验失败自动换图
                if "向右拖动" in state["tips"]:
                    return False
            time.sleep(0.5)
        return False

    def _geetest_detect_offset(self, page: Any) -> float | None:
        """用 image_solver 识别极验拼图缺口 x 偏移；失败返回 None。

        2026-11-14 对极验官方 demo 实测修正：拼图件是 ``.geetest_canvas_slice``
        （与背景同宽的全幅 canvas），背景是 ``.geetest_canvas_bg``。旧实现
        截 ``.geetest_slider_button``（拖拽手柄）当拼图件、``.geetest_panel``
        （控件外壳）当背景，模板匹配对象完全错误——手柄是 66×66 的圆形
        图标，与拼图内容无关。
        """
        if self.image_solver is None:
            return None
        bg_el = _first_query(page, (".geetest_canvas_bg", ".geetest_window"))
        if bg_el is None:
            return None
        try:
            bg_bytes = bg_el.screenshot()
        except Exception as exc:
            logger.warning("failed to screenshot geetest background canvas: %s", exc)
            return None
        piece_el = _first_query(page, (".geetest_canvas_slice", ".geetest_slice"))
        if piece_el is None:
            return None
        try:
            piece_bytes = piece_el.screenshot()
        except Exception as exc:
            logger.warning("failed to screenshot geetest slice canvas: %s", exc)
            return None
        try:
            sol = self.image_solver.solve_slider(bg_bytes, piece_bytes)
        except Exception as exc:
            logger.warning("slider gap recognition failed: %s", exc)
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
        except Exception as exc:
            logger.warning("challenge iframe bounding_box failed: %s", exc)
            box = None
        if box is None:
            return False
        offset_x = float(box.get("x", 0.0))
        offset_y = float(box.get("y", 0.0))
        try:
            img_bytes = frame_handle.screenshot()
        except Exception as exc:
            logger.warning("failed to screenshot challenge iframe: %s", exc)
            return False
        prompt = self._read_challenge_prompt(page, info) or "请按提示点击对应元素"
        # 2026-11-14 hCaptcha 实测：拖拽连线/拖动放置类挑战不是点选范式，
        # 点选求解只会给出错误坐标并烧掉挑战次数——显式跳过交人工。
        if re.search(r"拖动|拖拽|拖至|drag\s", prompt, re.IGNORECASE):
            logger.info(
                "challenge prompt indicates a drag task (%r); click solver skipped",
                prompt[:48],
            )
            return False
        try:
            sol = self.image_solver.solve_click(img_bytes, prompt)
        except Exception as exc:
            logger.warning("click captcha recognition failed: %s", exc)
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
            except Exception as exc:
                logger.warning(
                    "challenge point click failed at (%s, %s): %s",
                    offset_x + px,
                    offset_y + py,
                    exc,
                )
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
        except Exception as exc:
            logger.warning(
                "challenge iframe lookup failed for %r: %s", info.container_selector, exc
            )
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
                except Exception as exc:
                    logger.debug("challenge prompt text probe failed for %r: %s", sel, exc)
                    text = None
                if text:
                    return str(text).strip()
        except Exception as exc:
            logger.warning("challenge frame access failed while reading prompt: %s", exc)
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
                except Exception as exc:
                    logger.debug("submit button probe failed for %r: %s", sel, exc)
                    continue
        except Exception as exc:
            logger.warning("challenge frame access failed while clicking submit: %s", exc)
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
        except Exception as exc:
            logger.warning("failed to locate hcaptcha checkbox in iframe: %s", exc)
            return None

    @staticmethod
    def _find_recaptcha_anchor(page: Any) -> Any | None:
        try:
            frame = page.frame_locator(_RECAPTCHA_IFRAME)
            return frame.locator("#recaptcha-anchor").element_handle(timeout=2000)
        except Exception as exc:
            logger.warning("failed to locate recaptcha anchor in iframe: %s", exc)
            return None

    # -- 人类化操作 --------------------------------------------------------
    def _humanize_click(self, page: Any, selector: Any) -> None:
        """人类化点击：贝塞尔曲线移动 + 随机偏移(±5px) + 随机延迟(0.5~2s)。"""
        try:
            box = selector.bounding_box()
        except Exception as exc:
            logger.debug("bounding_box unavailable, falling back to native click: %s", exc)
            box = None
        if box is None:
            # 取不到坐标时退化为元素原生点击
            try:
                selector.click()
            except Exception as exc:
                logger.warning("native element click failed: %s", exc)
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
                    except Exception as exc:
                        logger.debug("token field probe failed for %s[name=%s]: %s", tag, name, exc)
                        el = None
                    if el is None:
                        continue
                    try:
                        val = el.evaluate("el => el && el.value || ''")
                    except Exception as exc:
                        logger.debug(
                            "token value evaluate failed; falling back to attribute: %s", exc
                        )
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

    def solve_url(
        self,
        url: str,
        *,
        headless: bool = False,
        fetch_timeout: float = 180.0,
        fetcher_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """一键式入口：Camoufox（反指纹 Firefox）打开 ``url`` → detect → solve。

        把 detect/solve 路由、image_solver（math_mode/bbox 协议/剪影探测器）
        与 Camoufox 拟人输入串成一条主线——调用方不再自己拼装浏览器和流程。

        返回结构化结果 dict：``url`` / ``type``（验证码类型；``"none"`` 表示
        页面无验证码）/ ``solved``（True=通过，False=需人工，None=页面无验证
        码）/ ``steps``（过程事件）。

        需要 ``camoufox`` 可选依赖（``pip install web-crawler[camoufox]`` 后
        ``camoufox fetch`` 拉取浏览器），未安装时抛 ImportError 带安装指引。
        """
        from ..compat import require_camoufox
        from ..fetchers.camoufox import CamoufoxFetcher

        require_camoufox()

        holder: dict[str, Any] = {"url": url, "type": None, "solved": None, "steps": []}

        def action(page: Any) -> None:
            try:
                info = self.detector.detect(page)
            except Exception as exc:
                holder["steps"].append(f"detect-fail: {exc}")
                holder["type"] = CaptchaType.UNKNOWN.value
                holder["solved"] = False
                return
            if info is None:
                holder["type"] = CaptchaType.NONE.value
                holder["solved"] = None
                return
            holder["type"] = info.type.value
            if info.type is CaptchaType.NONE:
                holder["solved"] = None
                return
            try:
                holder["solved"] = self.solver.solve(page, info)
            except Exception as exc:
                logger.warning("solve_url solve failed for %s: %s", info.type.value, exc)
                holder["solved"] = False
            holder["steps"].append(f"solved[{info.type.value}]={holder['solved']}")

        kwargs = dict(fetcher_kwargs or {})
        with CamoufoxFetcher(
            os="windows",
            humanize=True,
            headless=headless,
            timeout=fetch_timeout,
            page_action=action,
            **kwargs,
        ) as fetcher:
            fetcher.fetch(url)
        return holder


__all__ = [
    "CaptchaDetector",
    "CaptchaInfo",
    "CaptchaManager",
    "CaptchaSolver",
    "CaptchaType",
]
