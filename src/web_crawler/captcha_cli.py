"""web-captcha 命令行入口：验证码识别的统一出口。

两个子命令：

- ``page <url>``：Camoufox（反指纹 Firefox）打开页面 → 自动检测验证码类型
  → 路由到对应求解器 → 输出结构化结果（JSON）。一条命令走完
  detect → solve 主线。
- ``image <path>``：对本地图片做文本/数学识别（``--math`` 开启
  math_mode，输出算式答案而非原文）。

退出码：0 = 成功或页面无验证码；1 = 失败/未通过；2 = 用法错误。

示例::

    web-captcha page https://example.com/login --headless
    web-captcha image captcha.png --math
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from web_crawler.compat import require_camoufox


def _build_image_solver(math_mode: bool) -> Any:
    """构造 ImageCaptchaSolver：有 key（env/.env）则带 LLM Vision，无则纯本地。"""
    from web_crawler.ai.image_captcha import ImageCaptchaSolver, ImageSolverConfig
    from web_crawler.ai.llm import get_vision_provider

    provider = get_vision_provider(None)
    cfg = ImageSolverConfig(math_mode=math_mode)
    return ImageCaptchaSolver(provider=provider, config=cfg)


def cmd_page(args: argparse.Namespace) -> int:
    """page 子命令：URL 一键式 detect → solve。"""
    from ..ai.captcha import CaptchaManager

    require_camoufox()
    image_solver = _build_image_solver(math_mode=False)
    manager = CaptchaManager(image_solver=image_solver)
    result = manager.solve_url(args.url, headless=args.headless)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if result["solved"] is None:
        return 0  # 页面无验证码
    return 0 if result["solved"] else 1


def cmd_image(args: argparse.Namespace) -> int:
    """image 子命令：本地图片文本/数学识别。"""
    solver = _build_image_solver(math_mode=args.math)
    with open(args.path, "rb") as f:
        text = solver.solve_text(f.read(), mime="image/png")
    if not text:
        print(
            json.dumps(
                {"ok": False, "error": "识别失败（本地与 LLM 路径均无结果）"}, ensure_ascii=False
            )
        )
        return 1
    print(json.dumps({"ok": True, "text": text, "math": args.math}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="web-captcha",
        description="验证码识别统一入口：page（URL 一键式）/ image（本地图片）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_page = sub.add_parser("page", help="Camoufox 打开 URL，自动检测并求解验证码")
    p_page.add_argument("url", help="目标页面 URL")
    p_page.add_argument("--headless", action="store_true", help="无头模式（默认有头，行为更真实）")
    p_page.set_defaults(func=cmd_page)

    p_img = sub.add_parser("image", help="本地图片文本/数学识别")
    p_img.add_argument("path", help="图片文件路径（png/jpg）")
    p_img.add_argument("--math", action="store_true", help="数学运算验证码：输出算式答案")
    p_img.set_defaults(func=cmd_image)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ImportError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
