"""文件写入小工具：原子写（tmp + ``os.replace``）。

报告类产物（run_report.json / summary.txt / 清单 JSON / 离线索引）是用户事后
复核抓取结果的唯一依据。直接 ``write_text`` 会先截断目标文件；进程在写入中途
被杀（Ctrl+C、崩溃、断电）就留下半截 JSON——下一次读取要么解析失败，要么把
截断的数据当成完整结果。原子写保证读者要么看到旧文件，要么看到新文件。

本模块是叶子模块（只依赖标准库），供报告写入器与离线归档器共用，避免
``_crawler_context`` 与报告模块之间的循环依赖。
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """把 ``text`` 原子写入 ``path``（同目录 ``.tmp`` + ``os.replace``）。"""
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding=encoding)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
