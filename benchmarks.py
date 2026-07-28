"""Micro-benchmarks for the web_crawler parser and fetcher stack.

Run directly:

    python benchmarks.py                     # 打印各基准的 ms/op
    python benchmarks.py --save-baseline out.json   # 保存当前结果为基线
    python benchmarks.py --baseline out.json        # 对比基线，超 20% 退化即 exit 1
    python benchmarks.py --check-regression        # CI 模式，使用内置基线对比

These are not full statistical benchmarks — they measure wall-clock time of
common operations against a realistic-size HTML document so regressions in
parser speed or adaptive lookup are visible at a glance.
"""

from __future__ import annotations

import argparse
import json
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

# 内置基线（ms/op）：基于当前测试环境的典型值，CI 回归检测时使用。
# 基线值留有 20% 的容差区间，仅拦截显著退化。
BASELINE: dict[str, float] = {
    "Selector(str) — 500 elements": 2.5,
    "page.css('.product')": 2.0,
    "page.css_first('.product .name')": 4.5,
    "page.css('[data-id=\"250\"]')": 0.6,
    "page.xpath('//div[@class=\"product\"]')": 0.5,
    "el.text": 0.01,
    "el.get_all_text()": 0.01,
    "compute_fingerprint(el)": 0.2,
    "similarity_score(fp, fp)": 0.2,
    "css_first(adaptive=True)": 0.4,
}

# 回归阈值：当前 ms/op 超过基线的此倍数即视为退化
REGRESSION_THRESHOLD = 1.2


def _bench(label: str, func, iterations: int = 100) -> float:
    """运行单次基准并返回 ms/op；同时打印到 stdout。"""
    func()  # warm up
    start = time.perf_counter()
    for _ in range(iterations):
        func()
    elapsed = time.perf_counter() - start
    per_op = elapsed / iterations * 1000
    print(f"  {label:40s} {per_op:8.3f} ms/op  ({iterations} iters)")
    return per_op


def bench_parse() -> dict[str, float]:
    print("Parsing")
    return {
        "Selector(str) — 500 elements": _bench(
            "Selector(str) — 500 elements", lambda: Selector(SAMPLE_HTML)
        )
    }


def bench_css() -> dict[str, float]:
    page = Selector(SAMPLE_HTML)
    print("CSS selection")
    results: dict[str, float] = {}
    results["page.css('.product')"] = _bench("page.css('.product')", lambda: page.css(".product"))
    results["page.css_first('.product .name')"] = _bench(
        "page.css_first('.product .name')", lambda: page.css_first(".product .name")
    )
    results["page.css('[data-id=\"250\"]')"] = _bench(
        "page.css('[data-id=\"250\"]')", lambda: page.css('[data-id="250"]')
    )
    return results


def bench_xpath() -> dict[str, float]:
    page = Selector(SAMPLE_HTML)
    print("XPath selection")
    return {
        "page.xpath('//div[@class=\"product\"]')": _bench(
            "page.xpath('//div[@class=\"product\"]')",
            lambda: page.xpath('//div[@class="product"]'),
        )
    }


def bench_text() -> dict[str, float]:
    page = Selector(SAMPLE_HTML)
    el = page.css_first(".product")
    print("Text extraction")
    results: dict[str, float] = {}
    results["el.text"] = _bench("el.text", lambda: str(el.text))
    results["el.get_all_text()"] = _bench("el.get_all_text()", lambda: str(el.get_all_text()))
    return results


def bench_fingerprint() -> dict[str, float]:
    page = Selector(SAMPLE_HTML)
    el = page.css_first(".product").element
    fp = compute_fingerprint(el)
    print("Adaptive fingerprinting")
    results: dict[str, float] = {}
    results["compute_fingerprint(el)"] = _bench(
        "compute_fingerprint(el)", lambda: compute_fingerprint(el)
    )
    results["similarity_score(fp, fp)"] = _bench(
        "similarity_score(fp, fp)", lambda: similarity_score(fp, fp), iterations=1000
    )
    return results


def bench_adaptive_relocate() -> dict[str, float]:
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
        result = {
            "css_first(adaptive=True)": _bench(
                "css_first(adaptive=True)", lambda: page2.css_first("#p1", adaptive=True)
            )
        }
        storage.close()
        return result


def run_all_benchmarks() -> dict[str, float]:
    """运行全部基准，返回 {label: ms/op} 字典。"""
    print(f"web_crawler benchmarks — {len(SAMPLE_HTML)} bytes sample HTML\n")
    results: dict[str, float] = {}
    results.update(bench_parse())
    results.update(bench_css())
    results.update(bench_xpath())
    results.update(bench_text())
    results.update(bench_fingerprint())
    results.update(bench_adaptive_relocate())
    print("\nDone.")
    return results


def check_regression(
    current: dict[str, float],
    baseline: dict[str, float],
    threshold: float = REGRESSION_THRESHOLD,
) -> list[str]:
    """对比当前结果与基线，返回退化项列表（空列表表示全部通过）。"""
    regressions: list[str] = []
    for label, base_ms in baseline.items():
        cur_ms = current.get(label)
        if cur_ms is None:
            continue
        if cur_ms > base_ms * threshold:
            regressions.append(
                f"  {label}: {cur_ms:.3f} ms/op > 基线 {base_ms:.3f} × {threshold:.1f} "
                f"= {base_ms * threshold:.3f} ms/op"
            )
    return regressions


def save_baseline(results: dict[str, float], path: str) -> None:
    """把当前结果保存为基线 JSON 文件。"""
    Path(path).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"基线已保存到 {path}")


def load_baseline(path: str) -> dict[str, float]:
    """从 JSON 文件加载基线。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: float(v) for k, v in data.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="web_crawler 微基准测试")
    parser.add_argument(
        "--save-baseline",
        metavar="PATH",
        help="把当前结果保存为基线 JSON 文件",
    )
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        help="从 JSON 文件加载基线并对比，超过 20%% 退化即 exit 1",
    )
    parser.add_argument(
        "--check-regression",
        action="store_true",
        help="CI 模式：使用内置基线对比，超过 20%% 退化即 exit 1",
    )
    args = parser.parse_args()

    results = run_all_benchmarks()

    if args.save_baseline:
        save_baseline(results, args.save_baseline)
        return

    if args.baseline:
        baseline = load_baseline(args.baseline)
        regressions = check_regression(results, baseline)
        if regressions:
            print("\n性能回退检测（对比基线文件）：")
            for r in regressions:
                print(r)
            sys.exit(1)
        print("\n全部基准通过基线对比。")
        return

    if args.check_regression:
        regressions = check_regression(results, BASELINE)
        if regressions:
            print("\n性能回退检测（内置基线）：")
            for r in regressions:
                print(r)
            sys.exit(1)
        print("\n全部基准通过内置基线对比。")


if __name__ == "__main__":
    main()
