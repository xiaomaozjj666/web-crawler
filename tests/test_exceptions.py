"""统一异常体系测试：层次结构 / 兼容捕获矩阵 / pickle / SSRF 集成。

核心证明目标（兼容式渐进接入）：

* 每个异常类的 MRO 与 isinstance 链正确（子类同时是
  ``WebCrawlerError`` 与兼容基类 ``ValueError``/``RuntimeError`` 的实例）；
* ``except ValueError`` 依然能捕获 ``UnsafeTargetError`` 等（存量断言
  原样通过的兼容性证明）；
* ``except WebCrawlerError`` 能统一捕获全部库异常（新调用方入口）；
* 兼容基类之外的内置异常**不会**误捕（库错误与环境的错误精确区分）；
* ``_ssrf.validate_url_host`` 真实触发 ``UnsafeTargetError``，且对
  ``pytest.raises(ValueError)`` 完全兼容（test_ssrf_guard.py 既有断言
  形态的试金石）。
"""

from __future__ import annotations

import ast
import json
import pickle
import socket
import subprocess
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import web_crawler.exceptions as exceptions_module
from web_crawler._ssrf import validate_url_host
from web_crawler.ai.llm import OpenAICompatibleProvider, get_provider
from web_crawler.ai.loop import ContextCompressor
from web_crawler.ai.recorder import ReplayRunner
from web_crawler.app import _crawler_core
from web_crawler.app.crawler_net import SafeRedirectHandler
from web_crawler.exceptions import (
    AIProviderError,
    ConfigError,
    FetchError,
    InvalidStateError,
    ParseError,
    UnsafeTargetError,
    WebCrawlerError,
)
from web_crawler.fetchers import _pin
from web_crawler.fetchers.dynamic import DynamicFetcher
from web_crawler.fetchers.fetcher import Fetcher
from web_crawler.fetchers.proxy import ProxyPool
from web_crawler.fetchers.session import SessionPool
from web_crawler.mcp._tools_pentest import _parse_pentest_target
from web_crawler.mcp.server import ReverseMCPServer
from web_crawler.parser.selector import Selector
from web_crawler.parser.storage import AdaptiveStorage
from web_crawler.parser.visual import VisualExtractor
from web_crawler.pentest.port_scanner import PortScanner
from web_crawler.spider.autothrottle import AutoThrottle
from web_crawler.spider.cache import _META_VERSION, CacheMiddleware
from web_crawler.spider.spider import Request

# (异常类, 兼容基类) 的声明式矩阵——isinstance 兼容承诺的全部内容。
_EXCEPTION_CASES: list[tuple[type[Exception], type[Exception]]] = [
    (UnsafeTargetError, ValueError),
    (ConfigError, ValueError),
    (ParseError, ValueError),
    (FetchError, RuntimeError),
    (InvalidStateError, RuntimeError),
    (AIProviderError, RuntimeError),
]

# 全部公开异常类（含根基类）。
_ALL_CLASSES: list[type[Exception]] = [WebCrawlerError] + [cls for cls, _ in _EXCEPTION_CASES]


@pytest.fixture(autouse=True)
def _power_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离 Power Mode 环境变量，保证 host 门禁默认生效（放行用例再显式 setenv）。"""
    monkeypatch.delenv("WEB_CRAWLER_POWER_MODE", raising=False)


# ── 层次结构：MRO 与 isinstance 链 ─────────────────────────────────────────


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_mro_order(cls: type[Exception], compat: type[Exception]) -> None:
    """MRO 前三位依次为 (自身, WebCrawlerError, 兼容基类)，根基类优先于兼容基类。"""
    assert cls.__mro__[:3] == (cls, WebCrawlerError, compat)
    assert cls.__mro__[-1] is object


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_isinstance_dual_membership(cls: type[Exception], compat: type[Exception]) -> None:
    """实例同时是自身、WebCrawlerError、兼容基类与 Exception 的实例。"""
    exc = cls("boom")
    assert isinstance(exc, cls)
    assert isinstance(exc, WebCrawlerError)
    assert isinstance(exc, compat)
    assert isinstance(exc, Exception)


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_issubclass_matrix(cls: type[Exception], compat: type[Exception]) -> None:
    """子类关系：cls ⊂ WebCrawlerError ⊂ Exception，且 cls ⊂ 兼容基类。"""
    assert issubclass(cls, WebCrawlerError)
    assert issubclass(cls, compat)
    assert issubclass(WebCrawlerError, Exception)


def test_root_is_plain_exception() -> None:
    """根基类只继承 Exception：except ValueError/RuntimeError 不会误捕纯库错误。"""
    assert issubclass(WebCrawlerError, Exception)
    assert not issubclass(WebCrawlerError, ValueError)
    assert not issubclass(WebCrawlerError, RuntimeError)


# ── 可捕获性矩阵（兼容性核心证明） ─────────────────────────────────────────


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_catch_as_web_crawler_error(cls: type[Exception], compat: type[Exception]) -> None:
    """except WebCrawlerError 统一捕获每一个库异常（新调用方入口）。"""
    with pytest.raises(WebCrawlerError):
        raise cls("boom")


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_catch_as_compat_base(cls: type[Exception], compat: type[Exception]) -> None:
    """except 兼容基类（ValueError/RuntimeError）依然捕获（存量断言原样通过）。"""
    with pytest.raises(compat):
        raise cls("boom")


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_not_caught_by_foreign_builtin(cls: type[Exception], compat: type[Exception]) -> None:
    """兼容基类之外的内置异常不得捕获（库错误与环境的错误精确区分）。"""
    foreign: type[Exception] = RuntimeError if compat is ValueError else ValueError
    with pytest.raises(cls):
        try:
            raise cls("boom")
        except foreign:  # pragma: no cover - 被捕获即测试失败
            pytest.fail(f"{cls.__name__} 不应被 except {foreign.__name__} 捕获")


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_caught_as_exception(cls: type[Exception], compat: type[Exception]) -> None:
    """except Exception 依然捕获（标准异常协议完整保留）。"""
    try:
        raise cls("boom")
    except Exception:
        pass
    else:
        pytest.fail(f"{cls.__name__} 应能被 except Exception 捕获")


# ── repr / str / args 透传 ──────────────────────────────────────────────────


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_single_arg_message(cls: type[Exception], compat: type[Exception]) -> None:
    """单参数构造：str 与 args 透传，与内置异常行为一致。"""
    exc = cls("boom")
    assert exc.args == ("boom",)
    assert str(exc) == "boom"


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_multiple_args_passthrough(cls: type[Exception], compat: type[Exception]) -> None:
    """多参数构造：args 按元组透传（多继承不改变参数协议）。"""
    exc = cls("a", "b")
    assert exc.args == ("a", "b")


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_repr_format(cls: type[Exception], compat: type[Exception]) -> None:
    """repr 形如 ``ClassName('boom')``：类名与消息均不丢失。"""
    exc = cls("boom")
    assert repr(exc) == f"{cls.__name__}('boom')"


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_empty_construction(cls: type[Exception], compat: type[Exception]) -> None:
    """无参构造可用（Exception 惯例），args 为空元组。"""
    exc = cls()
    assert exc.args == ()
    assert str(exc) == ""


def test_exception_chaining_preserved() -> None:
    """raise ... from ... 的 __cause__ 链完整保留（多继承不影响异常链）。"""
    cause = ValueError("dns lookup failed")
    with pytest.raises(UnsafeTargetError) as excinfo:
        try:
            raise cause
        except ValueError as inner:
            raise UnsafeTargetError("blocked") from inner
    assert excinfo.value.__cause__ is cause


# ── pickle 往返（顶层可导入的异常类应支持 pickle） ─────────────────────────


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_pickle_roundtrip_single_arg(cls: type[Exception], compat: type[Exception]) -> None:
    """pickle 往返：类型、args、str 全部还原。"""
    exc = cls("boom")
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is cls
    assert restored.args == ("boom",)
    assert str(restored) == "boom"


@pytest.mark.parametrize(("cls", "compat"), _EXCEPTION_CASES)
def test_pickle_roundtrip_multiple_args(cls: type[Exception], compat: type[Exception]) -> None:
    """多 args 的 pickle 往返：args 元组逐项还原。"""
    exc = cls("a", "b")
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is cls
    assert restored.args == ("a", "b")


def test_pickle_root_class() -> None:
    """根基类同样支持 pickle 往返。"""
    exc = WebCrawlerError("root cause")
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is WebCrawlerError
    assert str(restored) == "root cause"


# ── 异常族完备性 ───────────────────────────────────────────────────────────


def test_all_exports_match_definitions() -> None:
    """__all__ 与模块顶层异常类定义一一对应，无多无漏。"""
    public = {
        name
        for name, obj in vars(exceptions_module).items()
        if isinstance(obj, type) and issubclass(obj, BaseException) and not name.startswith("_")
    }
    assert set(exceptions_module.__all__) == public
    assert len(exceptions_module.__all__) == 7


def test_all_is_sorted_and_lists_root_first_family() -> None:
    """__all__ 按字母序（模块风格），且包含根基类与 6 个具体子类。"""
    assert list(exceptions_module.__all__) == sorted(exceptions_module.__all__)
    for cls in _ALL_CLASSES:
        assert cls.__name__ in exceptions_module.__all__


@pytest.mark.parametrize("cls", _ALL_CLASSES)
def test_docstring_present_and_chinese(cls: type[Exception]) -> None:
    """每个公开异常类都带中文 docstring（语义 / 触发场景 / 兼容基类说明）。"""
    doc = cls.__doc__ or ""
    assert doc.strip(), f"{cls.__name__} 缺少 docstring"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in doc), f"{cls.__name__} docstring 应为中文"


def test_reimport_returns_same_module() -> None:
    """重复 import 返回同一模块对象（无循环导入 / 重复初始化问题）。"""
    first = import_module("web_crawler.exceptions")
    second = import_module("web_crawler.exceptions")
    assert first is second
    assert first is exceptions_module


def test_source_imports_only_stdlib() -> None:
    """源码层面：exceptions.py 只 import 标准库（__future__），零第三方依赖。"""
    path = Path(exceptions_module.__file__ or "")
    assert path.name == "exceptions.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= {"__future__"}


def test_standalone_import_without_heavy_deps() -> None:
    """子进程全新解释器导入：不触发 httpx/lxml/playwright 等重量级依赖。"""
    code = (
        "import sys\n"
        "import web_crawler.exceptions\n"
        "heavy = [m for m in ('httpx', 'bs4', 'lxml', 'curl_cffi', 'playwright', "
        "'camoufox', 'pydantic', 'ddddocr') if m in sys.modules]\n"
        "assert not heavy, f'heavy deps imported: {heavy}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ── 实际触发集成：_ssrf.validate_url_host ──────────────────────────────────


def _url_for_host(host: str, path: str = "/") -> str:
    """构造 URL，IPv6 字面量按 urllib.parse 要求加方括号（对齐 test_ssrf_guard）。"""
    literal = f"[{host}]" if ":" in host else host
    return f"http://{literal}{path}"


# 覆盖私网 / 环回 / 链路本地（云元数据）/ CGNAT / 未指定 / IPv6 各段 /
# 静态黑名单主机名 / 非点分 IPv4 归一化的代表性 host（参考 test_ssrf_guard）。
_UNSAFE_HOSTS: list[str] = [
    "169.254.169.254",  # 云元数据（链路本地）
    "100.100.100.200",  # 阿里云元数据（CGNAT）
    "127.0.0.1",  # IPv4 环回
    "10.0.0.5",  # 私网 A 段
    "192.168.1.1",  # 私网 C 段
    "172.16.0.1",  # 私网 B 段
    "0.0.0.0",  # 未指定地址
    "::1",  # IPv6 环回
    "fe80::1",  # IPv6 链路本地
    "fc00::1",  # IPv6 ULA
    "localhost",  # 静态黑名单主机名
    "metadata.google.internal",  # 云元数据主机名
    "2130706433",  # 单段十进制 → 127.0.0.1
    "0x7f000001",  # 0x 十六进制 → 127.0.0.1
]


@pytest.mark.parametrize("host", _UNSAFE_HOSTS)
def test_validate_url_host_raises_unsafe_target_error(host: str) -> None:
    """不安全 host（私网/环回/链路本地/云元数据等）触发 UnsafeTargetError。"""
    with pytest.raises(UnsafeTargetError):
        validate_url_host(_url_for_host(host))


@pytest.mark.parametrize("host", _UNSAFE_HOSTS)
def test_validate_url_host_value_error_compat(host: str) -> None:
    """兼容试金石：pytest.raises(ValueError) 依然通过（存量断言原样保留）。"""
    with pytest.raises(ValueError, match="unsafe host"):
        validate_url_host(_url_for_host(host))


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "169.254.169.254", "localhost", "::1"],  # IP / 元数据 / 主机名 / IPv6
)
def test_validate_url_host_catch_as_web_crawler_error(host: str) -> None:
    """except WebCrawlerError 统一捕获 SSRF 门禁错误（新调用方入口）。"""
    with pytest.raises(WebCrawlerError):
        validate_url_host(_url_for_host(host))


@pytest.mark.parametrize("host", _UNSAFE_HOSTS)
def test_validate_url_host_error_dual_instanceof(host: str) -> None:
    """捕获到的异常同时是 UnsafeTargetError / WebCrawlerError / ValueError 三重身份。"""
    with pytest.raises(UnsafeTargetError) as excinfo:
        validate_url_host(_url_for_host(host))
    exc = excinfo.value
    assert isinstance(exc, WebCrawlerError)
    assert isinstance(exc, ValueError)
    assert not isinstance(exc, RuntimeError)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "http://example.com:8080/path?q=1",
        "https://93.184.216.34/",  # 公网 IP 字面量
    ],
)
def test_validate_url_host_safe_target_passes(url: str) -> None:
    """公网目标（默认不做 DNS 解析）不抛任何异常。"""
    validate_url_host(url)


def test_validate_url_host_power_mode_bypasses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Power Mode（WEB_CRAWLER_POWER_MODE=1）下放行 host 校验（既有行为不变）。"""
    monkeypatch.setenv("WEB_CRAWLER_POWER_MODE", "1")
    validate_url_host("http://169.254.169.254/latest/meta-data/")
    validate_url_host("http://127.0.0.1:8080/admin")


def test_validate_url_host_message_keeps_url() -> None:
    """错误消息保留原始 URL（切换异常类型不丢失诊断信息）。"""
    with pytest.raises(UnsafeTargetError, match="http://10.0.0.5/secret"):
        validate_url_host("http://10.0.0.5/secret")


# ── 第二批接入：各模块真实路径触发新异常类型（双重身份验证） ────────────────
#
# 每个测试触发一条真实错误路径，断言捕获到的异常同时是新类型（ConfigError /
# ParseError / InvalidStateError / FetchError / AIProviderError /
# UnsafeTargetError）、兼容基类（ValueError / RuntimeError）与 WebCrawlerError
# 的实例——三重身份齐全才算接入成功。


def test_batch2_config_error_modules() -> None:
    """ConfigError 锚点扫描：各模块参数校验真实触发（ConfigError ⊂ ValueError）。"""
    cases: list[tuple[str, Callable[[], object]]] = [
        ("autothrottle", lambda: AutoThrottle(alpha=0.0)),
        ("session", lambda: SessionPool(max_size=0)),
        ("proxy", lambda: ProxyPool(strategy="bogus")),
        ("fetcher", lambda: Fetcher(max_redirects=-1)),
        ("request", lambda: Request(url="")),
        ("llm-provider", lambda: get_provider("no-such-provider")),
        ("loop", lambda: ContextCompressor(None, max_history=5, compress_to=5)),
        ("ports", lambda: PortScanner._validate_ports([0, 70000])),
    ]
    for label, factory in cases:
        with pytest.raises(ConfigError) as excinfo:
            factory()
        exc = excinfo.value
        assert isinstance(exc, ValueError), label
        assert isinstance(exc, WebCrawlerError), label


def test_batch2_cache_meta_version_raises_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spider/cache.py 缓存 meta 版本不符触发 ParseError（⊂ ValueError）。

    ``_load`` 会把损坏条目就地清理并按 miss 返回（行为不变）；类型断言
    经 ``_remove`` spy 以 ``sys.exc_info()`` 取出正被处理的异常对象。
    """
    mw = CacheMiddleware(tmp_path)
    key = "deadbeef" * 8
    mw._meta_path(key).write_text(
        json.dumps({"version": _META_VERSION + 1, "url": "https://example.com/"}),
        encoding="utf-8",
    )
    captured: list[BaseException | None] = []
    monkeypatch.setattr(mw, "_remove", lambda k: captured.append(sys.exc_info()[1]))
    assert mw._load(key) is None  # 既有行为：损坏条目按 miss 处理
    exc = captured[0]
    assert exc is not None
    assert isinstance(exc, ParseError)
    assert isinstance(exc, ValueError)
    assert isinstance(exc, WebCrawlerError)
    assert "unsupported cache meta version" in str(exc)


@pytest.mark.parametrize(
    "call",
    [
        lambda ve: ve.extract([], "prompt"),
        lambda ve: ve.extract_with_client([], "prompt"),
    ],
    ids=["extract", "extract_with_client"],
)
def test_batch2_visual_empty_tiles_raises_parse_error(call: Callable[[Any], str]) -> None:
    """parser/visual.py tiles 参数为空触发 ParseError（路线图归类，⊂ ValueError）。"""
    ve = VisualExtractor(api_key="test-key")
    with pytest.raises(ParseError) as excinfo:
        call(ve)
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, WebCrawlerError)


@pytest.mark.parametrize("api", ["save", "retrieve", "relocate"])
def test_batch2_selector_requires_adaptive_raises_invalid_state(api: str) -> None:
    """parser/selector.py save/retrieve/relocate 需 adaptive=True（InvalidStateError ⊂ RuntimeError）。"""
    sel = Selector("<p>hello</p>")
    element = sel.css_first("p")
    assert element is not None
    with pytest.raises(InvalidStateError) as excinfo:
        if api == "save":
            sel.save(element, "id")
        elif api == "retrieve":
            sel.retrieve("id")
        else:
            sel.relocate({"fingerprint": "x"})
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_adaptive_storage_closed_raises_invalid_state(tmp_path: Path) -> None:
    """parser/storage.py AdaptiveStorage 关闭后再写（InvalidStateError ⊂ RuntimeError）。"""
    store = AdaptiveStorage(tmp_path / "adapt.db")
    store.close()
    with pytest.raises(InvalidStateError) as excinfo:
        store.save("example.com", "id", "fp")
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_replay_runner_without_script_raises_invalid_state() -> None:
    """ai/recorder.py 未 load_script 就 run（InvalidStateError ⊂ RuntimeError）。"""
    with pytest.raises(InvalidStateError) as excinfo:
        ReplayRunner().run()
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_llm_missing_api_key_raises_ai_provider_error() -> None:
    """ai/llm.py 未配置 API key（AIProviderError ⊂ RuntimeError）。"""
    provider = OpenAICompatibleProvider(model="test-model")
    provider.api_key = ""  # 隔离构造期从环境 / .env 读到的 key
    with pytest.raises(AIProviderError) as excinfo:
        provider._ensure_api_key()
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)
    assert "no API key" in str(excinfo.value)


def _fake_vlm_client(content: Any, *, raise_exc: BaseException | None = None) -> Any:
    """构造 OpenAI 风格的假 client：返回固定 content 或抛给定异常。"""

    def create(**kwargs: Any) -> Any:
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_batch2_visual_vlm_empty_content_raises_ai_provider_error() -> None:
    """parser/visual.py VLM 返回空 content（AIProviderError ⊂ RuntimeError）。"""
    ve = VisualExtractor(api_key="test-key")
    client = _fake_vlm_client(None)
    with pytest.raises(AIProviderError, match="VLM returned empty content") as excinfo:
        ve.extract_with_client([{"b64": "aGk="}], "prompt", client=client)
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_visual_vlm_client_failure_raises_ai_provider_error() -> None:
    """parser/visual.py VLM client 调用失败（AIProviderError ⊂ RuntimeError，保留异常链）。"""
    ve = VisualExtractor(api_key="test-key")
    client = _fake_vlm_client(None, raise_exc=ConnectionError("vlm down"))
    with pytest.raises(AIProviderError, match="VLM client call failed") as excinfo:
        ve.extract_with_client([{"b64": "aGk="}], "prompt", client=client)
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value.__cause__, ConnectionError)


def test_batch2_stealth_fetcher_unavailable_raises_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app/_crawler_core.py stealth fetcher 不可用（FetchError ⊂ RuntimeError）。"""
    monkeypatch.setattr(_crawler_core, "_stealth_fetcher", None)
    monkeypatch.setattr(_crawler_core, "_import_stealth_fetcher", lambda: None)
    with pytest.raises(FetchError, match="stealth fetcher unavailable") as excinfo:
        _crawler_core._get_stealth_fetcher("chrome131", 30.0, None)
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_fetcher_too_many_redirects_raises_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetchers/fetcher.py 重定向超限（同步路径，FetchError ⊂ RuntimeError）。"""
    f = Fetcher(max_redirects=0)
    monkeypatch.setattr(f, "_send_once_sync", lambda *args, **kwargs: object())
    monkeypatch.setattr(f, "_advance_redirect", lambda *args, **kwargs: True)
    with pytest.raises(FetchError, match="too many redirects") as excinfo:
        f._send_with_redirects_sync(
            "GET", "https://example.com/", None, None, None, {}, None, 30.0, True, True
        )
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_dynamic_fetch_failure_raises_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetchers/dynamic.py JS 渲染抓取失败（FetchError ⊂ RuntimeError，保留异常链）。"""

    def _boom(browser: object, url: str, proxy_settings: object) -> object:
        raise RuntimeError("render failed")

    df = DynamicFetcher(headless=True)
    monkeypatch.setattr(df, "_ensure_browser", lambda: None)
    monkeypatch.setattr(df, "_render_page", _boom)
    with pytest.raises(FetchError, match="dynamic fetch") as excinfo:
        df.fetch("https://example.com/")
    assert isinstance(excinfo.value, RuntimeError)
    assert isinstance(excinfo.value, WebCrawlerError)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_batch2_pin_url_dns_to_private_raises_unsafe_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetchers/_pin.py TOCTOU 拦截：DNS 解析落入私网段（UnsafeTargetError ⊂ ValueError）。"""
    fake_infos = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", 80))]
    monkeypatch.setattr(_pin.socket, "getaddrinfo", lambda host, port, *args, **kwargs: fake_infos)
    with pytest.raises(UnsafeTargetError, match="TOCTOU guard") as excinfo:
        _pin.pin_url("http://attacker.example/")
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, WebCrawlerError)


@pytest.mark.parametrize(
    ("newurl", "match"),
    [
        ("file:///etc/passwd", "non-http"),
        ("http://127.0.0.1/secret", "unsafe host"),
        ("http://169.254.169.254/latest/meta-data/", "unsafe host"),
    ],
    ids=["non-http-scheme", "loopback-host", "metadata-host"],
)
def test_batch2_safe_redirect_handler_raises_unsafe_target(newurl: str, match: str) -> None:
    """app/crawler_net.py 重定向门禁：scheme 白名单 + 不安全 host（UnsafeTargetError ⊂ ValueError）。"""
    handler = SafeRedirectHandler()
    with pytest.raises(UnsafeTargetError, match=match) as excinfo:
        handler.redirect_request(None, None, 302, "Found", None, newurl)
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_pentest_scheme_whitelist_raises_unsafe_target() -> None:
    """mcp/_tools_pentest.py scheme 白名单（UnsafeTargetError ⊂ ValueError）。"""
    with pytest.raises(UnsafeTargetError, match="unsupported target scheme") as excinfo:
        _parse_pentest_target("ftp://example.com")
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_crawler_core_unsafe_redirect_raises_unsafe_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app/_crawler_core.py stealth 路径重定向跳向云元数据（UnsafeTargetError ⊂ ValueError）。"""
    fake_resp = SimpleNamespace(
        status=302,
        headers={"location": "http://169.254.169.254/latest/meta-data/"},
        content=b"",
    )

    class FakeFetcher:
        def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
            return fake_resp

    monkeypatch.setattr(_crawler_core, "_get_stealth_fetcher", lambda *args: FakeFetcher())
    with pytest.raises(UnsafeTargetError, match="blocked redirect to unsafe URL") as excinfo:
        _crawler_core._stealth_fetch("https://example.com/", {}, 30, None, "chrome131")
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, WebCrawlerError)


def test_batch2_mcp_browser_task_gate_raises_unsafe_target() -> None:
    """mcp/server.py 浏览器工具门禁：门禁错误转 UnsafeTargetError（⊂ ValueError）。

    门禁在浏览器启动之前拒绝（无 Playwright 依赖），只验证类型切换。
    """
    srv = ReverseMCPServer(provider_name="deepseek", model="test-model")
    with pytest.raises(UnsafeTargetError) as excinfo:
        srv._run_browser_task("http://169.254.169.254/latest/meta-data/", lambda page: None)
    assert isinstance(excinfo.value, ValueError)
    assert isinstance(excinfo.value, WebCrawlerError)
