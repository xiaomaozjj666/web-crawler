# 快速开始

本页帮你在 10 分钟内完成：安装 → 发出第一个隐身请求 → 跑通第一只 Spider。
更深入的专题（Spider 框架、隐身网络、AI 能力）见[进阶指南](guides/spider.md)。

## 环境要求

- Python 3.10+
- 能访问目标站点的网络（库默认拒绝私网 / 环回 / 云元数据目标，
  详见[隐身与网络指南 · 安全边界](guides/stealth.md#safety)）

## 安装

推荐在虚拟环境中从源码安装：

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

`[dev]` 包含核心依赖与开发工具（pytest / ruff / mypy）。核心抓取与解析只依赖
`httpx` + `lxml`；隐身与渲染能力按需追加 extras：

| extras | 追加能力 | 说明 |
| --- | --- | --- |
| `all` | TLS 指纹隐身 + JS 渲染 | `curl_cffi` + `playwright` |
| `camoufox` | 抗指纹 Firefox | JS 逆向 Agent 默认浏览器 |
| `mcp` | MCP 服务 / CLI | 隐含 `camoufox` |
| `captcha` | 图片验证码识别 | `ddddocr`（文本 OCR / 滑块） |
| `crypto` | AES-128 分段解密 | `pycryptodome` |
| `visual` | 图片解码预处理 | `Pillow` |

```bash
pip install -e ".[all]"
playwright install chromium      # 仅 DynamicFetcher / StealthyFetcher 需要
```

!!! warning "不要直接 `pip install web-crawler`"
    PyPI 上同名包属于另一个无关项目，会装到错误的库。请始终使用上面的
    源码安装方式。

安装成功后会自动注册四个命令：`web-crawler`（资源下载 CLI）、`crawler-ui`
（本地 Web UI）、`web-crawler-mcp`（MCP 服务）、`web-crawler-reverse`
（JS 逆向 CLI）。

## 第一个请求：隐身抓取

`Fetcher` 通过 `curl_cffi` 重放真实浏览器的 TLS/JA3 指纹，让请求在网络层
与 Chrome 难以区分：

```python
from web_crawler import Fetcher

with Fetcher(impersonate="chrome131", timeout=30.0) as f:
    resp = f.get("https://example.com")
    print(resp.status)
    print(resp.css_first("h1").text)
```

`resp` 是统一的 [`Response`](api-reference.md#web_crawler.response.Response) 对象——无论底层用哪个
fetcher，`.css()` / `.xpath()` / `.json()` 助手全都可用：

```python
for link in resp.css("a"):
    print(link.attr("href"))

data = resp.json()          # JSON 响应直接解析
abs_url = resp.urljoin("../about")   # 相对 URL 拼接
```

## 解析：自适应选择器

`Selector` 的招牌能力是**元素指纹重定位**——站点改版导致选择器失效时，
已保存的指纹会按结构相似度自动找回对应元素：

```python
from web_crawler import Selector, AdaptiveStorage

storage = AdaptiveStorage()  # 默认存于 ~/.web_crawler/adaptive.sqlite3

# 首次运行：保存元素指纹
page = Selector(html_v1, url="https://shop.example.com", adaptive=True, storage=storage)
title = page.css_first("#product-title", auto_save=True)

# 站点改版、id 消失后……
page2 = Selector(html_v2, url="https://shop.example.com", adaptive=True, storage=storage)
title = page2.css_first("#product-title", adaptive=True)  # 按相似度自动重定位
```

不需要自适应能力时，`Selector(html)` 可当普通 lxml 选择器使用，零额外开销。

## 爬取整站：Spider

`Spider` 是回调驱动的爬虫框架——子类声明起始 URL 与 `parse` 回调，
回调 `yield` 字典即产出 item，`yield` `Request` 即调度新请求：

```python
from web_crawler import Spider, Request, Fetcher


class QuotesSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]
    allowed_domains = ["quotes.toscrape.com"]

    def parse(self, response):
        for quote in response.css("div.quote"):
            yield {
                "text": quote.css_first(".text").text,
                "author": quote.css_first(".author").text,
            }
        nxt = response.css_first("li.next > a")
        if nxt:
            yield Request(response.urljoin(nxt.attr("href")))


items = QuotesSpider(fetcher=Fetcher(impersonate="chrome131")).run()
print(f"共抓取 {len(items)} 条 quote")
```

爬大站前建议先打开礼貌开关（限速 / 重试 / robots）：

```python
class RobustSpider(Spider):
    start_urls = ["https://example.com/"]
    max_retries = 2                    # 失败指数退避重试
    per_domain_delay = {"example.com": 1.0}   # 同域最小间隔（秒）
    respect_robots = True              # 回调产出的请求先过 robots.txt
```

## 遇到 JS 重页面？

`DynamicFetcher` 驱动真实浏览器渲染；`StealthyFetcher` 再叠加人类化输入
轨迹与 Cloudflare 感知：

```python
from web_crawler import DynamicFetcher, StealthyFetcher

with DynamicFetcher(headless=True, wait_selector="div.content") as f:
    resp = f.fetch("https://spa.example.com")

with StealthyFetcher(google_search=True, humanize=True) as f:
    resp = f.fetch("https://protected.example.com")
```

## 出错时怎么办

库内所有错误都在 `WebCrawlerError` 异常族下，一次 `except` 即可收口；
子类同时继承 `ValueError` / `RuntimeError`，旧代码的捕获逻辑不受影响：

```python
from web_crawler import Fetcher, WebCrawlerError, UnsafeTargetError, FetchError

try:
    with Fetcher() as f:
        resp = f.get("https://example.com")
except UnsafeTargetError as e:
    print(f"目标不安全: {e}")   # 私网/环回/云元数据等
except FetchError as e:
    print(f"抓取失败: {e}")     # 重定向超限/渲染失败等
except WebCrawlerError as e:
    print(f"其他库错误: {e}")
```

## 下一步

- [Spider 爬虫框架指南](guides/spider.md) — 中间件、item 管道、去重、
  暂停续跑、限速与统计导出
- [隐身与网络指南](guides/stealth.md) — TLS 指纹定制、会话池、代理轮换、
  DNS 钉扎与安全边界
- [AI 能力指南](guides/ai.md) — AIExtractor、AIScrapeAgent、图片验证码、
  JS 逆向 Agent
- [API 参考](api-reference.md) — 全部 59 个公开符号的自动生成文档
- [JS 逆向 Agent](reverse-agent.md) — 完整逆向手册
