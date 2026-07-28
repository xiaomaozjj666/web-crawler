# web-crawler

> Scrapling 风格隐身爬虫库 + JS 逆向 Agent

`web-crawler` 是一个面向生产环境的 Python 爬虫库，对齐 [Scrapling](https://github.com/D4Vinci/Scrapling) 的核心 API，并在其基础上扩展出 **JS 逆向 Agent**、**TLS 指纹定制**、**多标签页管理**、**人类化输入轨迹** 等高级能力。

## 核心特性

- **自适应解析器** — `Selector` 基于 `lxml`，支持元素指纹 + 结构相似度重定位，站点改版后指纹自动重定位元素（Scrapling 招牌特性）。
- **隐身 HTTP** — `Fetcher` 使用 `curl_cffi` 重放真实浏览器的 TLS/JA3 指纹与 HTTP/2 帧序，从网络层看与 Chrome 难以区分；并支持 JA4 指纹细粒度定制。
- **JS 渲染** — `DynamicFetcher` 驱动 Playwright/Chromium 渲染动态页面，按选择器等待、按资源类型屏蔽。
- **反爬对抗** — `StealthyFetcher` 注入指纹补丁 JS、人类化鼠标/滚动，尽力解决 Cloudflare 挑战。
- **代理轮换** — `ProxyPool` 支持轮询/随机策略与失败冷却。
- **Spider 框架** — `Spider`/`Request` 提供回调分发、优先级调度、域名过滤、去重、JSON 暂停/续跑。
- **统一 `Response`** — 所有 fetcher 返回同一 `Response`，自带 `.css()` / `.xpath()` / `.json()` 辅助。
- **AI 抽取** — `AIExtractor` 把自然语言字段 schema 转成可验证的 CSS 选择器；`AIScrapeAgent` 编排 fetch + extract，遵守 robots.txt、429/503 退避。
- **JS 逆向 Agent** — `ReverseAgent` 跑 observe→think→act 循环，注入 JS Hook、捕获网络流量、拆分 webpack bundle，再让 LLM 反混淆并重写签名算法。

## 快速开始

### 安装

```bash
pip install -e ".[dev]"          # 解析器 + 测试 + lint/types
pip install -e ".[all]"          # + curl_cffi TLS 隐身 + Playwright JS 渲染
pip install -e ".[camoufox]"     # + Camoufox 抗指纹 Firefox
pip install -e ".[mcp]"          # + MCP server / CLI（含 camoufox）
playwright install chromium       # DynamicFetcher / StealthyFetcher 需要
```

### 隐身 HTTP 抓取

```python
from web_crawler import Fetcher

with Fetcher(impersonate="chrome131", timeout=30.0) as f:
    resp = f.get("https://example.com")
    print(resp.status, resp.css_first("h1").text)
```

### 自适应解析（站点改版后自动重定位）

```python
from web_crawler import Selector, AdaptiveStorage

storage = AdaptiveStorage()  # ~/.web_crawler/adaptive.sqlite3

# 第一次：保存元素指纹
page = Selector(html_v1, url="https://shop.example.com", adaptive=True, storage=storage)
title = page.css_first("#product-title", auto_save=True)

# 站点改版后 id 没了，靠相似度重定位
page2 = Selector(html_v2, url="https://shop.example.com", adaptive=True, storage=storage)
relocated = page2.css_first("#product-title", adaptive=True)
```

### Spider 框架

```python
from web_crawler import Spider, Request, Fetcher


class QuotesSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]
    allowed_domains = ["quotes.toscrape.com"]

    def parse(self, response):
        for quote in response.css("div.quote"):
            yield {"text": quote.css_first(".text").text, "author": quote.css_first(".author").text}
        nxt = response.css_first("li.next > a")
        if nxt:
            yield Request(response.urljoin(nxt.attr("href")))


items = QuotesSpider(fetcher=Fetcher(impersonate="chrome131")).run()
```

### JS 逆向 Agent（MCP 接入 Claude Desktop / Cursor）

```bash
pip install -e ".[mcp]"
export DEEPSEEK_API_KEY=sk-...        # Windows: set DEEPSEEK_API_KEY=...
web-crawler-mcp                       # 通过 stdio 跑 JSON-RPC
```

在 AI 客户端的 MCP 配置里注册：

```json
{
  "mcpServers": {
    "js-reverse": {
      "command": "web-crawler-mcp",
      "env": {"DEEPSEEK_API_KEY": "sk-..."}
    }
  }
}
```

## 文档导航

- [架构说明](architecture.md) — 模块树与各组件职责
- [JS 逆向 Agent](reverse-agent.md) — 完整使用指南与配置项
- [API 参考](api-reference.md) — 公开 API 自动生成文档

## 合规说明

Agent 仅模拟正常用户交互，不破解图片验证码、不伪造登录凭证、不绕过付费墙。
当页面返回 401/403 或出现无法处理的验证码时，Agent 会停止并返回"人工接管"状态。

## License

MIT. 见 [LICENSE](https://github.com/xiaomaozjj666/web-crawler/blob/main/LICENSE)。
