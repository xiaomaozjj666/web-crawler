"""合规优先的 AI 爬虫。

把现有的 fetcher（JS 渲染 :class:`DynamicFetcher` 或普通 :class:`Fetcher`）
和 :class:`~web_crawler.ai.extractor.AIExtractor` 串成一个小型爬取循环：

- 遵循 ``robots.txt``（默认 ``respect_robots=True``），
- 在请求之间应用可配置的最小间隔（限速），
- 遇到 ``429 Too Many Requests`` / ``503`` 响应时退避重试，存在
  ``Retry-After`` 时按其等待 — 即主动*放慢*，而不是试图突破站点的限流。

这是一个通用自动化便利层；不做逆向、hook 或伪造任何请求签名。

示例
----
>>> from web_crawler import AIScrapeAgent
>>> agent = AIScrapeAgent(render=True)          # DeepSeek-V4-Pro + Playwright
>>> result = agent.scrape("https://example.com", {"title": "page heading"})
>>> result.data
{'title': 'Example Domain'}
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from typing_extensions import Self

from .extractor import AIExtractor, ExtractionResult
from .llm import LLMProvider

if TYPE_CHECKING:
    from ..response import Response

# 遇到这些状态码时执行退避重试，而不是绕过限流
_BACKOFF_STATUS = frozenset({429, 503})

# 拦截页面的 HTTP 状态码（鉴权/禁止访问）
_BLOCK_STATUS = frozenset({401, 403})

# Retry-After 头 / 指数退避的上限（秒）：防止恶意响应头或长时间故障拖死爬虫
_MAX_RETRY_AFTER = 300.0
_MAX_BACKOFF = 60.0

# 反爬/验证码/人机验证的页面正文标记（小写匹配）。
# 命中反爬检测标记即停下并移交人工处理
_BLOCK_MARKERS: tuple[str, ...] = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "challenges.cloudflare.com",
    "just a moment",
    "unusual traffic",
    "verify you are human",
    "are you a robot",
    "人机验证",
    "滑动验证",
    "请完成验证",
    "安全验证",
)


def detect_block(resp: Response) -> str | None:
    """若 ``resp`` 看起来是反爬墙，返回人类可读的原因。

    刻意保守：仅鉴权/禁止访问状态码，或正文中出现明确的验证码/反爬
    标记才算命中。其余情况返回 ``None``。
    """
    if resp.status in _BLOCK_STATUS:
        return f"http {resp.status}"
    body = resp.text[:20000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in body:
            return f"page marker: {marker!r}"
    return None


def _http_get_text(
    url: str,
    timeout: float = 10.0,
    user_agent: str = "web-crawler",
) -> str:
    """轻量 GET（stdlib only）：仅用于 robots.txt 等元数据拉取。

    不走重型 fetcher / 渲染，失败返回空串（等价于"无 robots 约束"）。
    """
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


@dataclass
class ScrapeResult:
    """:meth:`AIScrapeAgent.scrape` 调用的结果。"""

    url: str
    status: int
    data: dict[str, Any]
    selectors: dict[str, str]
    missing: list[str]
    response: Response
    # BrowserAct 式人工移交：命中反爬/验证码时置位，抓取被主动跳过。
    needs_human: bool = False
    block_reason: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400 and not self.missing and not self.needs_human


class RobotsPolicy:
    """带按主机缓存的小型 ``robots.txt`` 闸门（仅用标准库）。"""

    def __init__(self, user_agent: str = "*") -> None:
        self.user_agent = user_agent
        self._cache: dict[str, robotparser.RobotFileParser] = {}

    def _parser_for(self, url: str, fetch_text: Any) -> robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host in self._cache:
            return self._cache[host]
        robots_url = urljoin(host, "/robots.txt")
        rp = robotparser.RobotFileParser()
        try:
            text = fetch_text(robots_url)
            rp.parse(text.splitlines())
        except Exception:
            rp = robotparser.RobotFileParser()
            rp.parse([])
        self._cache[host] = rp
        return rp

    def allowed(self, url: str, fetch_text: Any) -> bool:
        rp = self._parser_for(url, fetch_text)
        if rp is None:  # pragma: no cover - _parser_for 始终返回 RobotFileParser
            return True
        return rp.can_fetch(self.user_agent, url)


class AIScrapeAgent:
    """AI 辅助爬虫，自带速率控制。

    Parameters
    ----------
    fetcher:
        任意带 ``get(url)`` 或 ``fetch(url)`` 方法且返回 :class:`Response`
        的对象。缺省时延迟创建 fetcher（``render=True`` 用
        :class:`DynamicFetcher`，否则 :class:`Fetcher`）。
    render:
        对 JS 密集页面使用 Playwright 后端的 :class:`DynamicFetcher`。
    provider:
        用于抽取的 LLM 供应商（默认 DeepSeek / ``DeepSeek-V4-Pro``）。
    min_delay:
        相邻请求之间的最小间隔秒数（限速）。
    respect_robots:
        跳过 ``robots.txt`` 禁止的 URL。
    max_retries:
        遇 ``429``/``503`` 时的指数退避重试次数。
    user_agent:
        robots.txt 检查使用的 User-Agent 字符串。
    detect_blocks:
        检测反爬/验证码墙并移交人工，而非尝试绕过（受 BrowserAct 启发
        的负责任变体）。
    on_block:
        检测到拦截时以 :class:`ScrapeResult` 为参数调用的可选回调
        （例如通知运维或打开手动浏览器）。
    """

    def __init__(
        self,
        fetcher: Any | None = None,
        *,
        render: bool = False,
        provider: LLMProvider | None = None,
        extractor: AIExtractor | None = None,
        model: str | None = None,
        min_delay: float = 1.0,
        respect_robots: bool = True,
        max_retries: int = 3,
        user_agent: str = "web-crawler",
        detect_blocks: bool = True,
        on_block: Callable[[ScrapeResult], None] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._render = render
        self.extractor = extractor or AIExtractor(provider=provider, model=model)
        self.min_delay = min_delay
        self.respect_robots = respect_robots
        self.max_retries = max_retries
        self.robots = RobotsPolicy(user_agent)
        self.detect_blocks = detect_blocks
        self.on_block = on_block
        self._last_request_ts = 0.0

    # -- fetcher plumbing ---------------------------------------------------
    def _ensure_fetcher(self) -> Any:
        if self._fetcher is not None:
            return self._fetcher
        if self._render:
            from ..fetchers.dynamic import DynamicFetcher

            self._fetcher = DynamicFetcher()
        else:
            from ..fetchers.fetcher import Fetcher

            self._fetcher = Fetcher()
        return self._fetcher

    @staticmethod
    def _do_fetch(fetcher: Any, url: str) -> Response:
        # Fetcher 与 DynamicFetcher 均提供 get（后者为动词统一别名）
        return fetcher.get(url)

    def _fetch_text(self, url: str) -> str:
        resp = self._do_fetch(self._ensure_fetcher(), url)
        return resp.text

    def _fetch_robots_text(self, url: str) -> str:
        """轻量拉取 robots.txt（stdlib HTTP，不经重型 fetcher/渲染），并纳入限速。"""
        self._throttle()
        text = _http_get_text(url, user_agent=self.robots.user_agent)
        self._last_request_ts = time.monotonic()
        return text

    # -- rate limiting / backoff -------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        wait = self.min_delay - elapsed
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _retry_after(resp: Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        if header:
            try:
                # Retry-After 来自不可信响应头，设上限防恶意大值拖死爬虫
                return max(0.0, min(float(header), _MAX_RETRY_AFTER))
            except ValueError:
                pass
        # 指数退避兜底（同样设上限）
        return min(float(2**attempt), _MAX_BACKOFF)

    def fetch(self, url: str) -> Response:
        """礼貌地抓取 ``url``：robots 检查、限速、429/503 退避。"""
        fetcher = self._ensure_fetcher()
        if self.respect_robots and not self.robots.allowed(url, self._fetch_robots_text):
            raise PermissionError(f"robots.txt disallows fetching {url!r}")

        attempt = 0
        while True:
            self._throttle()
            resp = self._do_fetch(fetcher, url)
            self._last_request_ts = time.monotonic()
            if resp.status in _BACKOFF_STATUS and attempt < self.max_retries:
                time.sleep(self._retry_after(resp, attempt))
                attempt += 1
                continue
            return resp

    # -- high-level API -----------------------------------------------------
    def scrape(
        self,
        url: str,
        schema: dict[str, str],
        *,
        self_heal: bool = True,
    ) -> ScrapeResult:
        """抓取 ``url`` 并用 AI 抽取器抽取 ``schema`` 字段。

        若 ``detect_blocks`` 开启且页面看起来是反爬/验证码墙，则跳过抽取，
        返回 ``needs_human=True`` 的 :class:`ScrapeResult`（并调用
        ``on_block``）。
        """
        resp = self.fetch(url)
        if self.detect_blocks:
            reason = detect_block(resp)
            if reason is not None:
                blocked = ScrapeResult(
                    url=resp.url,
                    status=resp.status,
                    data={},
                    selectors={},
                    missing=list(schema),
                    response=resp,
                    needs_human=True,
                    block_reason=reason,
                )
                if self.on_block is not None:
                    self.on_block(blocked)
                return blocked
        extracted: ExtractionResult = self.extractor.extract(resp, schema, self_heal=self_heal)
        return ScrapeResult(
            url=resp.url,
            status=resp.status,
            data=extracted.data,
            selectors=extracted.selectors,
            missing=extracted.missing,
            response=resp,
        )

    def close(self) -> None:
        """若底层 fetcher 持有可关闭的资源，则将其关闭。"""
        if self._fetcher is not None and hasattr(self._fetcher, "close"):
            self._fetcher.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["AIScrapeAgent", "RobotsPolicy", "ScrapeResult", "detect_block"]
