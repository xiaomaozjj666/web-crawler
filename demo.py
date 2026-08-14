"""web_crawler 使用示范 —— 由浅入深 5 个场景。

直接运行：
    双击 demo.bat，或命令行：py -3.14 demo.py

每个场景独立，可按需复制使用。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 自动把项目 src/ 加入模块搜索路径，双击 .py 或 .bat 都能直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from web_crawler import (
    Fetcher,
    ProxyPool,
    Request,
    Selector,
    Spider,
)


# ---------------------------------------------------------------------------
# 场景 1：解析本地 HTML（最基础的 Selector 用法，无需网络）
# ---------------------------------------------------------------------------
def demo_1_parse_local_html() -> None:
    print("\n=== 场景 1：解析本地 HTML ===")

    html = """
    <html><body>
      <ul class="products">
        <li class="product" data-id="1">
          <h3 class="name">Widget</h3>
          <span class="price">9.99</span>
        </li>
        <li class="product" data-id="2">
          <h3 class="name">Gadget</h3>
          <span class="price">19.99</span>
        </li>
      </ul>
    </body></html>
    """
    page = Selector(html, url="https://shop.example.com")

    # CSS 选择器
    for item in page.css(".product"):
        name = item.css_first(".name").text
        price = item.css_first(".price").text
        data_id = item.attr("data-id")
        print(f"  [{data_id}] {name} — ¥{price}")

    # XPath（返回元素节点，再取 .text）
    first_name_el = page.xpath_first('//h3[@class="name"]')
    print(f"  第一个名字 (XPath)：{first_name_el.text if first_name_el else None}")

    # 批量读取属性：Scrapling 风格 ::attr(name) 伪元素
    all_ids = page.css(".product::attr(data-id)")
    print(f"  所有 ID：{all_ids}")


# ---------------------------------------------------------------------------
# 场景 2：隐身 HTTP 抓取（curl_cffi TLS 指纹）
# ---------------------------------------------------------------------------
def demo_2_stealth_fetch() -> None:
    print("\n=== 场景 2：隐身 HTTP 抓取 ===")

    # impersonate 模拟 Chrome 131 的 TLS/JA3 指纹
    with Fetcher(impersonate="chrome131", timeout=15.0) as fetcher:
        resp = fetcher.get("https://httpbin.org/get")
        print(f"  状态码：{resp.status}")
        print(f"  是否 OK：{resp.ok}")

        # Response 自带 Selector，可直接 css/xpath
        # httpbin 返回 JSON，这里只演示取 header
        data = resp.json()
        print(f"  服务器看到的 UA：{data['headers'].get('User-Agent', '?')[:60]}...")
        print(f"  来源 IP：{data.get('origin')}")


# ---------------------------------------------------------------------------
# 场景 3：异步并发抓取多个页面
# ---------------------------------------------------------------------------
async def demo_3_async_concurrent() -> None:
    print("\n=== 场景 3：异步并发抓取 ===")

    urls = [
        "https://httpbin.org/delay/1",
        "https://httpbin.org/delay/1",
        "https://httpbin.org/delay/1",
    ]

    async with Fetcher(impersonate="chrome131", timeout=30.0) as fetcher:
        # 并发发起 3 个请求（串行需 3 秒，并发约 1 秒）
        tasks = [fetcher.async_get(url) for url in urls]
        responses = await asyncio.gather(*tasks)

        for i, resp in enumerate(responses, 1):
            print(f"  [{i}] {resp.url} → {resp.status}")


# ---------------------------------------------------------------------------
# 场景 4：代理池轮换
# ---------------------------------------------------------------------------
def demo_4_proxy_pool() -> None:
    print("\n=== 场景 4：代理池轮换 ===")

    # 填入你自己的代理，此处仅演示 API
    pool = ProxyPool(
        ["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"],
        strategy="round_robin",
        max_failures=3,
        cooldown=60.0,
    )
    print(f"  {pool!r}")

    # 模拟取 5 次（即使代理不可用也能看到轮换逻辑）
    for i in range(5):
        proxy = pool.get()
        print(f"  第 {i + 1} 次取到的代理：{proxy}")

    # 模拟某个代理失败到阈值后进入冷却
    pool.mark_failed("http://proxy1:8080")
    pool.mark_failed("http://proxy1:8080")
    pool.mark_failed("http://proxy1:8080")
    print(f"  标记 proxy1 失败 3 次后：{pool!r}")
    print(f"  可用代理数：{pool.available_count()}")


# ---------------------------------------------------------------------------
# 场景 5：Spider 框架 —— 回调式爬虫
# ---------------------------------------------------------------------------
class QuotesSpider(Spider):
    """抓取 quotes.toscrape.com 的示范爬虫。

    这是专门给学习者用的沙盒站点，适合演示。

    注意：爬虫引擎只认 ``run(max_requests=N)`` 的 kwarg（见 demo_5_spider），
    类属性 ``max_requests`` 不会生效，所以这里不再声明它。
    """

    start_urls = ["https://quotes.toscrape.com/"]
    allowed_domains = ["quotes.toscrape.com"]

    def parse(self, response):
        """首页及翻页回调。"""
        for quote in response.css(".quote"):
            text = quote.css_first(".text").text
            author = quote.css_first(".author").text
            tags = [t.text for t in quote.css(".tag")]
            yield {
                "text": text,
                "author": author,
                "tags": tags,
            }

        # 跟踪「下一页」链接（用 ::attr 伪元素直接取 href）
        next_href = response.css_first(".next > a::attr(href)")
        if next_href:
            yield Request(response.urljoin(next_href))


def demo_5_spider() -> None:
    print("\n=== 场景 5：Spider 框架 ===")
    with Fetcher(impersonate="chrome131", timeout=15.0) as fetcher:
        spider = QuotesSpider(fetcher=fetcher)
        # 限制只抓 3 页，避免示范跑太久（max_requests 是 run() 的 kwarg）
        results = spider.run(max_requests=3)

    print(f"  抓取到 {len(results)} 条引言，前 3 条：")
    for item in results[:3]:
        print(f"    - {item['author']}: {item['text'][:40]}...")
    print(f"  统计：{spider.stats}")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main() -> None:
    print("web_crawler 使用示范")
    print("=" * 50)

    # 场景 1：纯本地，必跑
    demo_1_parse_local_html()

    # 场景 2~5 需要网络；若离线可注释掉
    try:
        demo_2_stealth_fetch()
    except Exception as e:
        print(f"  [场景 2 跳过] {e}")

    try:
        asyncio.run(demo_3_async_concurrent())
    except Exception as e:
        print(f"  [场景 3 跳过] {e}")

    demo_4_proxy_pool()

    try:
        demo_5_spider()
    except Exception as e:
        print(f"  [场景 5 跳过] {e}")

    print("\n" + "=" * 50)
    print("示范结束。")


if __name__ == "__main__":
    main()
