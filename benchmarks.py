"""Micro-benchmarks for the web_crawler parser and fetcher stack.

Run directly:

    python benchmarks.py

These are not full statistical benchmarks — they measure wall-clock time of
common operations against a realistic-size HTML document so regressions in
parser speed or adaptive lookup are visible at a glance.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure src/ is importable when running from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from web_crawler import Selector, compute_fingerprint, similarity_score

# A ~50 KB synthetic product-listing page — enough elements to stress the
# parser without dominating the runtime in I/O.
SAMPLE_HTML = """
<html><head><title>Bench</title></head><body>
  <div id="catalog">
    {}
  </div>
</body></html>
""".format(
    "\n".join(
        f'<div class="product" data-id="{i}">'
        f'<h3 class="name">Product {i}</h3>'
        f'<span class="price">{i}.99</span>'
        f'<p class="desc">Description for product {i}</p>'
        f"</div>"
        for i in range(500)
    )
)


def _bench(label: str, func, iterations: int = 100) -> None:
    func()  # warm up
    start = time.perf_counter()
    for _ in range(iterations):
        func()
    elapsed = time.perf_counter() - start
    per_op = elapsed / iterations * 1000
    print(f"  {label:40s} {per_op:8.3f} ms/op  ({iterations} iters)")


def bench_parse() -> None:
    print("Parsing")
    _bench("Selector(str) — 500 elements", lambda: Selector(SAMPLE_HTML))


def bench_css() -> None:
    page = Selector(SAMPLE_HTML)
    print("CSS selection")
    _bench("page.css('.product')", lambda: page.css(".product"))
    _bench("page.css_first('.product .name')", lambda: page.css_first(".product .name"))
    _bench("page.css('[data-id=\"250\"]')", lambda: page.css('[data-id="250"]'))


def bench_xpath() -> None:
    page = Selector(SAMPLE_HTML)
    print("XPath selection")
    _bench("page.xpath('//div[@class=\"product\"]')", lambda: page.xpath('//div[@class="product"]'))


def bench_text() -> None:
    page = Selector(SAMPLE_HTML)
    el = page.css_first(".product")
    print("Text extraction")
    _bench("el.text", lambda: str(el.text))
    _bench("el.get_all_text()", lambda: str(el.get_all_text()))


def bench_fingerprint() -> None:
    page = Selector(SAMPLE_HTML)
    el = page.css_first(".product").element
    fp = compute_fingerprint(el)
    print("Adaptive fingerprinting")
    _bench("compute_fingerprint(el)", lambda: compute_fingerprint(el))
    _bench("similarity_score(fp, fp)", lambda: similarity_score(fp, fp), iterations=1000)


def bench_adaptive_relocate() -> None:
    """End-to-end: save a fingerprint, then relocate after markup change."""
    import tempfile

    from web_crawler.parser.adaptive import AdaptiveStorage

    with tempfile.TemporaryDirectory() as tmp:
        storage = AdaptiveStorage(Path(tmp) / "bench.sqlite3")
        v1 = '<div><a id="p1" class="product">Widget</a></div>'
        v2 = '<div><span class="wrap"><a data-id="p1" class="product new">Widget</a></span></div>'
        page1 = Selector(v1, url="https://bench.example", adaptive=True, storage=storage)
        page1.css_first("#p1", auto_save=True)
        page2 = Selector(v2, url="https://bench.example", adaptive=True, storage=storage)

        print("Adaptive relocate")
        _bench("css_first(adaptive=True)", lambda: page2.css_first("#p1", adaptive=True))
        storage.close()


def main() -> None:
    print(f"web_crawler benchmarks — {len(SAMPLE_HTML)} bytes sample HTML\n")
    bench_parse()
    bench_css()
    bench_xpath()
    bench_text()
    bench_fingerprint()
    bench_adaptive_relocate()
    print("\nDone.")


if __name__ == "__main__":
    main()
