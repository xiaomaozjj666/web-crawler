"""web-crawler 统一异常体系（兼容式渐进接入）。

对标 ``scrapy.exceptions`` / ``httpx.HTTPError`` 的分层设计：调用方用
``except WebCrawlerError`` 一次捕获**库自身**的错误，与环境的原生
``ValueError`` / ``RuntimeError`` 精确区分，而不必记忆散落各模块的
raise 类型。

兼容式多继承
------------
每个具体异常类同时继承 :class:`WebCrawlerError` 与一个**兼容基类**
（``ValueError`` 或 ``RuntimeError``，对齐该错误语义在存量代码中的既有
形态）：

* 改 raise 类型后，既有 ``except ValueError`` / ``pytest.raises(ValueError)``
  等断言**原样通过**（isinstance 兼容）；
* 新代码可 ``except WebCrawlerError`` 统一捕获库错误。

类族
----
* ``WebCrawlerError``（Exception）— 根基类，仅作统一捕获收口，不直接 raise
* ``UnsafeTargetError``（ValueError）— SSRF 门禁：私网/环回/链路本地/云元数据
* ``ConfigError``（ValueError）— 配置与参数校验失败
* ``ParseError``（ValueError）— 解析层内容/格式错误
* ``FetchError``（RuntimeError）— 网络抓取层失败
* ``InvalidStateError``（RuntimeError）— 对象状态/前置条件不满足
* ``AIProviderError``（RuntimeError）— LLM/VLM 供应商调用失败

零第三方依赖：本模块不 import httpx / lxml / playwright / curl_cffi，
任意轻量环境（fetchers 钉扎、MCP 门禁、CLI）可直接导入。

渐进接入路线图
--------------
**已接入**（raise 语句已切换到新类型）：

* ``_ssrf.validate_url_host`` — ``ValueError`` → :class:`UnsafeTargetError`
  （第一批）
* ``mcp/_ssrf_gate.py`` — 复用 ``_ssrf`` 的归一化，门禁以「返回错误信息
  字符串」表达（无 raise 语句），无需接入。
* :class:`UnsafeTargetError` — ``fetchers/_pin.py``（DNS 解析落入拒绝段的
  TOCTOU 拦截）、``app/crawler_net.py``（重定向到不安全 host / 非
  http(s) scheme）、``app/_crawler_core.py``（stealth 路径重定向到不安全
  URL）、``mcp/server.py``（浏览器工具门禁错误转异常）、
  ``mcp/_tools_pentest.py``（scheme 白名单）（第二批）
* :class:`FetchError` — ``fetchers/fetcher.py``（重定向超限 / 无异常失败，
  同步与异步各两处）、``fetchers/dynamic.py``（JS 渲染抓取失败）、
  ``app/_crawler_core.py``（stealth fetcher 不可用）（第二批）
* :class:`ConfigError` — ``spider/autothrottle.py``（数值范围 7 处）、
  ``fetchers/session.py``（参数校验 7 处）、``fetchers/proxy.py``（策略名）、
  ``fetchers/fetcher.py``（max_redirects）、``ai/loop.py``（compress_to）、
  ``ai/llm.py``（provider 注册 2 处）、``spider/spider.py``（Request.url
  校验）、``pentest/port_scanner.py``（端口校验）（第二批）
* :class:`ParseError` — ``spider/cache.py``（缓存 meta 版本不符）、
  ``parser/visual.py``（tiles 参数 2 处）（第二批）
* :class:`InvalidStateError` — ``parser/selector.py``（save/retrieve/
  relocate 需要 adaptive=True）、``parser/storage.py``（AdaptiveStorage 已
  关闭）、``ai/recorder.py``（未 load_script 就 replay）（第二批）
* :class:`AIProviderError` — ``ai/llm.py``（缺 API key）、
  ``parser/visual.py``（VLM 调用失败 / 空响应 5 处）（第二批）

**计划接入**（后续任务逐模块替换，遵循同样的兼容式原则）：

* ``ai/_reverse_actions.py``（原路线图 ``ai/reverse_agent.py`` 的动作参数
  校验锚点，已随 ReverseAgent 拆分迁入该模块）：动作类型 / selector 缺失
  等 ``ValueError`` → :class:`ConfigError`。

**暂不纳入 / 待评估**：

* ``fetchers/_pin.py`` 的「DNS resolution returned no addresses」：
  解析结果为空的防御分支（pragma: no cover），语义非「目标不安全」，
  保持 ``ValueError``。
* ``app/_crawler_core.py`` 的「too many redirects fetching」：现状
  ``ValueError``，与 :class:`FetchError`（RuntimeError 兼容基类）类型
  基类不一致，直接切换会破坏既有 ``except ValueError`` 捕获，待评估。
* ``spider/cache.py`` 的 ``_read_body`` 三处（压缩 / 解压超限、截断
  zlib 流）：与 meta 版本不符同属损坏条目处理路径，本批仅接入点名
  锚点，其余待后续统一。
* ``spider.py`` 的 ``SpiderError`` / ``IgnoreRequest`` / ``DropItem``：
  并行任务正在重构该文件；后续让 ``class SpiderError(WebCrawlerError)``
  等（``isinstance(e, Exception)`` 兼容不受影响）。
* ``ai/recorder.py`` 的「compiled replay script invalid」：现状
  ``RuntimeError``、语义偏解析失败，接入 :class:`ParseError` 前需评估
  既有 ``except RuntimeError`` 依赖。
* ``app/_crawler_core.py`` / ``app/_ui_runner.py`` 的「cancelled by user」：
  语义独立，未来可考虑专用取消异常类。
* ``mcp/_host.py`` 的 11 处 ``NotImplementedError``：抽象方法占位
  （Python 惯例），不属于库运行错误，保持不变。
* 验证码求解（``ai/captcha.py`` / ``ai/image_captcha.py``）：失败路径均
  「返回 None / 交人工」而非 raise，无锚点；若未来改为抛错，再引入
  ``CaptchaError(WebCrawlerError, RuntimeError)``。
"""

from __future__ import annotations

__all__ = [
    "AIProviderError",
    "ConfigError",
    "FetchError",
    "InvalidStateError",
    "ParseError",
    "UnsafeTargetError",
    "WebCrawlerError",
]


class WebCrawlerError(Exception):
    """web-crawler 库内所有异常的根基类。

    语义：仅作 ``isinstance`` / ``except`` 的统一收口，生产代码不直接
    raise 它——具体错误一律抛更精确的子类。

    典型用法::

        try:
            validate_url_host(url)
        except WebCrawlerError:
            ...  # 库自身的任何错误，与环境错误精确区分
    """


class UnsafeTargetError(WebCrawlerError, ValueError):
    """SSRF 门禁拒绝：目标 host / URL 指向不安全地址。

    语义：私网、环回、链路本地（含云元数据 ``169.254.169.254`` /
    ``100.100.100.200``）、CGNAT、组播、未指定地址，``localhost`` /
    ``*.local`` 等静态黑名单主机名，或 DNS 解析落入拒绝段的目标。

    典型触发场景：``_ssrf.validate_url_host``、``fetchers/_pin.py`` 的
    TOCTOU 钉扎拦截、重定向跳向不安全 host（均已接入）。

    兼容基类 :class:`ValueError`：存量 ``except ValueError`` /
    ``pytest.raises(ValueError)`` 断言在 raise 类型切换后原样通过。
    """


class ConfigError(WebCrawlerError, ValueError):
    """配置或参数校验失败（调用方传参错误，可立即修正的使用错误）。

    典型触发场景：``spider/autothrottle.py`` 与 ``fetchers/session.py``
    的数值范围校验、``fetchers/proxy.py`` 的策略名、``ai/llm.py`` 的
    provider 注册、``Spider`` 的 ``Request.url`` 非空校验（均已接入）。

    兼容基类 :class:`ValueError`：与「非法参数值」的 Python 惯例一致。
    """


class ParseError(WebCrawlerError, ValueError):
    """解析层错误：输入内容 / 持久化数据不符合预期的结构或格式。

    典型触发场景：``parser/visual.py`` 的 tiles 参数、``spider/cache.py``
    的缓存 meta 版本不符（均已接入）。

    兼容基类 :class:`ValueError`：解析层失败多为「内容不合法」，
    与既有 ``ValueError`` 断言保持兼容。
    """


class FetchError(WebCrawlerError, RuntimeError):
    """网络抓取层失败：请求已发出但抓取流程无法完成。

    典型触发场景：``fetchers/fetcher.py`` 的重定向超限、
    ``fetchers/dynamic.py`` 的 JS 渲染抓取失败、``app/_crawler_core.py``
    的 stealth fetcher 不可用（均已接入）。

    兼容基类 :class:`RuntimeError`：存量抓取层失败统一以
    ``RuntimeError`` 表达，切换后 ``except RuntimeError`` 断言原样通过。
    """


class InvalidStateError(WebCrawlerError, RuntimeError):
    """对象状态 / 前置条件不满足（方法在当前状态下不可调用）。

    典型触发场景：``parser/selector.py`` 的 save/retrieve/relocate 需要
    ``adaptive=True``、``parser/storage.py`` 的 AdaptiveStorage 已关闭、
    ``ai/recorder.py`` 未 load_script 就 replay（均已接入）。

    兼容基类 :class:`RuntimeError`：命名对齐 asyncio.InvalidStateError
    惯例，同时保持 ``RuntimeError`` 兼容以不破坏既有断言。
    """


class AIProviderError(WebCrawlerError, RuntimeError):
    """LLM / VLM 供应商调用失败（缺 API key、接口报错、空响应）。

    典型触发场景：``ai/llm.py`` 的 provider 未配置 API key、
    ``parser/visual.py`` 的 VLM 调用失败 / 空响应（均已接入）。

    兼容基类 :class:`RuntimeError`：存量 AI 层失败统一以
    ``RuntimeError`` 表达，切换后 ``except RuntimeError`` 断言原样通过。
    """
