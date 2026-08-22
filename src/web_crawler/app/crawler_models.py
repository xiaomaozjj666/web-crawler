"""网页资源爬虫的共享数据模型。

定义拆分后各爬虫模块共用的两个 dataclass：

- :class:`Resource` —— 扫描页面时发现的资源。
- :class:`ManifestRow` —— 可下载资源清单中的一行。

两个类原本定义在 :mod:`app.crawler` 中；移到这里是为了让网络/解析模块
（:mod:`app.crawler_net`）与报告模块（:mod:`app.crawler_report`）无需
导入 ``app.crawler``（那会形成循环导入）即可构造和标注它们。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    url: str
    found_in: str
    kind: str
    page_url: str


@dataclass
class ManifestRow:
    status: str
    url: str
    saved_path: str
    content_type: str
    bytes: int
    category: str
    found_in: str
    kind: str
    page_url: str
    page_title: str
    diagnostic: str
    sha256: str = ""
