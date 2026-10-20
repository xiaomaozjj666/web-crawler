"""Micro-benchmarks for the web_crawler parser and fetcher stack.

Run directly:

    python benchmarks.py                     # 打印各基准的 ms/op
    python benchmarks.py --save-baseline out.json   # 保存当前结果为基线
    python benchmarks.py --baseline out.json        # 对比基线，超 20% 退化即 exit 1
    python benchmarks.py --check-regression        # CI 模式，使用内置基线对比
    python benchmarks.py --check-regression --json-out result.json
                                                   # 附加机器可读报告（CI 存档用）

These are not full statistical benchmarks — they measure wall-clock time of
common operations against a realistic-size HTML document so regressions in
parser speed or adaptive lookup are visible at a glance.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
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
# 注：xpath/fingerprint/similarity 三项基线按 GitHub Actions runner
# （共享 CPU、Python 3.12 + 发行版 lxml）实测值校准——本地 Python 3.14
# 下这三项明显更快，基线取 CI 环境典型值并保留容差。
# 2026-10 解析层热路径优化轮更新（多轮实测中位 + 保守余量）：
# - css/css_first/xpath 三项受益于 CSS→XPath 翻译缓存（lru_cache）、
#   _wrap 绕过 __init__（免逐元素重复 urlparse）、css_first/xpath_first
#   短路（只包装首个匹配），按隔离对比实测提升幅度下调；
# - 其余项（解析/文本/指纹/自适应）路径未变，维持原值。
# 2026-10 网络层热路径优化轮新增四条网络条目（本地实测 + CI 保守余量）：
# - _throttle_delay_for 受益于 _throttle_host 的 lru_cache 与 stream()
#   的 precomputed 单次计算（本地实测 2.3 us/op，优化前 ~3.8 us/op
#   且每请求计算两次）；
# - _merge_headers (httpx) 受益于 _decodable_encodings 进程内探测缓存
#   （优化前每请求重复 try-import brotli ≈ 3.5 ms/op，缓存后亚微秒）；
# - _retry_errors 受益于实例级缓存（优化前 ~1.9 us/op）。
BASELINE: dict[str, float] = {
    "Selector(str) — 500 elements": 2.5,
    "page.css('.product')": 1.85,
    "page.css_first('.product .name')": 4.2,
    "page.css('[data-id=\"250\"]')": 0.6,
    "page.xpath('//div[@class=\"product\"]')": 0.7,
    "el.text": 0.01,
    "el.get_all_text()": 0.01,
    "compute_fingerprint(el)": 0.3,
    "similarity_score(fp, fp)": 0.34,
    "css_first(adaptive=True)": 0.4,
    "spider._throttle_delay_for(url)": 0.01,
    "Fetcher._merge_headers (curl)": 0.001,
    "Fetcher._merge_headers (httpx)": 0.002,
    "Fetcher._retry_errors()": 0.001,
}

# 回归阈值：当前 ms/op 超过基线的此倍数即视为退化
REGRESSION_THRESHOLD = 1.2


def _bench(label: str, func, iterations: int = 100) -> float:
    """运行单次基准并返回 ms/op；同时打印到 stdout。

    亚毫秒级（< 0.1 ms/op）的微基准以 us/op 显示，避免格式化成 0.000
    无法分辨；返回值恒为 ms/op，与 BASELINE/JSON 报告的单位一致。
    """
    func()  # warm up
    start = time.perf_counter()
    for _ in range(iterations):
        func()
    elapsed = time.perf_counter() - start
    per_op = elapsed / iterations * 1000
    if per_op < 0.1:
        print(f"  {label:40s} {per_op * 1000:8.2f} us/op  ({iterations} iters)")
    else:
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


def bench_net_hot_paths() -> dict[str, float]:
    """网络层热路径：三源延迟调度 + fetcher 请求头合并 + 重试错误元组。

    全部零网络（robots 解析器直接注入缓存、不触发任何请求），只测
    CPU 热路径——对应 2026-10 网络层优化轮的受益点：``_throttle_host``
    的 lru_cache（重复 URL 免二次 urlparse）、``_decodable_encodings``
    进程内探测缓存（httpx 兜底路径每请求省 ~1.7ms 的重复 try-import）、
    ``_retry_errors`` 实例缓存（免每请求重建异常元组）。

    ``_merge_headers`` 的分支条件 ``self._use_curl`` 在构造时从模块级
    ``HAS_CURL_CFFI`` 快照——切换模块变量不影响已构造实例。两条基准
    条目各自"先切换、后构造"专属实例（参照 tests/test_net_perf.py 的
    patch 顺序），保证标签与实测路径一致：curl 条目强制走 curl 分支
    （``dict(extra_headers)`` 纯字典操作，未装 curl_cffi 的 CI 最小依赖
    档也能正确测量）；httpx 条目强制走兜底分支（抑制已装 curl_cffi 环境
    下的降级 RuntimeWarning，保持基准输出干净）。测量完成后恢复模块
    变量并关闭全部实例，基准自身不泄漏资源。
    """
    import urllib.robotparser
    import warnings

    from web_crawler.fetchers import fetcher as fetcher_mod
    from web_crawler.fetchers.fetcher import Fetcher
    from web_crawler.spider import AutoThrottle, Spider
    from web_crawler.spider.spider import _throttle_host

    url = "https://bench.example.com/products/list?page=3"
    at = AutoThrottle(alpha=1.0, target_concurrency=1.0)
    at.note_response("bench.example.com", 0.05, True)
    spider = Spider(fetcher=None, autothrottle=at)
    spider.respect_robots = True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Crawl-delay: 2"])
    spider._robots_policy._cache[f"https://{_throttle_host(url)}"] = parser

    print("Network hot paths")
    results: dict[str, float] = {}
    f_retry: Fetcher | None = None
    saved = fetcher_mod.HAS_CURL_CFFI
    try:
        results["spider._throttle_delay_for(url)"] = _bench(
            "spider._throttle_delay_for(url)",
            lambda: spider._throttle_delay_for(url),
            iterations=20000,
        )
        # curl 分支：先强制 HAS_CURL_CFFI=True 再构造（_use_curl 构造时快照）。
        # _merge_headers 的 curl 分支是纯字典操作，不加载后端——未装
        # curl_cffi 的环境也能正确测量该分支
        fetcher_mod.HAS_CURL_CFFI = True
        f_curl = Fetcher()
        try:
            results["Fetcher._merge_headers (curl)"] = _bench(
                "Fetcher._merge_headers (curl)",
                lambda: f_curl._merge_headers({"X-Bench": "1"}),
                iterations=20000,
            )
        finally:
            f_curl.close()
        # httpx 兜底分支：切换后重新构造实例（先 patch 后构造，保证
        # _use_curl=False 真实生效）
        fetcher_mod.HAS_CURL_CFFI = False
        with warnings.catch_warnings():
            # 已装 curl_cffi 的环境强制走兜底分支会发降级告警；抑制以
            # 保持基准输出干净（pytest 严格模式亦不受影响）
            warnings.simplefilter("ignore", RuntimeWarning)
            f_httpx = Fetcher()
        try:
            results["Fetcher._merge_headers (httpx)"] = _bench(
                "Fetcher._merge_headers (httpx)",
                lambda: f_httpx._merge_headers({"X-Bench": "1"}),
                iterations=20000,
            )
        finally:
            f_httpx.close()
        # 重试条目：恢复模块变量后按真实后端环境构造（_retry_errors 读
        # 实例属性 _use_curl——同样是构造时快照，不受上面切换影响）
        fetcher_mod.HAS_CURL_CFFI = saved
        f_retry = Fetcher()
        results["Fetcher._retry_errors()"] = _bench(
            "Fetcher._retry_errors()",
            lambda: f_retry._retry_errors(),
            iterations=20000,
        )
    finally:
        fetcher_mod.HAS_CURL_CFFI = saved
        if f_retry is not None:
            f_retry.close()
    return results


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
    results.update(bench_net_hot_paths())
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


def write_json_report(
    current: dict[str, float],
    baseline: dict[str, float],
    path: str,
    threshold: float = REGRESSION_THRESHOLD,
) -> None:
    """把当前结果与基线写成机器可读 JSON 报告（CI artifact / 趋势分析用）。

    与 ``--save-baseline`` 的平铺 {label: ms} 不同，这里输出结构化报告：
    - ``seconds`` / ``baseline`` 均以秒/op 计（同单位，可直接相除）；
    - ``ratio`` 为当前值/基线值（无量纲，>1 即慢于基线）；
    - ``regression`` 判定与 ``check_regression`` 完全一致（超基线
      ``threshold`` 倍即 True），与命令行 exit code 同源。
    基线中缺失的基准项 ``baseline``/``ratio`` 为 null、``regression`` 为 False。
    """
    entries: list[dict[str, object]] = []
    for name, cur_ms in current.items():
        base_ms = baseline.get(name)
        if base_ms is None:
            entries.append(
                {
                    "name": name,
                    "seconds": cur_ms / 1000.0,
                    "baseline": None,
                    "ratio": None,
                    "regression": False,
                }
            )
        else:
            entries.append(
                {
                    "name": name,
                    "seconds": cur_ms / 1000.0,
                    "baseline": base_ms / 1000.0,
                    "ratio": round(cur_ms / base_ms, 4),
                    "regression": cur_ms > base_ms * threshold,
                }
            )
    report = {
        "benchmarks": entries,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
    }
    Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"JSON 报告已写入 {path}")


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
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        help="把结果写为机器可读 JSON 报告（含基线对比与判定），可与任意模式组合",
    )
    args = parser.parse_args()

    results = run_all_benchmarks()

    # --json-out 可与任意模式组合：报告中的基线取「生效中的对比基线」
    # （显式 --baseline 文件优先，否则内置 BASELINE），判定与 check_regression
    # 同源，CI 存档后可离线复核。不传 --json-out 时本块无副作用，
    # 其余输出与历史行为完全一致。
    active_baseline = load_baseline(args.baseline) if args.baseline else BASELINE
    if args.json_out:
        write_json_report(results, active_baseline, args.json_out)

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
