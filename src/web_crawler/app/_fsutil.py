"""文件写入小工具：原子写（唯一 tmp + fsync + ``os.replace``）。

报告类产物（run_report.json / summary.txt / 清单 JSON/CSV / 离线索引）是用户
事后复核抓取结果的唯一依据。直接 ``write_text`` 会先截断目标文件；进程在写入
中途被杀（Ctrl+C、崩溃、断电）就留下半截 JSON——下一次读取要么解析失败，
要么把截断的数据当成完整结果。原子写保证读者要么看到旧文件，要么看到新文件。

本模块是叶子模块（只依赖标准库），供报告写入器与离线归档器共用，避免
``_crawler_context`` 与报告模块之间的循环依赖。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


def _unique_temp(path: Path) -> tuple[int, Path]:
    """在目标同目录创建唯一临时文件（并发写同一目标不会互相截断）。

    大批量报告写入时父目录几乎总是已存在：先直接 mkstemp、仅当目录缺失时
    再补 mkdir（实测每写一次省掉 mkdir 的 2 次 stat + 1 次 create，约 0.5ms/写）。
    """
    try:
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    return descriptor, Path(name)


def _replace_or_copy(temporary: Path, path: Path) -> None:
    """原子替换目标；Windows 目标被占用时退化为直接拷贝（尽力落盘）。"""
    try:
        os.replace(temporary, path)
    except PermissionError:
        # 目标被查看器/杀毒/索引器以不可删除方式打开：原子替换不可用。
        # 直接覆盖通常仍可写——优先保证报告存在，原子性此时让位于可用性。
        shutil.copyfile(temporary, path)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """把 ``text`` 原子写入 ``path``（同目录唯一 tmp + fsync + ``os.replace``）。

    * tmp 名由 :func:`tempfile.mkstemp` 生成（唯一），并发写同一目标不会互相
      截断；``finally`` 只清理自己的 tmp。
    * 落盘前 ``flush + fsync``：Ctrl+C/崩溃/断电都不会留下半截文件。
    """
    descriptor, temporary = _unique_temp(path)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_or_copy(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


@contextmanager
def atomic_text_writer(
    path: Path, *, encoding: str = "utf-8", newline: str = ""
) -> Iterator[TextIO]:
    """流式原子写：向 yield 的文本句柄写完后再 fsync + 替换目标。

    用于 CSV 这类由 ``csv.writer`` 逐行写入、不方便先拼成整串的产物。
    """
    descriptor, temporary = _unique_temp(path)
    handle = os.fdopen(descriptor, "w", encoding=encoding, newline=newline)
    try:
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        _replace_or_copy(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink(missing_ok=True)
