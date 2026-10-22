# API 参考

本节由 [mkdocstrings](https://mkdocstrings.github.io/) 从源码 docstring 自动生成。
所有公开 API 均通过 `web_crawler` 顶层包导出（`__all__` 共 59 个公共符号），
全部为懒加载——`import web_crawler` 不会强制加载 `playwright` / `curl_cffi`
等可选依赖，重型子模块仅在对应符号首次访问时才解析。

!!! tip "导入约定"
下列所有符号均可直接从顶层导入，例如
`from web_crawler import Fetcher, Spider, AIExtractor`。
`Adaptor` 是 `Selector` 的官方别名（对齐 Scrapling 命名），二者为同一对象，
文档统一在 Selector 一节呈现，不再单独渲染。

## 顶层包 {: #top }

`web_crawler` 包本身：`__version__` 版本号、懒加载机制说明与快速上手示例。

::: web_crawler

## 核心抓取 {: #fetchers }

主力 fetcher 家族与网络基础设施：`Fetcher` / `AsyncFetcher` 基于 `curl_cffi`
重放真实浏览器的 TLS/JA3 指纹与 HTTP/2 帧序（缺失时降级 `httpx` 并告警）；
`DynamicFetcher` / `StealthyFetcher` / `CamoufoxFetcher` 覆盖 JS 渲染与反反爬；
`SessionPool` / `ProxyPool` 负责会话与代理轮换；`BaseFetcher` 是全部 fetcher
的共享配置基类；`Response` 是统一返回的结果对象。

`Fetcher` — 主力同步 HTTP fetcher，同时暴露 `async_get` / `async_request`
异步方法与 `download()` 流式下载（断点续传，复用 SSRF / DNS 钉扎 / 代理 /
会话池全套校验链）。

::: web_crawler.fetchers.fetcher.Fetcher

`AsyncFetcher` — 纯异步 API 面的 fetcher，隐身能力与 `Fetcher` 完全一致。

::: web_crawler.fetchers.fetcher.AsyncFetcher

`BaseFetcher` — 所有 fetcher 的共享配置与 `Response` 构建基类。

::: web_crawler.fetchers._base.BaseFetcher

`Response` — 统一响应对象，内置 `.css()` / `.xpath()` / `.json()` / `urljoin()` 助手。

::: web_crawler.response.Response

`SessionPool` — `curl_cffi` 会话池：指纹随会话轮换（round_robin / random）、
LIFO 复用、空闲回收、连续失败淘汰与预热。

::: web_crawler.fetchers.session.SessionPool

`ProxyPool` — 轮询 / 随机代理轮换，按代理失败次数冷却。

::: web_crawler.fetchers.proxy.ProxyPool

`DynamicFetcher` — 驱动 Playwright/Chromium 渲染 JS 重页面，支持资源屏蔽
与选择器等待。

::: web_crawler.fetchers.dynamic.DynamicFetcher

`StealthyFetcher` — 反反爬：注入指纹补丁 JS、人类化鼠标/滚动轨迹，尽力处理
Cloudflare 挑战。

::: web_crawler.fetchers.stealthy.StealthyFetcher

`CamoufoxFetcher` — 基于 Camoufox 抗指纹 Firefox 的渲染 fetcher（JS 逆向
Agent 的默认浏览器）。

::: web_crawler.fetchers.camoufox.CamoufoxFetcher

## 解析与自适应 {: #parser }

`Selector` 是基于 `lxml` + `cssselect` 的自适应解析器，对齐 Scrapling `Adaptor`
API；指纹引擎与存储独立成模块，可单独复用。

`Selector` — 自适应选择器：CSS / XPath / 正则 / 文本查找、完整 DOM 遍历、
元素指纹与结构相似度重定位（`Adaptor` 即本类的别名）。

::: web_crawler.parser.selector.Selector

`Adaptors` — 批量选择器容器，提供 `css` / `xpath` 等 `ResultList` 批量辅助方法。

::: web_crawler.parser.selector.Adaptors

`AdaptiveStorage` — 指纹持久化存储：线程安全 SQLite（默认
`~/.web_crawler/adaptive.sqlite3`），支持 `save` / `retrieve`。

::: web_crawler.parser.storage.AdaptiveStorage

`compute_fingerprint` — 计算 lxml 元素的结构指纹（路径签名 + 文本 + class 特征）。

::: web_crawler.parser.adaptive.compute_fingerprint

`similarity_score` — 比较两份指纹的结构相似度（0~1），重定位引擎的打分核心。

::: web_crawler.parser.adaptive.similarity_score

`VisualExtractor` — PixelRAG 风格可视化提取：页面截图瓦片交给视觉 LLM 分析。

::: web_crawler.parser.visual.VisualExtractor

`LinkExtractor` — 超链接提取与过滤（`allow`/`deny` 正则、域白/黑名单、
去重保序、canonicalize），对标 Scrapy LinkExtractor；`Link` 是其产出的
数据类（url / text / attrs）。

::: web_crawler.parser.links.LinkExtractor

::: web_crawler.parser.links.Link

## Spider 框架 {: #spider-framework }

回调驱动的爬虫框架：优先级调度、域名过滤、指纹去重、失败重试、robots 遵守、
域级并发槽位、暂停/续跑、下载中间件与 item 管道（导出 / 校验）、自适应限速
与统计导出，另有 `CrawlSpider` / `SitemapSpider` 两个发现策略子类。深度用法
见 [Spider 爬虫框架指南](guides/spider.md)。

`Spider` — 爬虫基类：子类定义 `start_urls` 与 `parse` 回调即可运行。

::: web_crawler.spider.spider.Spider

`Request` — 已调度的请求（dataclass）：`callback` / `priority` / `meta` /
`retries` / `dont_filter` 等字段。

::: web_crawler.spider.spider.Request

`SpiderStats` — 轻量运行统计（pages / items / failed / elapsed / autothrottle 快照）。

::: web_crawler.spider.spider.SpiderStats

`DupeFilter` — 请求去重器：按 `method + url + body` 的 SHA1 指纹判定重复，
可经 `dupefilter=` 构造参数替换为自定义实现。

::: web_crawler.spider.spider.DupeFilter

`DownloaderMiddleware` — 下载中间件基类：`process_request` 返回 `Response`
可短路下载，抛 `IgnoreRequest` 丢弃请求；`process_response` 可变换响应。

::: web_crawler.spider.spider.DownloaderMiddleware

`ItemPipeline` — item 管道基类：回调产出的每条 item 依次经过各管道变换/过滤。

::: web_crawler.spider.spider.ItemPipeline

`FeedExportPipeline` — item 落盘导出管道：JSON 快照 / JSONL 追加 / CSV；
`Spider(feed_output=...)` 便捷参数等价于 Scrapy 的 `FEEDS` 配置。

::: web_crawler.spider.feed.FeedExportPipeline

`ValidationPipeline` — item 校验管道：pydantic `BaseModel`（校验 + 规范化
透传，延迟导入不破懒加载）或无依赖 dict schema（`type` / `required` /
`default` DSL）；失败抛 `DropItem` 并计入 `items_dropped` 统计。

::: web_crawler.spider.validation.ValidationPipeline

`CacheMiddleware` — GET 响应磁盘缓存中间件：TTL、离线回放、原子写与
解压炸弹防护。

::: web_crawler.spider.cache.CacheMiddleware

`AutoThrottle` — 按域自适应限速器：EMA 延迟反馈 + 失败惩罚 + 目标并发换算
（对齐 Scrapy 语义）。

::: web_crawler.spider.autothrottle.AutoThrottle

`RobotsPolicy` — `robots.txt` 解析与 `allowed` / `crawl_delay` 查询
（`Spider` 与 `AIScrapeAgent` 共用同一实现）。

::: web_crawler.robots.RobotsPolicy

`IgnoreRequest` — 中间件抛出以丢弃某个请求（计入 `requests_ignored`，
不打断运行）。

::: web_crawler.spider.spider.IgnoreRequest

`DropItem` — item 管道抛出以丢弃某条 item（不计入 `items_scraped`）。

::: web_crawler.spider.spider.DropItem

`SpiderError` — 致命 spider 引擎错误（回调错误、状态损坏），是
`IgnoreRequest` / `DropItem` 的父类。

::: web_crawler.spider.spider.SpiderError

`CrawlSpider` — 规则驱动的跟随爬虫（对标 Scrapy CrawlSpider）：`rules`
声明 `Rule(link_extractor, callback, follow)` 即得递归遍历，跟随请求自动
经基类域过滤 / 去重 / robots 链路。

::: web_crawler.spider.crawl.CrawlSpider

::: web_crawler.spider.crawl.Rule

`SitemapSpider` — sitemap 驱动的爬虫（对标 Scrapy 同名概念）：
sitemapindex 递归展开、`sitemap_rules` 正则分发回调、robots.txt 的
`Sitemap:` 行自动发现种子。

::: web_crawler.spider.sitemap.SitemapSpider

## 异常体系 {: #exceptions }

以 `WebCrawlerError` 为根的统一异常族：`except WebCrawlerError` 一次捕获
库自身的错误，与环境错误精确区分。每个子类同时继承 `ValueError` /
`RuntimeError`（兼容式多继承），既有捕获代码零破坏。

`WebCrawlerError` — 异常体系根，所有库内异常的公共父类。

::: web_crawler.exceptions.WebCrawlerError

`UnsafeTargetError` — 目标不安全：私网 / 环回 / 云元数据地址，或协议
白名单（http/https）之外。

::: web_crawler.exceptions.UnsafeTargetError

`FetchError` — 抓取失败：重定向超限、JS 渲染失败、传输层错误。

::: web_crawler.exceptions.FetchError

`ParseError` — 解析失败：非法选择器、编码问题。

::: web_crawler.exceptions.ParseError

`ConfigError` — 配置错误：非法参数值、未知 LLM provider。

::: web_crawler.exceptions.ConfigError

`InvalidStateError` — 状态错误：对已关闭对象操作、非法生命周期迁移。

::: web_crawler.exceptions.InvalidStateError

`AIProviderError` — LLM 供应商层错误：缺 API key、请求失败。

::: web_crawler.exceptions.AIProviderError

## AI 能力 {: #ai }

可插拔 LLM 层与 AI 辅助抓取：自然语言生成选择器、自带速率控制的礼貌爬虫、
图片验证码识别。配置与实战见 [AI 能力指南](guides/ai.md)。

`LLMProvider` — LLM 供应商协议（Protocol）：`chat(messages) -> LLMResponse`。

::: web_crawler.ai.llm.LLMProvider

`OpenAICompatibleProvider` — 任意 OpenAI 兼容端点的通用实现，可指定
`base_url` / `model` / `api_key`。

::: web_crawler.ai.llm.OpenAICompatibleProvider

`DeepSeekProvider` — 默认供应商，指向 DeepSeek-V4.1-Flash。

::: web_crawler.ai.llm.DeepSeekProvider

`LLMMessage` — 单条对话消息（role / content），支持构造 vision 图片消息。

::: web_crawler.ai.llm.LLMMessage

`LLMResponse` — 供应商返回：content / model / usage 等字段。

::: web_crawler.ai.llm.LLMResponse

`get_provider` — 按名称实例化已注册供应商（默认 `deepseek`），关键字参数
透传给构造器。

::: web_crawler.ai.llm.get_provider

`register_provider` — 以给定名称注册新的供应商工厂（大小写不敏感）。

::: web_crawler.ai.llm.register_provider

`available_providers` — 返回已注册的供应商名称列表。

::: web_crawler.ai.llm.available_providers

`AIExtractor` — 自然语言字段描述转换为校验过的 CSS 选择器，含空字段自愈
重试轮次。

::: web_crawler.ai.extractor.AIExtractor

`ExtractionResult` — `AIExtractor.extract` 的结果：data / selectors /
missing / rounds。

::: web_crawler.ai.extractor.ExtractionResult

`AIScrapeAgent` — 自带速率控制的礼貌 AI 爬虫：robots 遵守、429/503 指数退避
（遵循 `Retry-After`）、反爬检测移交人工。

::: web_crawler.ai.agent.AIScrapeAgent

`ScrapeResult` — `AIScrapeAgent.scrape` 的结果：data / selectors /
needs_human / block_reason。

::: web_crawler.ai.agent.ScrapeResult

`detect_block` — 检测响应是否为反爬/验证码墙，返回原因字符串或 `None`。

::: web_crawler.ai.agent.detect_block

`ImageCaptchaSolver` — 图片验证码求解器（无需浏览器）：文本 OCR、滑块缺口
定位、点选坐标识别，后端按 ddddocr → numpy 模板匹配 → LLM Vision 依次降级。

::: web_crawler.ai.image_captcha.ImageCaptchaSolver

## JS 逆向套件 {: #reverse }

`ReverseAgent` 的组成部件：JS Hook 注入、webpack 静态分析、验证码检测与处理
编排。完整使用指南、动作矩阵与配置项见 [JS 逆向 Agent](reverse-agent.md)。

`HookLibrary` — JS Hook 脚本库：fetch / XHR / cookie / `crypto.subtle` /
webpack / console 等注入脚本。

::: web_crawler.ai.hooks.HookLibrary

`generate_combined_script` — 生成多 个 Hook 合并后的注入脚本。

::: web_crawler.ai.hooks.generate_combined_script

`JSAnalyzer` — JS 静态分析器：webpack 模块抽取 + LLM 反混淆。

::: web_crawler.ai.analyzer.JSAnalyzer

`JSFragment` — 分析产物中的一个 JS 片段（来源 / 代码 / 语义标注）。

::: web_crawler.ai.analyzer.JSFragment

`CaptchaType` — 验证码类型枚举（图片 / 滑块 / 点选 / reCAPTCHA /
hCaptcha / GeeTest 等）。

::: web_crawler.ai.captcha.CaptchaType

`CaptchaManager` — 验证码检测与处理编排：浏览器内人类化交互 +
`ImageCaptchaSolver` 图片求解（`enable_image_captcha=True` 时自动注入）。

::: web_crawler.ai.captcha.CaptchaManager

`ReverseAgent` — JS 逆向 Agent 主循环，编排浏览器、Hook、AI 分析器与验证码
处理，形成「观察-思考-行动」的自主循环；同步入口 `run` 与异步入口 `arun`
共享同一套配置。

::: web_crawler.ai.reverse_agent.ReverseAgent

`ReverseAgentConfig` — Agent 全量配置：hooks / target_params / max_steps /
guard / checkpoint / screenshot / humanize_input 等能力独立开关。

::: web_crawler.ai.reverse_agent.ReverseAgentConfig

## MCP 服务器 {: #mcp }

`ReverseMCPServer` — JSON-RPC over stdio 的 MCP 服务，向 Claude Desktop /
Cursor 等 AI 客户端暴露 JS 逆向与授权侦察工具。

::: web_crawler.mcp.server.ReverseMCPServer
