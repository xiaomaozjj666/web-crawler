"""真实浏览器的 Web UI 冒烟测试（可选执行，未装浏览器时整组 skip）。

``tests/test_ui_presets.py`` 只做正则静态断言，看不到预设脚本与页面实际
渲染/事件绑定的差异（「预设 + JS 渲染误标」这类缺陷能直接漏过）。本组测试
真实启动 ``crawler-ui``（子进程、随机端口、临时数据库），用 Playwright 驱动
Chromium 验证页面的关键交互与响应式行为。

本地执行：:

    python -m playwright install chromium   # 只需一次
    pytest -m ui_smoke
    pytest tests/test_ui_smoke.py -q

Playwright 未安装时整模块 skip（带安装指引）；浏览器缺失时模块级 fixture
skip。测试内绝不下载浏览器，也不读写仓库里的 ``crawler_data.db``。
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui_smoke

# 最小依赖档（CI test 矩阵）没装 Playwright：整模块跳过而不是报错，
# 也不在测试里触发浏览器下载。
sync_api = pytest.importorskip(
    "playwright.sync_api",
    reason=(
        "需要 playwright 与其 Chromium 才能运行 Web UI 冒烟测试："
        "pip install playwright && python -m playwright install chromium"
    ),
)
expect = sync_api.expect

READY_TIMEOUT = 60.0

# 与 static/index.html 里 BASE 独立维护：测试要能发现页面脚本把预设含义改错，
# 不能从被测脚本里读期望值（否则改错两边一起错）。
BASE_CHECKS = {
    "same_domain": True,
    "include_css_urls": True,
    "crawl_pages": False,
    "sitemap": False,
    "expand_playlists": False,
    "resume_crawl": False,
    "list_only": False,
    "video_mode": False,
    "video_only": False,
    "resume": True,
    "dedup": True,
    "download_linked_data": False,
    "organize": True,
    "strip_overlays": True,
    "rewrite_html": False,
    "smart_extract": False,
    "extract_text": False,
    "extract_markdown": False,
}

# 预设键 -> (相对 BASE 的覆盖项, max_pages, 按钮文案)
PRESET_CASES = {
    "single": ({}, 1, "单页资源"),
    "mirror": (
        {
            "crawl_pages": True,
            "sitemap": True,
            "rewrite_html": True,
            "resume_crawl": True,
            "organize": False,
        },
        200,
        "整站镜像",
    ),
    "markdown": ({"list_only": True, "extract_markdown": True}, 1, "正文转 Markdown"),
    "video": ({"list_only": True, "video_mode": True, "video_only": True}, 1, "视频资源清单"),
}

# 只在下载阶段生效的选项；勾选「只生成清单」后应整组置灰
MOOT_NAMES = ("resume", "dedup", "decrypt", "include_css_urls", "expand_playlists", "max_bytes")

_FORM_STATE_JS = """
(names) => {
  const form = document.getElementById('crawler-form');
  const state = {};
  for (const name of names) {
    const field = form.elements.namedItem(name);
    if (field) state[name] = field.type === 'checkbox' ? field.checked : field.value;
  }
  state.max_pages = form.elements.namedItem('max_pages').value;
  state.pressed = [...document.querySelectorAll('.preset-btn')]
    .filter((b) => b.getAttribute('aria-pressed') === 'true')
    .map((b) => b.dataset.preset);
  state.status = document.getElementById('preset-status').textContent;
  state.moot = document.querySelectorAll('.is-moot').length;
  return state;
}
"""

_MOOT_JS = """
(names) => {
  const form = document.getElementById('crawler-form');
  const flags = {};
  for (const name of names) {
    const field = form.elements.namedItem(name);
    if (!field) { flags[name] = null; continue; }
    const box = field.type === 'checkbox' ? field.closest('label') : field.closest('div');
    flags[name] = Boolean(box && box.classList.contains('is-moot'));
  }
  return flags;
}
"""


def _free_port() -> int:
    """向系统要一个空闲回环端口（绑定后立即释放，交给子进程使用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - 日志文件异常时只需提示
        return f"<无法读取服务日志：{exc}>"
    return text[-limit:]


def _wait_until_ready(proc: subprocess.Popen, url: str, log_path: Path) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    last_error = "尚未发出请求"
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            pytest.fail(f"UI 服务进程提前退出（退出码 {code}）：\n{_tail(log_path)}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except Exception as exc:  # 就绪探测期间任何异常都只表示"还没好"
            last_error = str(exc)
        time.sleep(0.2)
    pytest.fail(
        f"UI 服务在 {READY_TIMEOUT:.0f}s 内未就绪（{url}）：{last_error}\n{_tail(log_path)}"
    )


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """启动真实的 crawler-ui 子进程，指向临时数据库，结束后强制清理。"""
    workdir = tmp_path_factory.mktemp("ui-smoke")
    db_path = workdir / "crawler.db"
    log_path = workdir / "server.log"
    port = _free_port()
    env = os.environ.copy()
    # 关键隔离：绝不让测试写仓库里的 crawler_data.db
    env["CRAWLER_DB_PATH"] = str(db_path)
    env["PYTHONUNBUFFERED"] = "1"
    url = f"http://127.0.0.1:{port}/"
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "web_crawler.app.ui", "--port", str(port), "-q"],
            cwd=str(workdir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            _wait_until_ready(proc, url, log_path)
            assert db_path.is_file(), f"服务未使用 CRAWLER_DB_PATH 指定的临时库：{db_path}"
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 服务无响应时的兜底
                proc.kill()
                proc.wait(timeout=10)


@pytest.fixture(scope="module")
def chromium():
    """真实 Chromium；未安装浏览器时整组 skip（不在测试里下载）。

    conftest 为 curl_cffi 把会话事件循环策略钉成 ``WindowsSelectorEventLoopPolicy``，
    而 Playwright 的同步驱动在 Windows 上必须用 Proactor 才能拉起 node 子进程
    （Selector 下 ``create_subprocess_exec`` 直接 NotImplementedError）。这里只在
    浏览器夹具生命周期内切换，结束后还原，避免影响其他测试。
    """
    previous_policy = None
    if sys.platform == "win32":
        with warnings.catch_warnings():
            # Python 3.14 起事件循环策略 API 有弃用告警；项目 filterwarnings=error
            warnings.simplefilter("ignore", DeprecationWarning)
            previous_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:  # 缺浏览器/驱动的表现因平台而异
                pytest.skip(
                    "无法启动 Playwright Chromium（多半是没装浏览器）："
                    f"{exc}；安装命令：python -m playwright install chromium"
                )
            try:
                yield browser
            finally:
                browser.close()
    finally:
        if previous_policy is not None:
            asyncio.set_event_loop_policy(previous_policy)


@contextmanager
def _open_ui(chromium, url: str, width: int = 1280, height: int = 900):
    """打开一个隔离的浏览器上下文，收集 JS 错误。"""
    context = chromium.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

    def _on_console(msg) -> None:
        if msg.type == "error":
            errors.append(f"console.error: {msg.text}")

    page.on("console", _on_console)
    try:
        page.goto(url, wait_until="load")
        page.wait_for_selector("#crawler-form", state="visible")
        yield page, errors
    finally:
        context.close()


@pytest.fixture
def ui_page(chromium, ui_server):
    with _open_ui(chromium, ui_server) as session:
        yield session


def test_page_shows_title_and_crawl_form(ui_page):
    page, _errors = ui_page
    expect(page).to_have_title("Web Crawler 控制台")
    expect(page.locator("#crawler-form")).to_be_visible()
    expect(page.locator("#max_pages")).to_be_visible()
    expect(page.locator("#run-button")).to_have_text("开始采集")
    assert page.locator(".preset-btn").count() == len(PRESET_CASES)


@pytest.mark.parametrize("preset", list(PRESET_CASES))
def test_presets_set_fields_and_highlight(ui_page, preset):
    page, _errors = ui_page
    overrides, max_pages, label = PRESET_CASES[preset]
    # 先从另一个预设切过来，顺带验证上一次的残留状态会被覆盖
    other = "markdown" if preset == "mirror" else "mirror"
    page.click(f'button[data-preset="{other}"]')
    page.click(f'button[data-preset="{preset}"]')

    state = page.evaluate(_FORM_STATE_JS, list(BASE_CHECKS))
    expected = {**BASE_CHECKS, **overrides}
    assert {name: state[name] for name in BASE_CHECKS} == expected
    assert state["max_pages"] == str(max_pages)
    assert state["pressed"] == [preset]
    assert page.locator(f'button[data-preset="{preset}"]').get_attribute("aria-pressed") == "true"
    assert f"已应用「{label}」" in state["status"]


def test_manual_change_switches_to_custom_combination(ui_page):
    page, _errors = ui_page
    page.click('button[data-preset="mirror"]')
    expect(page.locator("#preset-status")).to_have_text("已应用「整站镜像」，可继续微调下面的选项")

    # 数字框 input 事件路径
    page.fill("#max_pages", "5")
    expect(page.locator("#preset-status")).to_have_text("当前为自定义组合")
    assert page.locator('.preset-btn[aria-pressed="true"]').count() == 0

    # 复选框 change 事件路径
    page.click('button[data-preset="single"]')
    assert page.locator('.preset-btn[aria-pressed="true"]').count() == 1
    page.locator('input[name="sitemap"]').check()
    expect(page.locator("#preset-status")).to_have_text("当前为自定义组合")
    assert page.locator('.preset-btn[aria-pressed="true"]').count() == 0


def test_list_only_marks_download_options_moot(ui_page):
    page, _errors = ui_page
    assert page.locator(".is-moot").count() == 0
    list_only = page.locator('input[name="list_only"]')
    resume_label = page.locator('label.check:has(input[name="resume"])')

    list_only.check()
    expect(page.locator(".is-moot")).to_have_count(len(MOOT_NAMES))
    flags = page.evaluate(_MOOT_JS, [*MOOT_NAMES, "respect_robots"])
    assert {name: flags[name] for name in MOOT_NAMES} == dict.fromkeys(MOOT_NAMES, True)
    assert flags["respect_robots"] is False
    assert resume_label.get_attribute("title") is not None

    list_only.uncheck()
    expect(page.locator(".is-moot")).to_have_count(0)
    flags = page.evaluate(_MOOT_JS, list(MOOT_NAMES))
    assert flags == dict.fromkeys(MOOT_NAMES, False)
    assert resume_label.get_attribute("title") is None


def test_narrow_viewport_has_no_horizontal_scroll_and_touch_targets(chromium, ui_server):
    with _open_ui(chromium, ui_server, width=390, height=844) as (page, errors):
        metrics = page.evaluate(
            """() => {
              const de = document.documentElement;
              return {
                scrollWidth: de.scrollWidth,
                clientWidth: de.clientWidth,
                bodyScrollWidth: document.body.scrollWidth,
                heights: [...document.querySelectorAll('.preset-btn')]
                  .map((b) => b.getBoundingClientRect().height),
              };
            }"""
        )
        assert metrics["scrollWidth"] <= metrics["clientWidth"], (
            f"390px 视口出现横向滚动：scrollWidth={metrics['scrollWidth']} "
            f"clientWidth={metrics['clientWidth']}"
        )
        assert metrics["bodyScrollWidth"] <= metrics["clientWidth"]
        assert len(metrics["heights"]) == len(PRESET_CASES)
        assert all(height >= 44 for height in metrics["heights"]), metrics["heights"]
        assert errors == [], "窄屏页面出现 JS 错误：\n" + "\n".join(errors)


def test_console_has_no_js_errors(ui_page):
    page, errors = ui_page
    for preset in PRESET_CASES:
        page.click(f'button[data-preset="{preset}"]')
    list_only = page.locator('input[name="list_only"]')
    list_only.check()
    list_only.uncheck()
    for tab in ("history", "captcha", "reverse", "crawler"):
        page.click(f"#tabbtn-{tab}")
        expect(page.locator(f"#tab-{tab}")).to_have_class(re.compile(r"\bactive\b"))
    page.wait_for_timeout(500)
    assert errors == [], "页面执行期间出现 JS 错误：\n" + "\n".join(errors)
