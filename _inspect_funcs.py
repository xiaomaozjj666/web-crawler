"""检视指定测试文件中缺 docstring 的测试函数（只读）。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def has_parametrize(node: ast.FunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "parametrize":
            return True
        if isinstance(target, ast.Name) and "parametrize" in getattr(target, "id", ""):
            return True
    return False


def first_stmt(src: str, node: ast.FunctionDef) -> str:
    body = node.body
    if not body:
        return ""
    lines = src.splitlines()
    snippets = []
    for s in body[:3]:
        start = s.lineno - 1
        end = getattr(s, "end_lineno", s.lineno)
        chunk = " ".join(l.strip() for l in lines[start:end] if l.strip())
        if chunk:
            snippets.append(chunk)
    return " | ".join(snippets)[:160]


def main() -> None:
    path = Path(sys.argv[1])
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_") or ast.get_docstring(node):
            continue
        flag = "P" if has_parametrize(node) else " "
        print(f"{node.lineno}\t{flag}\t{node.name}\t{first_stmt(src, node)}")


if __name__ == "__main__":
    main()
