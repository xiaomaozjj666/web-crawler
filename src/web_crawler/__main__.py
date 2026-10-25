"""``python -m web_crawler`` 直跑入口。

对标 ``python -m pip`` 的可用性：无参数打印版本与一行指引（不进入任何
子命令，避免与 ``web-crawler`` 等 6 个专门入口职责重叠）；
``--version`` / ``-V`` 打印 ``web-crawler <版本>``，与各 CLI 入口的
``-V/--version`` 输出风格保持一致。
"""

from __future__ import annotations

import sys

from web_crawler import __version__

_HELP = (
    f"web-crawler {__version__} — 隐身爬虫 / JS 逆向 / 资源下载 / Web UI / MCP 工具箱\n"
    "\n"
    "可用命令行入口:\n"
    "  web-crawler              资源下载器 CLI（抓取与下载）\n"
    "  web-crawler-shell        交互式调试 shell（对标 scrapy shell）\n"
    "  web-crawler-startproject 生成项目骨架（对标 scrapy startproject）\n"
    "  web-crawler-reverse      JS 逆向 Agent CLI\n"
    "  web-crawler-mcp          MCP 服务器\n"
    "  crawler-ui               本地 Web 控制台\n"
    "\n"
    "文档: https://xiaomaozjj666.github.io/web-crawler/\n"
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args in (["--version"], ["-V"]):
        print(f"web-crawler {__version__}")
        return 0
    if args in (["--help"], ["-h"]):
        print(_HELP)
        return 0
    if args:
        print(f"未知参数: {' '.join(args)}（本入口不接受子命令）", file=sys.stderr)
        print(_HELP, file=sys.stderr)
        return 2
    print(_HELP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
