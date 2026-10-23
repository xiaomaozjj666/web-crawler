"""web_crawler 与同类库的同进程性能对比（可复现对标基准）。

对标当前覆盖 Scrapling（`pip install scrapling`，本地解析路径）。
对比条件强调公平性：

- 同一 Python 进程、同一份 77KB / 500 元素 HTML 样本（``benchmarks.SAMPLE_HTML``）
- 双方各用惯用最短路径 API（web_crawler ``css_first`` vs Scrapling ``css(...).first``）
- ``timeit`` 15 轮 × 每轮 50 次取中位数，摊薄计时噪声

运行方式（需独立 venv 同时安装两库，避免依赖互扰）::

    python -m venv /tmp/bench-vs
    /tmp/bench-vs/bin/python -m pip install scrapling -e .
    /tmp/bench-vs/bin/python benchmarks_vs_peers.py

结果速览（2026-10-23，Windows 11 / CPython 3.12 / Scrapling 0.5）：

====================  ============  ============  ======
操作                  web_crawler   Scrapling     比值
====================  ============  ============  ======
构造解析 HTML→对象     1.60 ms       2.07 ms       1.3x 快
css 首个匹配           0.44 ms       0.43 ms       持平
css 多元素+文本提取    0.44 ms       0.43 ms       持平
xpath 多元素           0.18 ms       0.18 ms       持平
====================  ============  ============  ======

解读：查询热路径两者持平（同为 lxml 底层）；构造解析 web_crawler 更快
——Scrapling 在构造期做特征预提取以支撑其自适应重定位，属于能力换时间
（web_crawler 的重定位按需触发，构造零预提取）。
"""

from __future__ import annotations

import statistics
import timeit

REPEAT = 15
NUMBER = 50


def _median_ms(fn: object) -> float:
    """timeit 多轮取中位数，归一为 ms/op。"""
    times = timeit.repeat(fn, number=NUMBER, repeat=REPEAT)
    return statistics.median(times) / NUMBER * 1000


def main() -> None:
    """跑对比并输出表格（Scrapling 未安装时给出安装指引后退出）。"""
    try:
        from scrapling.parser import Selector as ScraplingSelector
    except ImportError as exc:
        raise SystemExit(
            "需要 scrapling 才能跑对标基准：pip install scrapling "
            "（建议独立 venv 同时安装两库，见模块 docstring）"
        ) from exc

    from benchmarks import SAMPLE_HTML
    from web_crawler import Selector as WcSelector

    sp = ScraplingSelector(content=SAMPLE_HTML)
    wc = WcSelector(SAMPLE_HTML)

    cases: list[tuple[str, object, object]] = [
        (
            "构造解析 (HTML→对象)",
            lambda: ScraplingSelector(content=SAMPLE_HTML),
            lambda: WcSelector(SAMPLE_HTML),
        ),
        ("css 首个匹配", lambda: sp.css("div.post").first, lambda: wc.css_first("div.post")),
        (
            "css 多元素 + 文本提取",
            lambda: [e.text for e in sp.css("div.post")],
            lambda: [e.text for e in wc.css("div.post")],
        ),
        (
            "xpath 多元素",
            lambda: [e.text for e in sp.xpath("//div[@class='post']")],
            lambda: [e.text for e in wc.xpath("//div[@class='post']")],
        ),
    ]

    print(f"{'操作':<24}{'web_crawler':>14}{'Scrapling':>14}{'比值':>10}")
    print("-" * 62)
    for label, sp_fn, wc_fn in cases:
        sp_ms = _median_ms(sp_fn)
        wc_ms = _median_ms(wc_fn)
        ratio = sp_ms / wc_ms
        verdict = (
            f"{ratio:.1f}x 快"
            if ratio >= 1.05
            else ("持平" if ratio > 0.95 else f"{1 / ratio:.1f}x 慢")
        )
        print(f"{label:<26}{wc_ms:>11.3f}ms{sp_ms:>11.3f}ms{verdict:>9}")


if __name__ == "__main__":
    main()
