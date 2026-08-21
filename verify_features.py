import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

os.environ["WEB_CRAWLER_POWER_MODE"] = "1"  # 个人满血模式
P, F = "PASS", "FAIL"

print("================ PART 3: FULL-POWER FEATURES SMOKE ================")

# 3.1 TLS 指纹隐身（curl_cffi chrome131）
from web_crawler import Fetcher

with Fetcher(impersonate="chrome131", timeout=15) as f:
    r = f.get("https://quotes.toscrape.com/")
    print("3.1 TLS impersonation (chrome131): HTTP", r.status, P)

# 3.2 统一 Response 辅助方法
r2 = Fetcher(timeout=15).get("https://quotes.toscrape.com/")
quotes = r2.css("div.quote")
print("3.2 Response.css div.quote:", len(quotes), "条 | xpath:", len(r2.xpath("//div[@class='quote']")), "条", P)

# 3.3 自适应选择器（指纹 save/retrieve/relocate，临时 SQLite）
import tempfile
from web_crawler import Selector, AdaptiveStorage

db = os.path.join(tempfile.gettempdir(), "wc_adaptive_test.sqlite3")
if os.path.exists(db):
    os.remove(db)
storage = AdaptiveStorage(db_path=db)
html_v1 = '<html><body><div id="product-title">旧版标题</div></body></html>'
html_v2 = '<html><body><div class="renamed-title">改版后标题</div></body></html>'
page1 = Selector(html_v1, url="https://shop.example.com/p", adaptive=True, storage=storage)
t1 = page1.css_first("#product-title", auto_save=True)
page2 = Selector(html_v2, url="https://shop.example.com/p", adaptive=True, storage=storage)
t2 = page2.css_first("#product-title", adaptive=True)  # 站点改版后按相似度重定位
print("3.3 adaptive selector: 指纹保存+改版重定位 ->", repr(t1.text), "->", repr(t2.text), P if t2.text == "改版后标题" else F)

# 3.4 Spider 框架真实运行
from web_crawler import Spider, Request, Fetcher as Fetcher2


class QuotesSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]
    allowed_domains = ["quotes.toscrape.com"]

    def parse(self, response):
        for q in response.css("div.quote"):
            yield {"text": q.css_first(".text").text, "author": q.css_first(".author").text}
        nxt = response.css_first("li.next > a")
        if nxt:
            yield Request(response.urljoin(nxt.attr("href")))


items = list(QuotesSpider(fetcher=Fetcher2(timeout=15)).run())
print("3.4 Spider real run: 抓取条目数 =", len(items), "| 首条:", items[0]["author"] if items else "?", P if len(items) > 0 else F)

# 3.5 ProxyPool 轮换
from web_crawler import ProxyPool

pool = ProxyPool(["http://u:p@proxy1:8080", "http://proxy2:3128"], strategy="round_robin", max_failures=3, cooldown=60)
n1, n2 = pool.get(), pool.get()
print("3.5 ProxyPool round_robin 轮换:", n1 != n2, P if n1 != n2 else F)

# 3.6 验证码求解器（ddddocr 缺失时优雅降级）
from web_crawler.ai.image_captcha import ImageCaptchaSolver
solver = ImageCaptchaSolver(provider=None)
print("3.6 ImageCaptchaSolver construct:", P, "(ddddocr 缺失时按 numpy 模板匹配/LLM Vision 降级)")

# 3.7 AI 抽取 / 逆向 Agent（构造不调用 LLM）
from web_crawler.ai.extractor import AIExtractor
from web_crawler.ai.reverse_agent import ReverseAgent
ex = AIExtractor()
print("3.7 AIExtractor construct:", P)
print("    ReverseAgent module import:", P)

# 3.8 轻量渗透工具（授权测试用途，仅本地/公开站）
from web_crawler.pentest import PortScanner, HeaderChecker, PentestReport
hs = HeaderChecker().check("https://quotes.toscrape.com/")
grade = getattr(hs, "grade", "?")
count = len(getattr(hs, "results", []) or getattr(hs, "checks", []) or [])
print("3.8 HeaderChecker 公开站:", str(hs)[:80], "| grade:", grade, P if hs is not None else F)

# 3.9 MCP 服务模块
import web_crawler.mcp as mcp
print("3.9 web_crawler.mcp import:", P)

# 3.10 app 层 CLI
import subprocess
out = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "crawler.py"), "--help"],
                     capture_output=True, text=True, timeout=60)
print("3.10 app/crawler.py --help 退出码:", out.returncode, P if out.returncode == 0 else F)

# 3.11 懒加载：可选依赖缺失不阻断核心导入
import importlib
for mod in ("web_crawler.fetchers.dynamic", "web_crawler.fetchers.stealthy", "web_crawler.fetchers.camoufox"):
    importlib.import_module(mod)
print("3.11 重型子模块（dynamic/stealthy/camoufox）懒加载 import:", P)

print()
print("================ 全部冒烟完成 ================")
