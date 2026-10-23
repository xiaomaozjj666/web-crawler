"""交互式调试 shell（对标 ``scrapy shell``）。

用法::

    web-crawler-shell https://example.com
    python -m web_crawler.app.shell https://example.com

启动即用库的 :class:`~web_crawler.Fetcher`（默认隐身 chrome131）抓取目标
URL 并打印响应摘要（状态 / URL / 最终 URL / 内容长度 / 标题），随后进入
交互循环。命令集：

- ``css <选择器>``（无前缀默认按 CSS 处理）
- ``xpath <表达式>``
- ``fetch <url>`` —— 换目标重新抓取
- ``links`` —— 列出当前页面链接（前 20 条）
- ``help`` / ``quit`` / ``exit``（Ctrl+C / Ctrl+D 优雅退出）

架构：:func:`parse_command` 是纯函数（一行输入 → 动作对象），
:class:`ShellSession` 持有可变状态并执行动作（返回待打印文本），
:func:`run_shell` 只是 ``input``/``print`` 的交互薄壳——核心逻辑无需
模拟 stdin 即可单测。SSRF 安全边界保持：shell 走 Fetcher 正常链路，
私网拦截自动生效（错误消息人话化）。抓取失败不退出，留在 shell 里
可继续 ``fetch`` 其他 URL。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from string import Template
from typing import Any, Protocol, TypeAlias

from web_crawler import Fetcher
from web_crawler.exceptions import UnsafeTargetError, WebCrawlerError
from web_crawler.parser.links import LinkExtractor
from web_crawler.response import Response

# 结果展示上限（对齐 scrapy shell 的直观性：够看、不刷屏）
MAX_DISPLAY = 20

# 每条选择器结果的文本/属性摘要截断长度
_TEXT_LIMIT = 60
_ATTR_LIMIT = 60

# _describe_node 里附带展示的元素属性（命中才显示）
_ATTR_KEYS = ("href", "src", "title", "alt", "name", "id")

_FETCH_HINT = (
    "建议：检查 URL 拼写与 scheme（http/https）、确认网络连通；"
    "部分站点需要代理（Fetcher(proxy=...)）或浏览器渲染（DynamicFetcher）。"
    "仍在 shell 中，可 fetch 其他 URL 重试。"
)

HELP_TEXT = """命令清单:
  css <选择器>     CSS 选择器查询，如 css h1 / css "div.item a"
  xpath <表达式>   XPath 查询，如 xpath //h1
  <选择器>         无前缀默认按 CSS 处理，如直接输入 h1
  fetch <url>      抓取新页面并更新当前响应
  links            列出当前页面的超链接（前 20 条）
  help / ?         显示本帮助
  quit / exit      退出 shell（Ctrl+C / Ctrl+D 亦可）"""

_SUMMARY_HEAD = Template(
    """── 响应摘要 ─────────────────────────────────────────
状态:     $status $ok
URL:      $url"""
)


# ── 命令对象（纯数据） ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class CssCommand:
    """CSS 选择器查询。"""

    query: str


@dataclass(frozen=True)
class XPathCommand:
    """XPath 查询。"""

    query: str


@dataclass(frozen=True)
class FetchCommand:
    """抓取新 URL 并切换当前响应。"""

    url: str


@dataclass(frozen=True)
class LinksCommand:
    """列出当前页面链接。"""


@dataclass(frozen=True)
class HelpCommand:
    """显示命令清单。"""


@dataclass(frozen=True)
class QuitCommand:
    """退出 shell。"""


@dataclass(frozen=True)
class NoopCommand:
    """空行：什么都不做。"""


@dataclass(frozen=True)
class UnknownCommand:
    """认识的动词但缺参数（如裸 ``fetch``）：提示用法。"""

    verb: str


Command: TypeAlias = (
    CssCommand
    | XPathCommand
    | FetchCommand
    | LinksCommand
    | HelpCommand
    | QuitCommand
    | NoopCommand
    | UnknownCommand
)


def _strip_quotes(text: str) -> str:
    """查询整体被同类引号包围时剥掉一层（``css "div a"`` → ``div a``）。

    只剥首尾成对的同类引号，内部的引号原样保留——XPath 的
    ``//a[@href="x"]`` 这类表达式不会被破坏。
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def parse_command(line: str) -> Command:
    """把一行 shell 输入解析为动作对象（纯函数，无副作用）。

    解析规则：

    - 空白行 → :class:`NoopCommand`；
    - ``quit`` / ``exit`` / ``q`` → :class:`QuitCommand`；``help`` / ``?``
      → :class:`HelpCommand`；``links`` → :class:`LinksCommand`；
    - ``fetch`` / ``css`` / ``xpath`` + 空白 + 查询 → 对应命令；动词后
      缺参数 → :class:`UnknownCommand`（动词只认「后跟空白」的形式，
      ``cssx`` 不会被误当 ``css``）；
    - 其余整行视为 CSS 选择器（scrapy shell 同款默认），查询整体被
      引号包裹时剥一层。
    """
    stripped = line.strip()
    if not stripped:
        return NoopCommand()
    lowered = stripped.lower()
    if lowered in ("quit", "exit", "q"):
        return QuitCommand()
    if lowered in ("help", "?"):
        return HelpCommand()
    if lowered == "links":
        return LinksCommand()
    for verb in ("fetch", "css", "xpath"):
        if lowered.startswith(verb):
            rest = stripped[len(verb) :]
            if not rest or rest[0].isspace():
                query = _strip_quotes(rest.strip())
                if not query:
                    return UnknownCommand(verb)
                if verb == "fetch":
                    return FetchCommand(query)
                if verb == "css":
                    return CssCommand(query)
                return XPathCommand(query)
    return CssCommand(_strip_quotes(stripped))


# ── 展示格式化（纯函数） ─────────────────────────────────────────────────────


def _truncate(text: str, limit: int) -> str:
    """超长文本截断加省略号，ASCII 中间省略保首尾可辨。"""
    if len(text) <= limit:
        return text
    keep = max(1, limit - 1)
    return text[:keep] + "…"


def _page_title(response: Response) -> str:
    """安全提取页面 ``<title>`` 文本（非 HTML / 解析失败返回空串）。"""
    try:
        element = response.css_first("title")
    except Exception:
        return ""
    return str(element.text).strip() if element is not None else ""


def format_summary(requested_url: str, response: Response) -> str:
    """格式化响应摘要：状态 / URL / 最终 URL / 内容长度 / 标题。"""
    lines = _SUMMARY_HEAD.substitute(
        status=response.status,
        ok="OK" if response.ok else "(非 2xx/3xx)",
        url=requested_url,
    ).splitlines()
    if response.url != requested_url:
        lines.append(f"最终 URL: {response.url}（跟随了重定向）")
    lines.append(f"内容长度: {len(response.content)} bytes")
    title = _page_title(response)
    if title:
        lines.append(f"标题:     {_truncate(title, 70)}")
    return "\n".join(lines)


def _describe_node(node: Any) -> str:
    """单条选择器结果的人话摘要：``<tag> 文本 [key=value]``。

    ``css a::attr(href)`` 一类查询返回 str（TextHandler），直接展示文本。
    """
    if isinstance(node, str):
        return _truncate(node.strip(), 80) or "(空)"
    tag = str(getattr(node, "tag", "?"))
    parts = [f"<{tag}>"]
    text = _truncate(" ".join(str(node.text).split()), _TEXT_LIMIT)
    if text:
        parts.append(text)
    for key in _ATTR_KEYS:
        value = node.attr(key)
        if value:
            parts.append(f"{key}={_truncate(str(value), _ATTR_LIMIT)!r}")
    return " ".join(parts)


def format_results(results: Sequence[Any], engine: str, query: str) -> str:
    """格式化选择器查询结果：匹配数 + 每条摘要（最多 MAX_DISPLAY 条）。"""
    total = len(results)
    if total == 0:
        return f"匹配 0 个元素（{engine} {query!r}）。换个选择器试试：css a / xpath //a。"
    lines = [f"匹配 {total} 个元素（{engine} {query!r}）:"]
    for index, node in enumerate(results[:MAX_DISPLAY], 1):
        lines.append(f"[{index}] {_describe_node(node)}")
    if total > MAX_DISPLAY:
        lines.append(f"  ……其余 {total - MAX_DISPLAY} 条已省略")
    return "\n".join(lines)


def format_links(links: Sequence[Any]) -> str:
    """格式化 ``links`` 命令输出：文本 → URL（最多 MAX_DISPLAY 条）。"""
    if not links:
        return "页面上没有 <a href> 链接。"
    lines = [f"共 {len(links)} 条链接（前 {MAX_DISPLAY} 条）:"]
    for index, link in enumerate(links[:MAX_DISPLAY], 1):
        text = _truncate(" ".join(link.text.split()), 40)
        lines.append(f"[{index}] {text or '(无文本)'} -> {link.url}")
    if len(links) > MAX_DISPLAY:
        lines.append(f"  ……其余 {len(links) - MAX_DISPLAY} 条已省略")
    return "\n".join(lines)


# ── 会话状态与命令执行 ───────────────────────────────────────────────────────


class _ShellFetcher(Protocol):
    """shell 驱动一个 fetcher 所需的最小接口（结构化类型）。

    :class:`~web_crawler.Fetcher` 及其全部同族实现天然满足；测试可传
    任意具有 ``get(url)`` 的对象（duck typing）。
    """

    def get(self, url: str, **kwargs: Any) -> Response: ...  # pragma: no cover


@dataclass
class ShellSession:
    """shell 的可变状态与命令执行核心（与交互循环解耦，便于单测）。

    :meth:`execute` 对每个动作返回待打印的文本；``fetch`` 失败时抛出的
    异常被人话化吞掉（不改变当前响应），shell 因此可以换 URL 重试。
    """

    fetcher: _ShellFetcher
    response: Response | None = None
    requested_url: str = ""

    def execute(self, command: Command) -> str:
        """执行动作并返回应打印的文本（纯文本进出，不直接 print）。"""
        if isinstance(command, FetchCommand):
            return self._do_fetch(command.url)
        if isinstance(command, CssCommand):
            return self._do_select("css", command.query)
        if isinstance(command, XPathCommand):
            return self._do_select("xpath", command.query)
        if isinstance(command, LinksCommand):
            return self._do_links()
        if isinstance(command, HelpCommand):
            return HELP_TEXT
        if isinstance(command, UnknownCommand):
            return (
                f"缺少参数。用法：{command.verb} <{'url' if command.verb == 'fetch' else '查询'}>"
            )
        return ""

    def _do_fetch(self, url: str) -> str:
        """抓取 ``url`` 并更新当前响应；失败时人话提示且保留旧响应。"""
        cleaned = url.strip()
        if not cleaned:
            return "用法：fetch <url>"
        try:
            response = self.fetcher.get(cleaned)
        except UnsafeTargetError as exc:
            return (
                f"已拦截：{exc}\n"
                "目标命中 SSRF 防护（私网/环回/链路本地/云元数据地址，"
                "或 DNS 无法解析的 host 均不可抓取），换一个公网 URL 试试。"
            )
        except WebCrawlerError as exc:
            return f"抓取失败：{exc}\n{_FETCH_HINT}"
        except Exception as exc:
            return f"抓取失败（{type(exc).__name__}）：{exc}\n{_FETCH_HINT}"
        self.response = response
        self.requested_url = cleaned
        return format_summary(cleaned, response)

    def _do_select(self, engine: str, query: str) -> str:
        """在当前响应上执行 css/xpath 查询并格式化结果。"""
        if self.response is None:
            return "当前没有已抓取的页面，先执行 fetch <url>（或启动时传入 URL）。"
        try:
            results: list[Any] = (
                list(self.response.css(query))
                if engine == "css"
                else list(self.response.xpath(query))
            )
        except Exception as exc:
            return (
                f"{engine} 查询执行失败：{exc}\n"
                "提示：CSS 用 response.css 语法（如 a[href]），XPath 以 // 开头。"
            )
        return format_results(results, engine, query)

    def _do_links(self) -> str:
        """用 LinkExtractor 列出当前页面的链接。"""
        if self.response is None:
            return "当前没有已抓取的页面，先执行 fetch <url>。"
        try:
            links = LinkExtractor().extract(self.response)
        except Exception as exc:
            return f"链接提取失败：{exc}"
        return format_links(links)


# ── 交互循环（薄壳）与 CLI 入口 ──────────────────────────────────────────────


def run_shell(
    initial_url: str | None,
    fetcher: _ShellFetcher,
    *,
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] | None = None,
) -> int:
    """运行交互循环直到 quit/EOF/Ctrl+C，返回进程退出码（恒 0）。

    无初始 URL 时直接进入空 shell（提示先 ``fetch``，更友好而非报错）；
    初始 URL 抓取失败也留在 shell 里，可换目标重试。
    ``input_fn`` / ``print_fn`` 可注入（缺省运行时解析 builtins，因此
    monkeypatch ``builtins.input`` / ``print`` 对本函数同样生效）。
    """
    read_line = input_fn if input_fn is not None else input
    emit = print_fn if print_fn is not None else print
    session = ShellSession(fetcher=fetcher)
    if initial_url:
        emit(session.execute(FetchCommand(initial_url)))
    else:
        emit("未提供初始 URL——先 fetch <url> 抓一个页面（输入 help 查看命令清单）。")
    while True:
        try:
            line = read_line("\nsh> ")
        except (EOFError, KeyboardInterrupt):
            emit("")
            break
        command = parse_command(line)
        if isinstance(command, QuitCommand):
            break
        if isinstance(command, NoopCommand):
            continue
        output = session.execute(command)
        if output:
            emit(output)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``web-crawler-shell [url]``。正常退出恒返回 0。"""
    parser = argparse.ArgumentParser(
        prog="web-crawler-shell",
        description="交互式调试 shell：抓取页面后用 CSS/XPath 即席查询（对标 scrapy shell）。",
        epilog=(
            "示例:\n"
            "  %(prog)s https://example.com\n"
            "  %(prog)s                    # 空 shell，进去后 fetch <url>"
        ),
    )
    parser.add_argument("url", nargs="?", help="启动时抓取的 URL（可省略，进 shell 后再 fetch）。")
    parser.add_argument(
        "--impersonate",
        default="chrome131",
        help="Fetcher 伪装的浏览器指纹（默认 chrome131）。",
    )
    args = parser.parse_args(argv)
    fetcher = Fetcher(impersonate=args.impersonate)
    return run_shell(args.url, fetcher)


if __name__ == "__main__":
    raise SystemExit(main())
