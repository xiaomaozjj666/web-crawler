"""公共 API 一致性测试：README 宣传的符号必须可从顶层导入。

防再犯：README 功能特性章节宣传 ``ImageCaptchaSolver`` 等类名，但顶层
``_LAZY_IMPORTS`` / ``__all__`` / ``TYPE_CHECKING`` 三处漏加时，
``from web_crawler import ImageCaptchaSolver`` 会抛 ``AttributeError``
（v0.5.1 实测回归）。本文件把「宣传面」与「导出面」钉死在一起：

- ``__all__`` 中的每个名称都能经懒加载 ``getattr`` 解析且非 ``None``；
- README 宣传的公开符号都可从顶层导入并出现在 ``__all__``；
- ``_LAZY_IMPORTS`` 键集合与 ``__all__`` 集合（减非懒加载项）完全一致。

全部用例只做进程内导入（懒加载本身不发网络），保持毫秒级开销。
"""

from __future__ import annotations

import web_crawler

# README「功能特性」章节与快速开始示例宣传的顶层公开符号。
# 新增宣传符号时同步维护本清单——顶层 ``__all__`` 漏加会被立即抓住。
_README_ADVERTISED: tuple[str, ...] = (
    # 自适应解析器 / 懒加载
    "Selector",
    "Adaptors",
    "AdaptiveStorage",
    # 隐身 HTTP
    "Fetcher",
    "AsyncFetcher",
    # JS 渲染与反爬
    "DynamicFetcher",
    "StealthyFetcher",
    "CamoufoxFetcher",
    # 代理轮换
    "ProxyPool",
    # 会话池与指纹轮换
    "SessionPool",
    # Spider 框架（含中间件 / 管道示例）
    "Spider",
    "Request",
    "DupeFilter",
    "DownloaderMiddleware",
    "ItemPipeline",
    "IgnoreRequest",
    "DropItem",
    # 缓存中间件与自适应限速
    "CacheMiddleware",
    "AutoThrottle",
    # 统一异常体系
    "WebCrawlerError",
    # 统一 Response
    "Response",
    # AI 辅助抓取
    "AIExtractor",
    "AIScrapeAgent",
    "RobotsPolicy",
    # JS 逆向 Agent
    "ReverseAgent",
    "ReverseAgentConfig",
    # 图片验证码识别
    "ImageCaptchaSolver",
    "CaptchaManager",
)

# ``__all__`` 中不走 ``_LAZY_IMPORTS`` 的名称（直接定义在模块顶部）。
_NON_LAZY_NAMES: frozenset[str] = frozenset({"__version__"})


def test_all_names_resolvable_and_not_none() -> None:
    """``__all__`` 中的每个名称都能经顶层 ``getattr`` 解析且非 ``None``。"""
    missing = [name for name in web_crawler.__all__ if not hasattr(web_crawler, name)]
    assert not missing, f"__all__ 声明但顶层不可解析: {missing}"
    none_valued = [name for name in web_crawler.__all__ if getattr(web_crawler, name) is None]
    assert not none_valued, f"__all__ 声明但顶层解析为 None: {none_valued}"


def test_readme_advertised_symbols_unique() -> None:
    """宣传清单本身无重复，避免同名符号静默合并。"""
    assert len(_README_ADVERTISED) == len(set(_README_ADVERTISED))


def test_readme_advertised_symbol_importable_from_top_level() -> None:
    """README 宣传的每个符号都可从顶层导入（懒加载解析）且非 ``None``。

    ``getattr`` 与 ``from web_crawler import <name>`` 走同一条模块属性
    查找路径（``__getattr__`` 懒加载兜底），故二者语义等价。
    """
    broken = [name for name in _README_ADVERTISED if getattr(web_crawler, name, None) is None]
    assert not broken, f"README 宣传但顶层不可导入: {broken}"
    not_exported = [name for name in _README_ADVERTISED if name not in web_crawler.__all__]
    assert not not_exported, f"README 宣传但不在 __all__（非正式导出面）: {not_exported}"


def test_image_captcha_solver_top_level_import_regression() -> None:
    """回归：``from web_crawler import ImageCaptchaSolver`` 必须可用。

    v0.5.1 曾因 ``_LAZY_IMPORTS`` / ``__all__`` / ``TYPE_CHECKING`` 三处
    漏加该符号，README 宣传的顶层导入抛 ``AttributeError``。
    """
    from web_crawler import ImageCaptchaSolver
    from web_crawler.ai.image_captcha import ImageCaptchaSolver as Direct

    assert ImageCaptchaSolver is Direct


def test_lazy_imports_keys_match_all() -> None:
    """``_LAZY_IMPORTS`` 键集合与 ``__all__``（减非懒加载项）完全一致。

    两侧任何一侧新增符号而忘记同步另一侧时，此断言立刻失败——
    ImageCaptchaSolver 顶层导出回归的根因即 ``__all__`` 与
    ``_LAZY_IMPORTS`` 的不同步漂移。
    """
    lazy_keys = set(web_crawler._LAZY_IMPORTS)
    all_keys = set(web_crawler.__all__) - _NON_LAZY_NAMES

    only_in_all = sorted(all_keys - lazy_keys)
    only_in_lazy = sorted(lazy_keys - all_keys)
    assert not only_in_all, f"__all__ 有但 _LAZY_IMPORTS 缺: {only_in_all}"
    assert not only_in_lazy, f"_LAZY_IMPORTS 有但 __all__ 缺: {only_in_lazy}"
