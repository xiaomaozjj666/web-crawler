# API 参考

本节由 [mkdocstrings](https://mkdocstrings.github.io/) 从源码 docstring 自动生成。
所有公开 API 均通过 `web_crawler` 顶层包导出，懒加载，`import web_crawler` 不会
强制加载 `playwright` / `curl_cffi` 等可选依赖。

## 顶层导出

::: web_crawler

## Fetcher

`Fetcher` 是主力 HTTP fetcher，基于 `curl_cffi` 重放真实浏览器的 TLS/JA3 指纹与
HTTP/2 帧序；`curl_cffi` 缺失时自动降级到 `httpx`（带 warning，无指纹能力）。

支持 `impersonate` 浏览器预设与 `ja4_fingerprint` 细粒度 TLS 指纹定制。

::: web_crawler.fetchers.fetcher.Fetcher

::: web_crawler.fetchers.fetcher.AsyncFetcher

## Selector

`Selector` 是基于 `lxml` + `cssselect` 的自适应解析器，对齐 Scrapling `Adaptor` API。
支持 CSS / XPath / 正则 / 文本查找，完整 DOM 遍历，以及元素指纹 + 相似度重定位。

::: web_crawler.parser.selector.Selector

## ReverseAgent

`ReverseAgent` 是 JS 逆向 Agent 主循环，编排浏览器、Hook、AI 分析器、验证码处理，
形成"观察-思考-行动"的自主循环。同步入口 `run` 与异步入口 `arun` 共享同一套配置。

::: web_crawler.ai.reverse_agent.ReverseAgent

::: web_crawler.ai.reverse_agent.ReverseAgentConfig

## LLM Provider

LLM provider 抽象层，OpenAI 兼容 chat-completions 接口。默认 `DeepSeekProvider`
指向 DeepSeek-V4-Pro。

::: web_crawler.ai.llm.LLMProvider

::: web_crawler.ai.llm.DeepSeekProvider

## Spider

回调驱动的爬虫框架，支持优先级调度、域名过滤、URL 去重、JSON 暂停/续跑。

::: web_crawler.spider.spider.Spider

::: web_crawler.spider.spider.Request
