# Spider 爬虫框架指南

`Spider` 是回调驱动的爬虫框架：子类声明起始 URL 与回调，引擎负责调度、
去重、限速、重试、中间件与统计。本页覆盖全部框架级机制；API 细节见
[API 参考 · Spider 框架](../api-reference.md#spider-framework)。

## 最小可用的 Spider

```python
from web_crawler import Spider, Request, Fetcher


class QuotesSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]
    allowed_domains = ["quotes.toscrape.com"]

    def parse(self, response):
        for quote in response.css("div.quote"):
            yield {"text": quote.css_first(".text").text}
        nxt = response.css_first("li.next > a")
        if nxt:
            yield Request(response.urljoin(nxt.attr("href")))


items = QuotesSpider(fetcher=Fetcher(impersonate="chrome131")).run()
```

回调里 `yield` 的规则只有两条：

- `yield Request(...)` → 请求进入调度队列，可指定其他回调；
- `yield {…}`（任意非 Request 对象）→ 视为 item，被收集进返回列表
  （并流经 item 管道，见[下文](#item-pipeline)）。

## Request：调度单位

`Request` 是 dataclass，常用字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `url` | 必填 | 目标地址 |
| `method` | `"GET"` | HTTP 方法 |
| `callback` | `"parse"` | Spider 子类上的回调方法名 |
| `headers` / `body` | `None` | 请求头 / 请求体 |
| `priority` | `0` | 越大越先调度（heap 取负实现） |
| `meta` | `{}` | 透传到 `response.meta`，供回调间传递状态 |
| `retries` | `0` | 引擎重试计数（配合 `max_retries`） |
| `dont_filter` | `False` | `True` 时跳过去重器 |

多级页面用 `callback` + `meta` 串联：

```python
def parse(self, response):
    yield Request(detail_url, callback="parse_detail", meta={"source": response.url})


def parse_detail(self, response):
    yield {"source": response.meta["source"], "title": response.css_first("h1").text}
```

## 域名过滤与去重

- `allowed_domains = ["example.com"]`：引擎只调度该域（含子域）的请求，
  其余直接丢弃；空列表表示不过滤。
- 去重由 `DupeFilter` 完成，指纹 = `method + url + body` 的 SHA1——同一 URL
  的不同 method / body（如不同分页参数）不会互相误杀。

需要磁盘持久化等自定义行为时，实现 `request_seen(request) -> bool` 并注入：

```python
class DiskDupeFilter(DupeFilter):
    def __init__(self, path):
        self.seen = load_seen_set(path)  # 启动时加载

    def request_seen(self, request):
        fp = self.fingerprint(request)
        if fp in self.seen:
            return True
        self.seen.add(fp)
        return True  # 占位；实际实现记得返回 False 并落盘


spider = MySpider(fetcher=f, dupefilter=DiskDupeFilter("seen.txt"))
```

## 失败重试

```python
class RobustSpider(Spider):
    max_retries = 2  # 下载失败后的最大重试次数（指数退避）；默认 0 不重试
```

重试针对**下载失败**（网络错误、非 2xx 状态码），带指数退避；回调内抛出的
异常不属于重试范畴（会记入统计并继续处理后续请求）。

## 限速：三个来源取最保守

同域相邻请求的实际延迟由三个来源的最大值决定（`_throttle_delay_for`）：

1. `per_domain_delay` — 静态下限，用户显式承诺的礼貌底线；
2. `AutoThrottle` — 自适应值，只允许比静态下限更保守；
3. robots `Crawl-delay` — 站点方显式声明的礼貌底线（仅
   `respect_robots=True` 时生效），同样只抬高、永不放宽。

```python
# 静态：同域最小间隔 1 秒（设置了延迟的域在 stream() 中会被串行化）
class SlowSpider(Spider):
    per_domain_delay = {"example.com": 1.0, "api.example.com": 0.5}


# 自适应：站点变慢自动放慢、变快自动提速（对齐 Scrapy AutoThrottle 语义）
from web_crawler import AutoThrottle

throttle = AutoThrottle(target_concurrency=2.0, alpha=0.3)
items = MySpider(fetcher=Fetcher(), autothrottle=throttle).run()
```

`AutoThrottle` 关键参数：`alpha`（EMA 平滑系数，默认 0.3）、
`target_concurrency`（希望同域保持的并发数，`target_delay = EMA / 该值`）、
`min_delay` / `max_delay`（延迟上下限）、`penalty_factor`（失败惩罚放大系数）。

## 遵守 robots.txt

```python
class PoliteSpider(Spider):
    respect_robots = True  # 回调产出的请求先过 robots.txt（默认关闭）
    user_agent = "my-bot"  # robots.txt 检查使用的 UA（"*" 取所有 UA 规则的保守并集）
```

行为细节：robots 拉取失败时保守视为允许（不让一次瞬时故障拦截整个爬取），
404 视为全允许；`Crawl-delay` 并入上面的三源延迟取最保守者。实现与
`AIScrapeAgent` 共用同一个 `RobotsPolicy`。

## 下载中间件

`DownloaderMiddleware` 在请求发出前 / 响应返回后介入下载流程，按声明顺序执行：

- `process_request(request, spider)` 返回 `None` 放行下载；返回 `Response`
  直接短路（不再发请求）；抛 `IgnoreRequest` 丢弃该请求（计入
  `requests_ignored`）；
- `process_response(response, request, spider)` 收到下载结果，返回
  （可替换的）`Response`。

```python
from web_crawler import DownloaderMiddleware, IgnoreRequest


class BlockListMiddleware(DownloaderMiddleware):
    def process_request(self, request, spider):
        if any(kw in request.url for kw in ("/logout", "/admin")):
            raise IgnoreRequest(f"blocked: {request.url}")
        return None


class MySpider(Spider):
    middlewares = [BlockListMiddleware]  # 类或实例均可
```

### 内置缓存：CacheMiddleware

`CacheMiddleware` 为 GET 响应提供磁盘缓存（`<sha256>.meta.json` +
zlib 压缩 `.body`，临时文件 + `os.replace` 原子写）：

```python
from web_crawler import CacheMiddleware


class CachedSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]
    # 1 小时 TTL；离线回放时不发任何网络请求
    middlewares = [CacheMiddleware(cache_dir=".web_crawler_cache", ttl=3600)]

    def parse(self, response):
        yield {"title": response.css_first("title").text}


# 离线回放：同目录已有缓存时绝不发网络请求（未命中抛 IgnoreRequest）
class ReplaySpider(Spider):
    middlewares = [CacheMiddleware(cache_dir=".web_crawler_cache", offline=True)]
```

要点：仅缓存 GET；默认只缓存 2xx（`cache_4xx=True` 可纳入 4xx，5xx 永不缓存）；
`ignore_lookup=True` 跳过查找强制走网络但结果仍回写（缓存刷新模式）；
`max_body_size`（默认 64MB）钳制解压后体积，防 zlib 炸弹。缓存的信任模型与
残余风险详见 [SECURITY.md · 磁盘缓存信任模型](https://github.com/xiaomaozjj666/web-crawler/blob/master/SECURITY.md)。

## item 管道 {: #item-pipeline }

回调产出的每条 item 依次流经 `item_pipelines` 声明的管道链：返回值是变换后的
item，返回 `None` 或抛 `DropItem` 即丢弃该条（不计入 `items_scraped`）：

```python
from web_crawler import ItemPipeline, DropItem


class CleanPipeline(ItemPipeline):
    def process_item(self, item, spider):
        if not item.get("name"):
            raise DropItem("empty name")
        return {**item, "name": item["name"].strip()}


class MySpider(Spider):
    item_pipelines = [CleanPipeline]  # 类或实例均可，按声明顺序执行
```

## 暂停与续跑

`run()` 支持基于 JSON 状态文件的暂停 / 恢复——队列与已见集合都会持久化：

```python
# 首次运行：带上 state_file，中断（含回调异常）时会自动落盘
spider.run(state_file="state.json", max_requests=100)

# 继续跑：从状态文件恢复队列与去重集合
items = MySpider(fetcher=Fetcher()).run(state_file="state.json", resume=True)
```

运行中也可调用 `spider.pause()` 通知循环在当前批次后持久化并停止。

## feed 导出：item 落盘

`stats_output` 落的是运行度量，`feed_output` 落的是业务数据本身——一行
便捷参数即可把 item 导出为 JSON / JSONL / CSV（对标 Scrapy 的 `FEEDS` 配置）：

```python
spider = QuotesSpider(fetcher=Fetcher(impersonate="chrome131"), feed_output="quotes.jsonl")
items = spider.run()  # item 逐条追加到 quotes.jsonl
```

也可声明在 `item_pipelines` 上与其他管道串联（先变换、后落盘）：

```python
from web_crawler import FeedExportPipeline


class QuotesSpider(Spider):
    item_pipelines = [CleanPipeline, FeedExportPipeline("quotes.jsonl")]
```

格式语义：`.json` 单次快照（覆盖写，空运行产出 `[]`）；`.jsonl` 逐条追加
（`ensure_ascii=False`，多次运行天然聚合，适合断点续爬累积数据）；`.csv`
首条 item 定列（表头），缺失列补空串、多余列记 warning 丢弃。

导出容错与统计导出同源——「导出绝不连累已抓数据」：写盘失败（磁盘满 /
权限）只记 warning 不抛异常，item 照常通过管道；父目录不存在时自动逐级
创建；未知后缀在构造时抛 `ConfigError`。

## 规则跟随：LinkExtractor + CrawlSpider

整站遍历不必在 `parse` 里手写 `css("a")` + `urljoin` + 去重样板——
`LinkExtractor` 提取并过滤链接，`CrawlSpider` 用 `rules` 声明「哪些链接
跟进、命中后交给哪个回调」（对标 Scrapy 同名概念）：

```python
from web_crawler import CrawlSpider, Rule, LinkExtractor


class ArticleSpider(CrawlSpider):
    start_urls = ["https://blog.example.com/"]
    allowed_domains = ["blog.example.com"]
    rules = [
        # 列表页与翻页：只跟进，不产 item
        Rule(LinkExtractor(allow=(r"/posts/$", r"/page/\d+/"))),
        # 文章详情页：提取 item；follow=False 不再从文章页继续提取
        Rule(LinkExtractor(allow=(r"/posts/\d+/$")), callback="parse_article", follow=False),
    ]

    def parse_article(self, response):
        return {"title": response.css_first("h1::text")}


items = ArticleSpider(fetcher=Fetcher(impersonate="chrome131")).run()
```

`LinkExtractor` 的过滤参数：`allow` / `deny`（URL 正则白/黑名单，`re.search`
语义，deny 优先）、`allow_domains` / `deny_domains`（后缀匹配，`example.com`
命中 `sub.example.com`）、`unique`（按规范化 URL 去重保序）、`canonicalize`
（查询参数排序 + 去 fragment）。跟随请求自动经过 Spider 基类的域过滤 /
`DupeFilter` 去重 / robots 检查链路——环路（A→B→A）不会死循环。

`CrawlSpider` 继承 `Spider` 全部能力（`feed_output` / `autothrottle` /
`CacheMiddleware` / 暂停续跑等均可直接组合）。

## 统计导出

构造参数 `stats_output` 指定导出路径：`.jsonl` 后缀按行追加（多次运行可
聚合分析），其他后缀写单次 JSON 快照（覆盖写）。正常结束、异常与暂停均会落盘。

```python
spider = MySpider(fetcher=Fetcher(), stats_output="stats.jsonl")
items = spider.run()
# stats.jsonl 每行：pages_crawled / items_scraped / requests_failed /
#                   requests_ignored / elapsed_seconds / autothrottle 快照
```

运行结束的统计同时挂在 `spider.stats`（`SpiderStats` 实例）上，可直接读取。

## 流式调度：stream()

`run()` 适合批量抓取后一次性处理；`stream()` 是持续流式调度——并发槽位
空出即派发下一个请求，慢请求不会阻塞后续调度，逐条产出处理结果：

```python
import asyncio


async def consume():
    spider = QuotesSpider(fetcher=Fetcher(impersonate="chrome131"))
    async for item in spider.stream():
        print(item)  # 边爬边消费，不必等全部完成


asyncio.run(consume())
```

`stream()` 同样支持 `max_requests` / `state_file` / `resume` 参数。注意设置了
`per_domain_delay` 的域在流式模式下会被串行化（同域并发降为 1）。

## 下一步

- [隐身与网络指南](stealth.md) — Fetcher 的 TLS 指纹、会话池与代理轮换
- [API 参考 · Spider 框架](../api-reference.md#spider-framework) — 全部类的
  自动生成文档
