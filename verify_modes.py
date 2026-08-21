import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

for k in ("WEB_CRAWLER_POWER_MODE", "WEB_CRAWLER_ALLOW_PRIVATE_HOSTS"):
    os.environ.pop(k, None)

from web_crawler._ssrf import validate_url_host, is_power_mode
from web_crawler.fetchers._base import _default_allow_private_hosts, validate_url
from app.crawler_net import _is_safe_hostname
from web_crawler import Fetcher

P, F = "PASS", "FAIL"


def reject(u):
    try:
        validate_url_host(u)
        return False
    except ValueError:
        return True


print("================ PART 1: PUBLIC DEFAULT (SAFE) ================")
print("power mode default:", _default_allow_private_hosts(), "(expect False)")
blocked = [
    "http://169.254.169.254/latest/meta-data",
    "http://100.100.100.200/",
    "http://127.0.0.1:8000/",
    "http://127.1.2.3/",
    "http://0.0.0.0/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://localhost/",
    "http://foo.local/",
    "http://metadata.google.internal/",
]
fails = [u for u in blocked if not reject(u)]
print("blocked set:", len(blocked), "| all rejected:", P if not fails else F, fails[:2])

allowed = ["https://example.com/", "https://quotes.toscrape.com/", "http://8.8.8.8/"]
bad = [u for u in allowed if reject(u)]
print("public targets allowed:", P if not bad else F, bad)

try:
    validate_url("file:///etc/passwd")
    print("scheme whitelist: FAIL")
except ValueError:
    print("scheme whitelist (file:// rejected):", P)

print("-- real Fetcher behavior --")
f = Fetcher(timeout=5)
for u in ["http://127.0.0.1:8767/", "http://169.254.169.254/latest/meta-data"]:
    try:
        f.get(u)
        print("  FAIL: Fetcher fetched", u)
    except ValueError:
        print("  ", P, "Fetcher blocked", u)
r = f.get("https://quotes.toscrape.com/")
h1 = r.css_first("h1")
print("  public crawl quotes.toscrape.com: HTTP", r.status, len(r.content), "bytes | h1 =", h1.text if h1 else "?")

print()
print("================ PART 2: POWER MODE (FULL) ================")
os.environ["WEB_CRAWLER_POWER_MODE"] = "1"
print("is_power_mode:", is_power_mode(), "| fetcher default:", _default_allow_private_hosts(), "(expect True True)")
f2 = Fetcher(timeout=5)
r2 = f2.get("http://127.0.0.1:8767/")
print("  power fetch local server 127.0.0.1:8767: HTTP", r2.status, P)
try:
    validate_url_host("http://169.254.169.254/latest/meta-data")
    print("  power metadata host allowed:", P)
except ValueError:
    print("  power metadata host allowed: FAIL")
print("  app._is_safe_hostname(192.168.1.1):", _is_safe_hostname("192.168.1.1"), "(expect True)")
try:
    validate_url("file:///etc/passwd")
    print("  power scheme still enforced: FAIL")
except ValueError:
    print("  power scheme whitelist stays:", P)
r3 = f2.get("https://quotes.toscrape.com/")
print("  power public crawl still OK: HTTP", r3.status, P)
