"""shell 命令解析与执行核心测试（无网络：mock fetcher + 构造 Response）。

解析逻辑是纯函数、ShellSession 只做文本进出、run_shell 的 input/print
均可注入——因此全套测试无需模拟 stdin，也不发真实请求。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

import pytest

from web_crawler.app import shell
from web_crawler.app.shell import (
    HELP_TEXT,
    CssCommand,
    FetchCommand,
    HelpCommand,
    LinksCommand,
    NoopCommand,
    QuitCommand,
    ShellSession,
    UnknownCommand,
    XPathCommand,
    format_summary,
    parse_command,
    run_shell,
)
from web_crawler.exceptions import FetchError, UnsafeTargetError
from web_crawler.response import Response

EXAMPLE_HTML = b"""<!DOCTYPE html>
<html><head><title>Example Domain</title></head>
<body>
<div id="main">
<h1>Example Domain</h1>
<p>This domain is for use in examples.</p>
<a href="https://www.iana.org/domains/reserved">More information...</a>
</div>
</body></html>"""


def make_response(
    url: str = "https://example.com/", html: bytes = EXAMPLE_HTML, status: int = 200
) -> Response:
    """构造离线 Response（不走网络）。"""
    return Response(url=url, status=status, content=html)


def make_fetcher(
    responses: dict[str, Response] | None = None, error: Exception | None = None
) -> Mock:
    """构造 mock fetcher：按 URL 返回预设响应，或恒抛 ``error``。"""
    fetcher = Mock(name="fetcher")
    if error is not None:
        fetcher.get.side_effect = error
    else:
        fetcher.get.side_effect = lambda url: (responses or {}).get(url, make_response())
    return fetcher


def fake_input(lines: list[str]) -> Callable[[str], str]:
    """构造按序吐出 ``lines`` 的 input 替身；耗尽后抛 EOFError（模拟 Ctrl+D）。"""
    state = iter(lines)

    def _fn(_prompt: str) -> str:
        try:
            return next(state)
        except StopIteration:
            raise EOFError from None

    return _fn


class TestParseCommand:
    """一行输入 → 动作对象的纯函数解析。"""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("quit", QuitCommand()),
            ("exit", QuitCommand()),
            ("q", QuitCommand()),
            ("QUIT", QuitCommand()),
            ("  quit  ", QuitCommand()),
            ("help", HelpCommand()),
            ("?", HelpCommand()),
            ("links", LinksCommand()),
            ("LINKS", LinksCommand()),
        ],
    )
    def test_simple_verbs(self, line: str, expected: Any) -> None:
        assert parse_command(line) == expected

    @pytest.mark.parametrize("line", ["", "   ", "\t"])
    def test_blank_lines_are_noop(self, line: str) -> None:
        assert parse_command(line) == NoopCommand()

    def test_css_with_plain_query(self) -> None:
        assert parse_command("css h1") == CssCommand("h1")

    def test_css_with_quoted_query(self) -> None:
        """整体引号包裹的选择器剥一层引号。"""
        assert parse_command('css "div.item a"') == CssCommand("div.item a")
        assert parse_command("css 'div.item a'") == CssCommand("div.item a")

    def test_css_multi_token_query_kept_intact(self) -> None:
        """选择器内部的空格原样保留。"""
        assert parse_command("css div.item a") == CssCommand("div.item a")

    def test_xpath_query_keeps_inner_quotes(self) -> None:
        """XPath 内部引号不被破坏（不走 shlex 全切的原因）。"""
        assert parse_command('xpath //a[@href="x"]') == XPathCommand('//a[@href="x"]')

    def test_fetch(self) -> None:
        assert parse_command("fetch https://example.com/") == FetchCommand("https://example.com/")

    def test_fetch_quoted_url(self) -> None:
        assert parse_command("fetch 'https://example.com/'") == FetchCommand("https://example.com/")

    def test_bare_verbs_without_args_are_unknown(self) -> None:
        assert parse_command("fetch") == UnknownCommand("fetch")
        assert parse_command("css") == UnknownCommand("css")
        assert parse_command("xpath") == UnknownCommand("xpath")

    def test_verb_prefix_requires_whitespace(self) -> None:
        """``cssx`` 不被误认成 css 动词，整体按 CSS 选择器处理。"""
        assert parse_command("cssx") == CssCommand("cssx")
        assert parse_command("fetchhttps://x") == CssCommand("fetchhttps://x")

    def test_bare_selector_defaults_to_css(self) -> None:
        """无前缀默认按 CSS 处理（scrapy shell 同款体验）。"""
        assert parse_command("h1") == CssCommand("h1")
        assert parse_command("div.item > a[href]") == CssCommand("div.item > a[href]")


class TestFormatSummary:
    """响应摘要：状态 / URL / 最终 URL / 内容长度 / 标题。"""

    def test_basic_fields(self) -> None:
        summary = format_summary("https://example.com/", make_response())
        assert "200" in summary
        assert "https://example.com/" in summary
        assert f"{len(EXAMPLE_HTML)} bytes" in summary
        assert "Example Domain" in summary

    def test_non_ok_status_marked(self) -> None:
        summary = format_summary("https://example.com/", make_response(status=404))
        assert "404" in summary
        assert "非 2xx/3xx" in summary

    def test_redirected_final_url(self) -> None:
        """重定向后的最终 URL 单独一行展示。"""
        summary = format_summary(
            "https://example.com/start", make_response(url="https://example.com/final")
        )
        assert "最终 URL: https://example.com/final" in summary

    def test_no_final_url_line_when_unchanged(self) -> None:
        summary = format_summary("https://example.com/", make_response())
        assert "最终 URL" not in summary

    def test_non_html_content_does_not_crash(self) -> None:
        """JSON/二进制响应没有 <title>，摘要不崩、不显示标题行。"""
        summary = format_summary(
            "https://example.com/api",
            Response(url="https://example.com/api", status=200, content=b'{"a": 1}'),
        )
        assert "200" in summary
        assert "标题" not in summary


class TestShellSession:
    """命令执行核心：fetch 切换、选择查询、links、错误路径。"""

    def test_fetch_updates_response_and_returns_summary(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        out = session.execute(FetchCommand("https://example.com/"))
        assert session.response is not None
        assert session.response.url == "https://example.com/"
        assert "200" in out and "Example Domain" in out

    def test_fetch_switches_target(self) -> None:
        """fetch 指令切换当前响应（前 20 条链接/选择器作用于新页面）。"""
        responses = {
            "https://example.com/": make_response(html=b"<html><title>A</title></html>"),
            "https://iana.org/": make_response(
                url="https://iana.org/", html=b"<html><title>B</title></html>"
            ),
        }
        session = ShellSession(fetcher=make_fetcher(responses))
        session.execute(FetchCommand("https://example.com/"))
        session.execute(FetchCommand("https://iana.org/"))
        assert session.response is not None
        assert session.response.url == "https://iana.org/"
        assert "B" in session.execute(CssCommand("title"))

    def test_fetch_error_keeps_previous_response(self) -> None:
        """换目标抓取失败时人话报错，且不清空当前响应。"""
        session = ShellSession(fetcher=make_fetcher())
        session.execute(FetchCommand("https://example.com/"))
        failing = ShellSession(fetcher=make_fetcher(error=FetchError("boom")))
        failing.response = session.response
        out = failing.execute(FetchCommand("https://down.example/"))
        assert "抓取失败" in out
        assert "建议" in out
        assert failing.response is session.response

    def test_first_fetch_error_leaves_response_none(self) -> None:
        session = ShellSession(fetcher=make_fetcher(error=FetchError("dns")))
        out = session.execute(FetchCommand("https://down.example/"))
        assert "抓取失败" in out
        assert session.response is None

    def test_ssrf_blocked_message_is_friendly(self) -> None:
        """SSRF 拦截走人话提示（私网防护经 Fetcher 正常链路生效）。"""
        session = ShellSession(fetcher=make_fetcher(error=UnsafeTargetError("private host")))
        out = session.execute(FetchCommand("http://192.168.1.1/"))
        assert "已拦截" in out
        assert "SSRF" in out

    def test_empty_fetch_url_shows_usage(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        assert "用法" in session.execute(FetchCommand("   "))

    def test_css_query(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        out = session.execute(CssCommand("h1"))
        assert "匹配 1 个元素" in out
        assert "Example Domain" in out
        assert "<h1>" in out

    def test_css_attr_query_returns_text(self) -> None:
        """``a::attr(href)`` 返回属性值文本而非元素。"""
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        out = session.execute(CssCommand("a::attr(href)"))
        assert "匹配 1" in out
        assert "https://www.iana.org/domains/reserved" in out

    def test_css_no_match(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        assert "匹配 0 个元素" in session.execute(CssCommand("nosuch"))

    def test_xpath_query(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        out = session.execute(XPathCommand("//h1"))
        assert "匹配 1 个元素" in out
        assert "Example Domain" in out

    def test_xpath_attr_query(self) -> None:
        """XPath 用 ``::attr()`` 伪元素取属性值（库对原生 ``@href`` 返回
        字符串的过滤行为是既有约定，元素属性经摘要行展示）。"""
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        out = session.execute(XPathCommand("//a::attr(href)"))
        assert "匹配 1" in out
        assert "https://www.iana.org/domains/reserved" in out

    def test_xpath_element_shows_href_in_summary(self) -> None:
        """``//a`` 元素查询的摘要附带 href 属性。"""
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        out = session.execute(XPathCommand("//a"))
        assert "匹配 1" in out
        assert "href=" in out
        assert "https://www.iana.org/domains/reserved" in out

    def test_css_without_response_prompts_fetch(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        out = session.execute(CssCommand("h1"))
        assert "fetch" in out

    def test_xpath_without_response_prompts_fetch(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        assert "fetch" in session.execute(XPathCommand("//h1"))

    def test_bad_selector_reports_error(self) -> None:
        """非法选择器给人话错误而非 traceback。"""
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        out = session.execute(CssCommand("!!!invalid"))
        assert "执行失败" in out

    def test_links(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response()
        out = session.execute(LinksCommand())
        assert "共 1 条链接" in out
        assert "https://www.iana.org/domains/reserved" in out

    def test_links_without_response_prompts_fetch(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        assert "fetch" in session.execute(LinksCommand())

    def test_links_truncated_at_20(self) -> None:
        """超过 20 条链接截断展示。"""
        anchors = "".join(f'<a href="/p/{i}">link {i}</a>' for i in range(25))
        html = f"<html><body>{anchors}</body></html>".encode()
        session = ShellSession(fetcher=make_fetcher())
        session.response = make_response(html=html)
        out = session.execute(LinksCommand())
        assert "共 25 条链接" in out
        assert "其余 5 条已省略" in out

    def test_help_and_unknown_and_noop(self) -> None:
        session = ShellSession(fetcher=make_fetcher())
        assert session.execute(HelpCommand()) == HELP_TEXT
        assert "用法" in session.execute(UnknownCommand("fetch"))
        assert session.execute(NoopCommand()) == ""


class TestRunShell:
    """交互循环薄壳：input 序列驱动，print 收集验证。"""

    def test_full_session_flow(self) -> None:
        """初始抓取 → css 查询 → quit，输出含摘要与查询结果。"""
        outputs: list[str] = []
        code = run_shell(
            "https://example.com/",
            make_fetcher(),
            input_fn=fake_input(["css h1", "quit"]),
            print_fn=outputs.append,
        )
        assert code == 0
        joined = "\n".join(outputs)
        assert "Example Domain" in joined  # 初始摘要
        assert "匹配 1 个元素" in joined  # css h1 结果

    def test_fetch_command_inside_loop(self) -> None:
        """循环内 fetch 切换目标后，查询作用于新页面。"""
        responses = {
            "https://example.com/": make_response(),
            "https://iana.org/": make_response(
                url="https://iana.org/", html=b"<html><title>IANA</title></html>"
            ),
        }
        outputs: list[str] = []
        run_shell(
            None,
            make_fetcher(responses),
            input_fn=fake_input(["fetch https://iana.org/", "css title", "quit"]),
            print_fn=outputs.append,
        )
        joined = "\n".join(outputs)
        assert "未提供初始 URL" in joined
        assert "IANA" in joined

    def test_no_initial_url_enters_empty_shell(self) -> None:
        """无 URL 启动进入空 shell 并提示先 fetch（而非报错退出）。"""
        outputs: list[str] = []
        code = run_shell(
            None, make_fetcher(), input_fn=fake_input(["quit"]), print_fn=outputs.append
        )
        assert code == 0
        assert "fetch" in outputs[0]

    def test_initial_fetch_failure_stays_in_shell(self) -> None:
        """初始 URL 抓取失败仍留在 shell，可 quit 正常退出。"""
        outputs: list[str] = []
        code = run_shell(
            "https://down.example/",
            make_fetcher(error=FetchError("boom")),
            input_fn=fake_input(["quit"]),
            print_fn=outputs.append,
        )
        assert code == 0
        assert "抓取失败" in outputs[0]

    def test_eof_exits_gracefully(self) -> None:
        """Ctrl+D（EOFError）优雅退出，返回 0。"""
        outputs: list[str] = []
        code = run_shell(None, make_fetcher(), input_fn=fake_input([]), print_fn=outputs.append)
        assert code == 0

    def test_keyboard_interrupt_exits_gracefully(self) -> None:
        """Ctrl+C（KeyboardInterrupt）优雅退出，返回 0。"""

        def raising_input(_prompt: str) -> str:
            raise KeyboardInterrupt

        outputs: list[str] = []
        code = run_shell(None, make_fetcher(), input_fn=raising_input, print_fn=outputs.append)
        assert code == 0

    def test_blank_lines_are_skipped(self) -> None:
        """空行不产生输出。"""
        outputs: list[str] = []
        run_shell(
            None,
            make_fetcher(),
            input_fn=fake_input(["", "   ", "quit"]),
            print_fn=outputs.append,
        )
        assert len(outputs) == 1  # 只有空 shell 的开场提示

    def test_exit_alias_works(self) -> None:
        outputs: list[str] = []
        code = run_shell(
            None, make_fetcher(), input_fn=fake_input(["exit"]), print_fn=outputs.append
        )
        assert code == 0


class TestMain:
    """CLI 入口：mock Fetcher，stdin 喂 quit。"""

    def test_main_with_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """带 URL 启动：真实打印摘要后退出 0。"""
        fake = make_fetcher()
        monkeypatch.setattr(shell, "Fetcher", lambda **kwargs: fake)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "quit")
        assert shell.main(["https://example.com/"]) == 0
        assert "Example Domain" in capsys.readouterr().out

    def test_main_without_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """无 URL 启动进入空 shell，退出 0。"""
        fake = make_fetcher()
        monkeypatch.setattr(shell, "Fetcher", lambda **kwargs: fake)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "quit")
        assert shell.main([]) == 0
        assert "fetch" in capsys.readouterr().out

    def test_main_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--help 正常打印用法并退出 0。"""
        with pytest.raises(SystemExit) as exc_info:
            shell.main(["--help"])
        assert exc_info.value.code == 0
        assert "CSS" in capsys.readouterr().out

    def test_main_passes_impersonate_to_fetcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--impersonate 透传给 Fetcher 构造参数。"""
        calls: dict[str, Any] = {}

        def fake_fetcher(**kwargs: Any) -> Mock:
            calls.update(kwargs)
            return make_fetcher()

        monkeypatch.setattr(shell, "Fetcher", fake_fetcher)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "quit")
        shell.main(["--impersonate", "firefox135"])
        assert calls == {"impersonate": "firefox135"}
