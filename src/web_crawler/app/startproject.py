"""``startproject`` 脚手架生成器（对标 ``scrapy startproject``）。

生成一个扁平、诚实、贴合本库「构造参数风格」的爬虫项目骨架：没有
Scrapy 那种 settings.py 全局配置，组装全部显式发生在 ``main.py`` 里
（``Fetcher`` 构造参数 + ``Spider`` 构造参数 + ``feed_output=``）。

用法::

    web-crawler-startproject myproject
    python -m web_crawler.app.startproject myproject

生成结构::

    myproject/
    ├── spiders/
    │   └── example_spider.py   # 基础 Spider 与 CrawlSpider 两种玩法示例
    ├── main.py                 # 组装 Fetcher + Spider + feed_output，直接运行
    ├── requirements.txt        # stealth-web-crawler[all] 一行
    └── README.md               # 3 步上手说明

安全边界：目标目录已存在且非空时拒绝覆盖（抛
:class:`~web_crawler.exceptions.ConfigError`）；已存在但为空则放行。
所有文件以 UTF-8 + LF 写入（Windows 下也保持 LF，避免混合行尾）。
模板渲染用标准库 :class:`string.Template`，不引入 Jinja2。
"""

from __future__ import annotations

import argparse
import keyword
import sys
from pathlib import Path
from string import Template

from web_crawler.exceptions import ConfigError

# ── 模板（纯字符串常量；教学站点用 example.com + iana.org 组合保稳）──────────

_SPIDER_TEMPLATE = '''\
"""示例 spider：抓取 example.com / iana.org 的页面标题。

两种玩法都在这个文件里：

1. ``TitleSpider`` —— 基础 Spider：声明 ``start_urls`` 与 ``parse`` 回调，
   逐条 ``yield`` dict item（最常用的入门形态）；
2. ``SiteCrawlSpider`` —— ``CrawlSpider`` + ``Rule`` + ``LinkExtractor``：
   声明式跟随链接，规则驱动地遍历整站。

上手后把 ``start_urls`` / ``rules`` / 选择器改成你自己的目标站点，
然后回项目根目录执行 ``python main.py``。
"""

from __future__ import annotations

from collections.abc import Iterator

from web_crawler import CrawlSpider, LinkExtractor, Response, Rule, Spider


class TitleSpider(Spider):
    """基础玩法：start_urls + parse 回调。

    引擎依次请求 ``start_urls``，每个响应调用一次 ``parse``；
    ``yield`` dict 视为抓取到的 item（经 ``feed_output`` 落盘），
    ``yield`` Request 则被调度为新请求（见下方进阶注释）。
    """

    name = "titles"
    start_urls = [
        "https://example.com/",
        "https://www.iana.org/",
    ]

    def parse(self, response: Response) -> Iterator[dict[str, str]]:
        """产出一条 {"url", "title", "heading"} item。"""
        title = response.css_first("title")
        # 优雅降级：部分页面没有 h1（如 example.com），回退取 title 文本
        heading = response.css_first("h1") or title
        yield {
            "url": response.url,
            "title": str(title.text).strip() if title is not None else "",
            "heading": str(heading.text).strip() if heading is not None else "",
        }
        # 进阶：跟着页面链接继续抓（先在顶部 from web_crawler import Request，
        # 再取消下面两行注释，即可体验翻页/整站遍历）：
        # for href in response.css("a::attr(href)"):
        #     yield Request(response.urljoin(str(href)))


class SiteCrawlSpider(CrawlSpider):
    """进阶玩法：CrawlSpider + Rule + LinkExtractor 声明式跟随。

    规则语义：命中 ``allow`` 正则且属于 ``allow_domains`` 的链接会被自动
    请求并递归提取（去重 / 域过滤由引擎统一处理）；命中页面的响应交给
    ``callback`` 指向的方法产出 item。
    """

    name = "site_crawl"
    start_urls = ["https://www.iana.org/"]
    allowed_domains = ["iana.org"]
    rules = [
        Rule(
            # 只跟随 /domains /protocols /numbers /about 板块页
            LinkExtractor(allow=(r"/(domains|protocols|numbers|about)",)),
            callback="parse_page",
        ),
    ]

    def parse_page(self, response: Response) -> Iterator[dict[str, str]]:
        """CrawlSpider 规则回调：每个命中页面的响应调用一次。"""
        # 优雅降级：无 h1 的页面回退取 title，item 永远有可读的标题
        heading = response.css_first("h1") or response.css_first("title")
        yield {
            "url": response.url,
            "heading": str(heading.text).strip() if heading is not None else "",
        }
'''

_MAIN_TEMPLATE = '''\
"""项目入口：组装 Fetcher + Spider 并运行。

本库不用 Scrapy 的 settings.py 全局配置——组装全部显式发生在这里
（构造参数风格）：``Fetcher`` 负责隐身抓取、``Spider`` 负责调度与解析、
``feed_output=`` 把 item 自动落盘（JSONL 追加格式，多次运行可聚合）。

直接运行::

    python main.py
"""

from __future__ import annotations

from spiders.example_spider import TitleSpider
from web_crawler import Fetcher

OUTPUT = "items.jsonl"


def main() -> None:
    """组装并运行 TitleSpider；item 自动写入 items.jsonl。"""
    # 隐身抓取：curl_cffi 重放 chrome131 的 TLS/JA3 指纹（默认值）
    fetcher = Fetcher(impersonate="chrome131")
    # feed_output 便捷参数等价于 Scrapy 的 FEEDS：item 逐条追加到 JSONL
    spider = TitleSpider(fetcher=fetcher, feed_output=OUTPUT)
    items = spider.run()
    print(f"完成：抓到 {len(items)} 条 item，已写入 {OUTPUT}")
    print("进阶：把 TitleSpider 换成 SiteCrawlSpider 体验规则驱动的整站跟随")
    print("     （跟随型 spider 建议带规模上限运行，如 spider.run(max_requests=20)）")


if __name__ == "__main__":
    main()
'''

_REQUIREMENTS_TEMPLATE = "stealth-web-crawler[all]\n"

_README_TEMPLATE = Template(
    """\
# $name

由 `web-crawler-startproject` 生成的爬虫项目骨架（基于
stealth-web-crawler 库）。

## 3 步上手

1. **装依赖**（任选其一）：

   ```bash
   pip install "stealth-web-crawler[all]"
   # 开发本库时改用可编辑安装：在 web-crawler 仓库根目录执行
   # pip install -e ".[all]"
   ```

2. **改目标**：编辑 `spiders/example_spider.py`——把 `start_urls` 换成
   你要抓的站点，把选择器（`css_first("title")` 等）改成页面真实结构。

3. **运行**：

   ```bash
   python main.py
   ```

   item 会写入 `items.jsonl`（JSONL 追加格式）。

## 目录结构

```
$name/
├── spiders/
│   └── example_spider.py   # TitleSpider（基础）+ SiteCrawlSpider（规则跟随）
├── main.py                 # 组装 Fetcher + Spider + feed_output
├── requirements.txt
└── README.md
```

## 换成自己的站点

- 单页/列表页抓取：改 `TitleSpider.start_urls` 与 `parse` 里的选择器；
- 整站跟随：用 `SiteCrawlSpider`，调 `rules` 里的 `LinkExtractor(allow=...)`
  正则与 `allowed_domains`；
- 想即席调试选择器？用交互 shell：`web-crawler-shell https://你的目标`。

## 文档

- 库文档：https://xiaomaozjj666.github.io/web-crawler/
"""
)


# ── 校验与生成 ───────────────────────────────────────────────────────────────


def validate_project_name(name: str) -> str:
    """校验项目名并返回 strip 后的值。

    项目目录会被当作 Python 包导入（``from spiders.xxx import ...``），
    因此必须是合法 Python 标识符且不是关键字——这也天然挡住了
    ``../`` 一类的路径穿越（``/``、``.`` 都不是标识符字符）。
    非法名抛 :class:`~web_crawler.exceptions.ConfigError`。
    """
    cleaned = name.strip()
    if not cleaned:
        raise ConfigError("项目名不能为空")
    if not cleaned.isidentifier():
        raise ConfigError(
            f"项目名 {cleaned!r} 不是合法的 Python 标识符："
            "目录会作为包导入，仅允许字母/数字/下划线且不以数字开头"
        )
    if keyword.iskeyword(cleaned):
        raise ConfigError(f"项目名 {cleaned!r} 是 Python 关键字，请换一个")
    return cleaned


def build_project_files(name: str) -> dict[Path, str]:
    """返回「相对路径 → 文件内容」映射（纯函数，不触盘）。"""
    return {
        Path("spiders") / "example_spider.py": _SPIDER_TEMPLATE,
        Path("main.py"): _MAIN_TEMPLATE,
        Path("requirements.txt"): _REQUIREMENTS_TEMPLATE,
        Path("README.md"): _README_TEMPLATE.substitute(name=name),
    }


def create_project(name: str, base_dir: Path | None = None) -> Path:
    """在 ``base_dir``（默认当前目录）下生成项目骨架，返回项目根路径。

    目标目录已存在且非空时抛 :class:`ConfigError` 拒绝覆盖；已存在但为空
    （或不存在）则正常生成。所有文件以 UTF-8 + LF 写入。
    """
    cleaned = validate_project_name(name)
    root = (base_dir if base_dir is not None else Path.cwd()) / cleaned
    if root.exists():
        if not root.is_dir():
            raise ConfigError(f"路径 {root} 已存在且不是目录，无法在此生成项目")
        if any(root.iterdir()):
            raise ConfigError(f"目录 {root} 已存在且非空，拒绝覆盖；换个项目名或先清理该目录")
    for rel_path, content in build_project_files(cleaned).items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n"：Windows 下 write_text 默认会转 CRLF，显式保持 LF
        target.write_text(content, encoding="utf-8", newline="\n")
    return root


# ── CLI 入口 ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``web-crawler-startproject <name>``。返回 0 成功 / 2 参数错误。"""
    parser = argparse.ArgumentParser(
        prog="web-crawler-startproject",
        description=(
            "生成爬虫项目骨架（对标 scrapy startproject；构造参数风格、无全局配置文件）。"
        ),
        epilog=(
            "示例:  %(prog)s myproject\n"
            '生成后: cd myproject && pip install "stealth-web-crawler[all]" '
            "&& python main.py"
        ),
    )
    parser.add_argument("name", help="项目名（合法 Python 标识符，同时作为目录名）")
    args = parser.parse_args(argv)
    try:
        root = create_project(args.name)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"已生成项目 {root}/")
    print()
    print("下一步：")
    print(f"  cd {root.name}")
    print('  pip install "stealth-web-crawler[all]"    # 开发本库则 pip install -e ".[all]"')
    print("  python main.py                             # item 会写入 items.jsonl")
    print()
    print("文档：https://xiaomaozjj666.github.io/web-crawler/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
