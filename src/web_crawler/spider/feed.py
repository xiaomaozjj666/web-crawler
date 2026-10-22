"""Feed 导出管道（对标 Scrapy FEEDS 的 item 数据落盘导出）。

按 ``output`` 路径后缀选择导出格式：

- ``.json`` — 单次快照（覆盖写）：``process_item`` 时缓存到内存，
  ``close`` 收尾钩子触发时一次性 ``json.dump``（缩进 2）；
- ``.jsonl`` — JSON Lines 逐条追加（``ensure_ascii=False``）：多次运行
  天然聚合，适合断点续爬累积数据；
- ``.csv`` — 首条 item 定列（表头），后续行按首条列对齐，缺失列补
  空串、多余列记 warning 丢弃。

容错语义与 ``Spider._export_stats`` 同源："导出绝不连累已抓数据"——
写盘失败（磁盘满 / 权限）捕获 ``OSError`` 记 warning，item 照常通过
管道；``close`` 处于 run()/stream() 的 finally 链上，异常穿透会打断
后续收尾步骤（stats 导出），因此同样只记日志不抛出。父目录不存在时
自动逐级创建。

并发模型：stream() 的 worker 协程在同一事件循环内交错调用
``process_item``，但写盘是同步无 ``await`` 点的临界区——JSONL/CSV
采用"每条一开一关"的追加写（简单且天然互斥），JSON 快照只在
``close`` 单次写入、无需锁。仅使用标准库。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from ..exceptions import ConfigError
from .spider import ItemPipeline, Spider

logger = logging.getLogger(__name__)

__all__ = ["FeedExportPipeline"]


class FeedExportPipeline(ItemPipeline):
    """item 数据落盘导出管道（JSON 快照 / JSONL 追加 / CSV）。

    用法一：声明在 ``item_pipelines`` 上（可与变换管道串联，落盘放最后）::

        class QuotesSpider(Spider):
            item_pipelines = [CleanPipeline, FeedExportPipeline("quotes.jsonl")]

    用法二：``Spider(feed_output=...)`` 便捷参数——自动把管道实例追加
    到声明管道之后（先变换、后落盘），等价于 Scrapy 的 ``FEEDS`` 配置。

    与 ``stats_output``（可观测性导出）互补：stats 落的是运行度量，
    feed 落的是业务数据本身，因此 feed 收尾先于 stats 导出执行
    （数据优先于可观测性）。未知后缀在构造时抛
    :class:`~web_crawler.exceptions.ConfigError`。
    """

    def __init__(self, output: str | Path) -> None:
        self.path = Path(output)
        self._format = self.path.suffix.lower()
        if self._format not in (".json", ".jsonl", ".csv"):
            raise ConfigError(
                f"unsupported feed output {output!r}: expected a .json / .jsonl / .csv path"
            )
        # JSON 快照的内存缓存（close 时一次性落盘；空运行也产出 []）
        self._buffer: list[Any] = []
        # CSV 首条 item 定下的列（None = 尚未见到 item）
        self._fieldnames: list[str] | None = None
        # close 幂等守卫：重复调用（用户手动 + 引擎收尾）不重写快照
        self._closed = False

    def process_item(self, item: Any, spider: Spider) -> Any:
        """写盘（或缓存）并原样返回 item——导出失败绝不连累数据。"""
        if self._format == ".json":
            # 快照语义：先缓存，close 时一次性落盘
            self._buffer.append(item)
        elif self._format == ".jsonl":
            self._write_jsonl(item)
        else:
            self._write_csv(item)
        return item

    def close(self, spider: Spider) -> None:
        """运行收尾钩子：JSON 快照此时一次性落盘（其他格式无操作）。

        由 ``Spider._close_item_pipelines`` 在 run()/stream() 的
        finally 链上调用（正常结束 / 异常 / 暂停均触发）；写盘失败
        只记 warning，不打断后续收尾步骤。
        """
        if self._closed:
            return
        self._closed = True
        if self._format != ".json":
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # default=str 对齐 _export_stats：item 含不可序列化对象时降级
            # 为 str 表示，不让 TypeError 打断 finally 链
            self.path.write_text(
                json.dumps(self._buffer, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("feed export to %s failed: %s", self.path, exc)

    def _write_jsonl(self, item: Any) -> None:
        """逐条追加 JSON Lines（每条一开一关，断点续爬天然聚合）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                # default=str：不可序列化字段降级为 str，写入失败不连累 item
                fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.warning("feed export to %s failed: %s", self.path, exc)

    def _write_csv(self, item: Any) -> None:
        """按首条 item 定列追加 CSV 行（缺失列空串、多余列丢弃）。"""
        if not isinstance(item, dict):
            logger.warning(
                "csv feed %s: skipping non-mapping item of type %s",
                self.path,
                type(item).__name__,
            )
            return
        if self._fieldnames is None:
            self._fieldnames = list(item.keys())
        # 值为 None 与缺失列统一写空串（str(None) 的 "None" 无业务价值）
        row = ["" if (v := item.get(f)) is None else str(v) for f in self._fieldnames]
        extra = [k for k in item if k not in self._fieldnames]
        if extra:
            logger.warning("csv feed %s: dropping extra keys %s", self.path, sorted(extra))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 表头只在文件不存在/为空时写：续爬追加不带重复表头
            write_header = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                if write_header:
                    writer.writerow(self._fieldnames)
                writer.writerow(row)
        except OSError as exc:
            logger.warning("feed export to %s failed: %s", self.path, exc)
