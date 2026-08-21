import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
os.environ["WEB_CRAWLER_POWER_MODE"] = "1"

import tempfile
from web_crawler import Selector, AdaptiveStorage

db = os.path.join(tempfile.gettempdir(), "wc_adaptive_test2.sqlite3")
if os.path.exists(db):
    os.remove(db)
storage = AdaptiveStorage(db_path=db)

html_v1 = '<html><body><div id="product-title">旧版标题</div></body></html>'
html_v2 = '<html><body><div class="renamed-title">改版后标题</div></body></html>'

print("source literal repr:", repr(html_v1))
print("literal is proper unicode:", html_v1 == '<html><body><div id="product-title">旧版标题</div></body></html>')

# 不经过 adaptive 的直接解析
p = Selector(html_v1, url="https://shop.example.com/p")
t = p.css_first("#product-title")
print("plain parse text:", repr(t.text), "| equal:", t.text == "旧版标题")

# adaptive 流程
page1 = Selector(html_v1, url="https://shop.example.com/p", adaptive=True, storage=storage)
t1 = page1.css_first("#product-title", auto_save=True)
print("adaptive saved text:", repr(t1.text), "| equal:", t1.text == "旧版标题")

page2 = Selector(html_v2, url="https://shop.example.com/p", adaptive=True, storage=storage)
t2 = page2.css_first("#product-title", adaptive=True)
print("adaptive relocated text:", repr(t2.text), "| equal:", t2.text == "改版后标题")
print("storage entries:", storage.count() if hasattr(storage, "count") else "n/a")
