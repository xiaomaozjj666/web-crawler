"""JS 逆向 Agent 命令行工具。

提供与 MCP server 相同能力的命令行入口，支持交互式和单次执行两种模式。

用法::

    # 单次逆向分析
    web-crawler-reverse https://example.com --target-params anti_content sign

    # 交互式 REPL
    web-crawler-reverse --interactive

    # 直接分析 JS 代码片段
    web-crawler-reverse --analyze-js script.js

    # 提取 webpack 模块
    web-crawler-reverse --webpack script.js
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _print_json(data: Any, *, indent: int = 2) -> None:
    """格式化输出 JSON。"""
    print(json.dumps(data, ensure_ascii=False, indent=indent, default=str))


def _make_server(model: str = "deepseek-v4-pro") -> Any:
    """创建 MCP server 实例（复用其内部所有工具）。"""
    from .server import ReverseMCPServer

    return ReverseMCPServer(model=model)


def cmd_reverse_url(args: argparse.Namespace) -> int:
    """逆向分析指定 URL。"""
    server = _make_server(args.model)
    try:
        result = server.handle_tool(
            "reverse_engineer_url",
            {
                "url": args.url,
                "target_params": args.target_params or [],
                "task": args.task or "",
                "max_steps": args.max_steps,
            },
        )
        _print_json(json.loads(result))
    finally:
        server.close()
    return 0


def cmd_inject_hooks(args: argparse.Namespace) -> int:
    """向页面注入 Hook。"""
    server = _make_server(args.model)
    try:
        result = server.handle_tool(
            "inject_hooks",
            {"url": args.url, "hooks": args.hooks or []},
        )
        _print_json(json.loads(result))
    finally:
        server.close()
    return 0


def cmd_analyze_js(args: argparse.Namespace) -> int:
    """分析 JS 代码片段。"""
    code = args.file.read_text(encoding="utf-8") if args.file else args.code
    if not code:
        print("错误：请通过 --file 或位置参数提供 JS 代码", file=sys.stderr)
        return 1
    server = _make_server(args.model)
    try:
        result = server.handle_tool(
            "analyze_js_code",
            {"code": code, "url": args.url or "", "target_param": args.target_param or ""},
        )
        _print_json(json.loads(result))
    finally:
        server.close()
    return 0


def cmd_webpack(args: argparse.Namespace) -> int:
    """提取 webpack 模块。"""
    source = args.file.read_text(encoding="utf-8") if args.file else args.code
    if not source:
        print("错误：请通过 --file 或位置参数提供 JS 源码", file=sys.stderr)
        return 1
    server = _make_server(args.model)
    try:
        result = server.handle_tool("extract_webpack_modules", {"source": source})
        _print_json(json.loads(result))
    finally:
        server.close()
    return 0


def cmd_deobfuscate(args: argparse.Namespace) -> int:
    """AI 反混淆 JS 代码。"""
    code = args.file.read_text(encoding="utf-8") if args.file else args.code
    if not code:
        print("错误：请通过 --file 或位置参数提供 JS 代码", file=sys.stderr)
        return 1
    server = _make_server(args.model)
    try:
        result = server.handle_tool("deobfuscate_js", {"code": code})
        data = json.loads(result)
        if "deobfuscated" in data:
            print(data["deobfuscated"])
        else:
            _print_json(data)
    finally:
        server.close()
    return 0


def cmd_reimplement(args: argparse.Namespace) -> int:
    """用指定语言重写加密逻辑。"""
    code = args.file.read_text(encoding="utf-8") if args.file else args.code
    if not code:
        print("错误：请通过 --file 或位置参数提供 JS 代码", file=sys.stderr)
        return 1
    server = _make_server(args.model)
    try:
        result = server.handle_tool(
            "reimplement_algorithm",
            {"code": code, "language": args.language},
        )
        data = json.loads(result)
        if "code" in data:
            print(data["code"])
        else:
            _print_json(data)
    finally:
        server.close()
    return 0


def cmd_captcha(args: argparse.Namespace) -> int:
    """检测并处理页面验证码。"""
    server = _make_server(args.model)
    try:
        result = server.handle_tool("solve_captcha", {"url": args.url})
        _print_json(json.loads(result))
    finally:
        server.close()
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """捕获页面网络请求。"""
    server = _make_server(args.model)
    try:
        result = server.handle_tool(
            "capture_network_requests",
            {"url": args.url, "wait_time": args.wait},
        )
        _print_json(json.loads(result))
    finally:
        server.close()
    return 0


def cmd_scripts(args: argparse.Namespace) -> int:
    """获取页面加载的 JS 脚本列表。"""
    server = _make_server(args.model)
    try:
        result = server.handle_tool("get_page_scripts", {"url": args.url})
        _print_json(json.loads(result))
    finally:
        server.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """一键运行完整 JS 逆向 Agent（直接调用 ReverseAgent.run，不走 MCP）。

    与 ``reverse`` 子命令的区别：``reverse`` 走 MCP server 的工具调用链路，
    ``run`` 直接构造 :class:`ReverseAgent` 并调用 ``run()``，参数更丰富，
    可保存成功路径脚本到指定文件。
    """
    # 延迟导入：避免 CLI 启动时加载 camoufox 等重依赖
    from web_crawler.ai.llm import DEFAULT_MODEL, DeepSeekProvider
    from web_crawler.ai.reverse_agent import ReverseAgent, ReverseAgentConfig

    target_params: list[str] = []
    if args.target_params:
        target_params = [p.strip() for p in args.target_params.split(",") if p.strip()]

    allowed_domains: list[str] | None = None
    if args.allowed_domains:
        allowed_domains = [d.strip() for d in args.allowed_domains.split(",") if d.strip()]

    config = ReverseAgentConfig(
        max_steps=args.max_steps,
        target_params=target_params or None,
        headless=args.headless,
        proxy=args.proxy or None,
        os_name=args.os,
        enable_checkpoint=args.enable_checkpoint,
        budget_total=args.budget_total,
        budget_per_step=args.budget_per_step,
        min_confidence=args.min_confidence,
        enable_guard=args.enable_guard,
        allowed_domains=allowed_domains,
        enable_screenshot=args.enable_screenshot,
    )

    provider = DeepSeekProvider(model=args.model or DEFAULT_MODEL)
    agent = ReverseAgent(config=config, provider=provider)
    try:
        result = agent.run(url=args.url, task=args.task or "")
    finally:
        agent.close()

    # 保存成功路径脚本
    if args.save_script and result.get("compiled_script"):
        try:
            Path(args.save_script).write_text(str(result["compiled_script"]), encoding="utf-8")
            print(f"成功路径脚本已保存：{args.save_script}", file=sys.stderr)
        except OSError as exc:
            print(f"保存脚本失败：{exc}", file=sys.stderr)

    # 输出 JSON 结果
    if args.output and args.output != "-":
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"结果已写入：{args.output}", file=sys.stderr)
    else:
        _print_json(result)
    return 0


def cmd_interactive(args: argparse.Namespace) -> int:
    """交互式 REPL 模式。"""
    server = _make_server(args.model)
    print("web-crawler JS 逆向 Agent — 交互模式")
    print("输入命令，Ctrl+C 退出\n")
    print("可用命令:")
    print("  reverse <url> [--target-params p1 p2]  逆向分析 URL")
    print("  hooks <url> [--hooks fetch xhr]        注入 Hook")
    print("  analyze <file|code>                    分析 JS 代码")
    print("  webpack <file|code>                     提取 webpack 模块")
    print("  deobfuscate <file|code>                反混淆 JS")
    print("  reimplement <file|code> [--lang python] 重写算法")
    print("  captcha <url>                           处理验证码")
    print("  capture <url> [--wait 5]               捕获网络请求")
    print("  scripts <url>                          获取 JS 脚本列表")
    print("  tools                                   列出所有工具")
    print("  exit                                    退出\n")
    try:
        while True:
            try:
                line = input(">>> ").strip()
            except EOFError:
                break
            if not line or line in ("exit", "quit", "q"):
                break
            parts = line.split()
            cmd = parts[0]
            if cmd == "tools":
                tools = server.get_tools()
                for t in tools:
                    print(f"  {t['name']:30s} {t.get('description', '')[:60]}")
                continue
            if cmd == "reverse" and len(parts) >= 2:
                url = parts[1]
                target = []
                if "--target-params" in parts:
                    idx = parts.index("--target-params")
                    target = parts[idx + 1 :]
                result = server.handle_tool(
                    "reverse_engineer_url",
                    {"url": url, "target_params": target, "task": "", "max_steps": 20},
                )
                _print_json(json.loads(result))
            elif cmd == "hooks" and len(parts) >= 2:
                result = server.handle_tool("inject_hooks", {"url": parts[1], "hooks": []})
                _print_json(json.loads(result))
            elif cmd in ("analyze", "webpack", "deobfuscate", "reimplement") and len(parts) >= 2:
                target_str: str = parts[1]
                code = (
                    Path(target_str).read_text(encoding="utf-8")
                    if Path(target_str).exists()
                    else target_str
                )
                tool_map = {
                    "analyze": "analyze_js_code",
                    "webpack": "extract_webpack_modules",
                    "deobfuscate": "deobfuscate_js",
                    "reimplement": "reimplement_algorithm",
                }
                kwargs: dict[str, Any] = {"code": code}
                if cmd == "reimplement":
                    kwargs["language"] = "python"
                result = server.handle_tool(tool_map[cmd], kwargs)
                _print_json(json.loads(result))
            elif cmd == "captcha" and len(parts) >= 2:
                result = server.handle_tool("solve_captcha", {"url": parts[1]})
                _print_json(json.loads(result))
            elif cmd == "capture" and len(parts) >= 2:
                result = server.handle_tool(
                    "capture_network_requests", {"url": parts[1], "wait_time": 5.0}
                )
                _print_json(json.loads(result))
            elif cmd == "scripts" and len(parts) >= 2:
                result = server.handle_tool("get_page_scripts", {"url": parts[1]})
                _print_json(json.loads(result))
            else:
                print(f"未知命令或参数不足: {line}")
    except KeyboardInterrupt:
        print("\n退出")
    finally:
        server.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="web-crawler-reverse",
        description="JS 逆向 Agent 命令行工具 — Camoufox + DeepSeek V4 Pro 驱动",
    )
    parser.add_argument(
        "--model",
        default="deepseek-v4-pro",
        help="DeepSeek 模型名（默认 deepseek-v4-pro）",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # reverse
    p = sub.add_parser("reverse", help="逆向分析指定 URL 的加密参数")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--target-params", nargs="*", default=[], help="目标加密参数名")
    p.add_argument("--task", default="", help="任务描述")
    p.add_argument("--max-steps", type=int, default=20, help="最大步数")
    p.set_defaults(func=cmd_reverse_url)

    # hooks
    p = sub.add_parser("hooks", help="向页面注入 JS Hook")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--hooks", nargs="*", default=[], help="Hook 名称列表")
    p.set_defaults(func=cmd_inject_hooks)

    # analyze
    p = sub.add_parser("analyze", help="分析 JS 代码片段的加密逻辑")
    p.add_argument("code", nargs="?", default="", help="JS 代码（或用 --file）")
    p.add_argument("--file", type=Path, help="JS 文件路径")
    p.add_argument("--url", default="", help="来源 URL（可选）")
    p.add_argument("--target-param", default="", help="目标参数名")
    p.set_defaults(func=cmd_analyze_js)

    # webpack
    p = sub.add_parser("webpack", help="从 JS 源码提取 webpack 模块")
    p.add_argument("code", nargs="?", default="", help="JS 源码（或用 --file）")
    p.add_argument("--file", type=Path, help="JS 文件路径")
    p.set_defaults(func=cmd_webpack)

    # deobfuscate
    p = sub.add_parser("deobfuscate", help="AI 反混淆 JS 代码")
    p.add_argument("code", nargs="?", default="", help="JS 代码（或用 --file）")
    p.add_argument("--file", type=Path, help="JS 文件路径")
    p.set_defaults(func=cmd_deobfuscate)

    # reimplement
    p = sub.add_parser("reimplement", help="用指定语言重写加密逻辑")
    p.add_argument("code", nargs="?", default="", help="JS 代码（或用 --file）")
    p.add_argument("--file", type=Path, help="JS 文件路径")
    p.add_argument("--language", default="python", help="目标语言（默认 python）")
    p.set_defaults(func=cmd_reimplement)

    # captcha
    p = sub.add_parser("captcha", help="检测并处理页面验证码")
    p.add_argument("url", help="目标 URL")
    p.set_defaults(func=cmd_captcha)

    # capture
    p = sub.add_parser("capture", help="捕获页面网络请求")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--wait", type=float, default=5.0, help="等待时间（秒）")
    p.set_defaults(func=cmd_capture)

    # scripts
    p = sub.add_parser("scripts", help="获取页面加载的 JS 脚本列表")
    p.add_argument("url", help="目标 URL")
    p.set_defaults(func=cmd_scripts)

    # interactive
    p = sub.add_parser("interactive", aliases=["repl"], help="交互式 REPL 模式")
    p.set_defaults(func=cmd_interactive)

    # run — 一键运行完整 ReverseAgent（不走 MCP，直接调用 agent.run）
    p = sub.add_parser(
        "run",
        help="一键运行完整 JS 逆向 Agent（直接调用 ReverseAgent.run）",
    )
    p.add_argument("--url", required=True, help="目标 URL（必填）")
    p.add_argument("--task", default="", help="自然语言任务描述")
    p.add_argument(
        "--target-params",
        default="",
        help="目标加密参数名（逗号分隔，如 anti_content,sign）",
    )
    p.add_argument("--max-steps", type=int, default=20, help="最大步数（默认 20）")
    p.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="无头模式（默认 False，可见浏览器）",
    )
    p.add_argument("--proxy", default="", help="代理（如 http://u:p@host:port）")
    p.add_argument("--os", default="windows", help="OS 指纹（默认 windows）")
    p.add_argument(
        "--enable-checkpoint",
        action="store_true",
        default=False,
        help="启用断点续跑",
    )
    p.add_argument("--budget-total", type=int, default=100_000, help="全局 token 上限")
    p.add_argument("--budget-per-step", type=int, default=8_000, help="单步 token 上限")
    p.add_argument(
        "--min-confidence",
        type=float,
        default=0.4,
        help="动作置信度阈值（0-1，默认 0.4）",
    )
    p.add_argument(
        "--enable-guard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="启用危险动作护栏（默认开，用 --no-enable-guard 禁用）",
    )
    p.add_argument(
        "--allowed-domains",
        default="",
        help="允许导航的域名白名单（逗号分隔，留空不限制）",
    )
    p.add_argument(
        "--enable-screenshot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="启用每步截图（默认开，用 --no-enable-screenshot 禁用）",
    )
    p.add_argument(
        "--output",
        default="-",
        help="输出文件路径（默认 - 表示 stdout，输出 JSON 结果）",
    )
    p.add_argument(
        "--save-script",
        default="",
        help="保存成功路径脚本到指定文件",
    )
    p.set_defaults(func=cmd_run)

    return parser


def main() -> None:
    """CLI 入口。"""
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
