"""ReverseAgent 的低耦合纯助手与数据结构层。

从 :mod:`web_crawler.ai.reverse_agent` 抽出的零状态部分，便于独立单测：

- 数据结构：``Observation`` / ``Action`` / ``ReverseAgentConfig``
  （经 reverse_agent 薄 re-export，历史导入路径不变）；
- 通用纯函数：JS 字符串转义、SSRF 校验、hook 记录参数搜索、
  文件名清理、截图滚动清理、checkpoint 快照读取、hook 数据合并、
  降级动作构造；
- JS 分析辅助：脚本抓取（async httpx）与最优 fragment 评分。

设计约束：本模块不得反向依赖 reverse_agent（避免循环导入）；所有与
self 状态耦合的编排（主循环、动作执行、页面监听）仍留在 reverse_agent。
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analyzer import AnalysisResult, JSAnalyzer, JSFragment
from .captcha import CaptchaType
from .checkpoint import CheckpointStore

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """单步观察结果，描述当前页面状态。"""

    url: str
    hook_data: dict
    network_requests: list[dict]
    scripts: list[str]
    captcha_type: CaptchaType
    page_title: str
    dom_summary: str
    # 当前步截图保存路径（启用 enable_screenshot 时由 _observe 写入）
    screenshot_path: str = ""


@dataclass
class Action:
    """AI 决定的下一步动作。"""

    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        """从 LLM 返回的 dict 构造 Action。"""
        return cls(
            action_type=str(data.get("action_type", "wait")),
            params=dict(data.get("params") or {}),
            reasoning=str(data.get("reasoning") or ""),
        )


@dataclass
class ReverseAgentConfig:
    """JS 逆向 Agent 配置。"""

    max_steps: int = 20
    hooks: list[str] | None = None
    headless: bool = False
    wait_after_navigate: float = 3.0
    target_params: list[str] | None = None
    proxy: str | None = None
    os_name: str = "windows"
    # Planner：周期重规划间隔（步），None 表示禁用 Planner
    planner_interval: int | None = 5
    # LoopDetector：触发循环的重复次数阈值
    loop_threshold: int = 3
    # ContextCompressor：历史压缩阈值（步）
    max_history: int = 25
    # Judge：是否启用 done 二次验证
    enable_judge: bool = True
    # Judge：严格模式（缺任一目标参数直接判失败）
    judge_strict: bool = True
    # Recorder：是否启用成功路径编译
    enable_recorder: bool = True
    # Watchdog：步进心跳超时（秒），超过即视为卡死
    heartbeat_timeout: float = 120.0
    # Watchdog：崩溃重试次数
    max_retries: int = 2
    # DomPruner：DOM 焦点裁剪字符上限，0 表示禁用
    dom_prune_max_chars: int = 0
    # DomPruner：是否启用 LLM 重要性评分
    dom_prune_llm_rank: bool = False
    # Checkpoint：是否启用断点续跑
    enable_checkpoint: bool = False
    # Checkpoint：保存间隔（步）
    checkpoint_interval: int = 1
    # Checkpoint：滚动保留数量
    checkpoint_keep: int = 5
    # Confidence：动作置信度阈值，低于此值触发 fallback（0-1）
    min_confidence: float = 0.4
    # Confidence：是否启用 LLM 评分
    confidence_llm_score: bool = False
    # Guard：是否启用危险动作护栏
    enable_guard: bool = True
    # Guard：允许导航的域名白名单（None 不限制）
    allowed_domains: list[str] | None = None
    # Screenshot：是否在每步观察和错误时保存页面截图（PNG）
    enable_screenshot: bool = True
    # Humanize：是否启用人类化输入轨迹模拟（click 先 hover 再点击、type 逐字符随机延迟）
    humanize_input: bool = True
    # ImageCaptcha：是否启用图片验证码自动识别（OCR/滑块/点选），需 provider 支持 vision
    enable_image_captcha: bool = True
    # 外部停止回调：每步循环顶部调用，返回 True 时中断循环并把结果状态标为 stopped。
    # 供 app 侧在"收尾/取消"阶段接线；None 表示不启用（默认行为不变）。
    should_stop: Callable[[], bool] | None = None


# ---------------------------------------------------------------------------
# 常量与通用纯函数
# ---------------------------------------------------------------------------

# 拉取 JS 源码用的默认 UA
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 每个任务最多保留的截图数量（超出按文件名清理最旧的）
MAX_SCREENSHOTS_PER_TASK = 50


def js_str(value: str) -> str:
    """把字符串转成 JS 双引号字符串字面量，用于安全注入到 evaluate 表达式。

    对反斜杠、双引号、换行等做转义，避免 selector / 文本中包含特殊字符时
    破坏 JS 字符串结构或被注入攻击。
    """
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def safe_page_url(page: Any) -> str:
    """读取 page.url，任何异常都返回空串（page 可能是 mock 或已关闭）。"""
    try:
        return page.url
    except Exception:
        return ""


def sanitize_filename_component(value: str) -> str:
    """清理文件名组件，防止路径穿越（task_id 可能来自外部输入）。"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


def rotate_screenshots(
    out_dir: Path,
    task_prefix: str,
    *,
    max_keep: int = MAX_SCREENSHOTS_PER_TASK,
) -> None:
    """按任务前缀滚动保留最近 N 张截图，防止磁盘无限增长。"""
    try:
        files = sorted(out_dir.glob(f"{task_prefix}_step*.png"))
        if len(files) <= max_keep:
            return
        for old in files[: len(files) - max_keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


def is_safe_script_url(url: str, allowed_domains: list[str] | None) -> bool:
    """判断脚本 URL 是否允许服务端拉取（防 SSRF）。

    仅允许 http/https、非 localhost/内网 IP 的 host；配置了
    ``allowed_domains`` 白名单时还需命中白名单。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
    except ValueError:
        pass  # 域名，交由白名单与 DNS 解析方处理
    domains = allowed_domains
    if domains and domains != ["*"]:
        matched = False
        for allowed in domains:
            if allowed == "*":
                matched = True
                break
            if allowed.startswith("*."):
                suffix = allowed[2:]
                if host == suffix or host.endswith("." + suffix):
                    matched = True
                    break
            elif host == allowed:
                matched = True
                break
        if not matched:
            return False
    return True


def search_param_in_records(records: list[dict], param_name: str) -> str | None:
    """在 hook 记录中搜索目标参数，返回首个命中的值。

    依次在 headers / url query / body（JSON 或 form）中做大小写不敏感匹配。
    """
    if not records:
        return None
    target_lower = param_name.lower()
    for rec in records:
        # 1. headers 中匹配键名
        headers = rec.get("headers") or {}
        if isinstance(headers, dict):
            for k, v in headers.items():
                if target_lower in k.lower():
                    return str(v)
        # 2. url query 中匹配参数名
        url = rec.get("url") or ""
        if target_lower in url.lower():
            qs = parse_qs(urlparse(url).query)
            for k, v in qs.items():
                if target_lower in k.lower():
                    return v[0] if v else None
        # 3. body 中匹配（先 JSON 后 form）
        body = rec.get("body")
        if isinstance(body, str) and target_lower in body.lower():
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        if target_lower in k.lower():
                            return str(v)
            except json.JSONDecodeError:
                pass
            form = parse_qs(body)
            for k, v in form.items():
                if target_lower in k.lower():
                    return v[0] if v else None
    return None


def merge_hook_data(
    cached_records: list[dict],
    final_hook_data: dict[str, Any],
) -> dict[str, Any]:
    """合并缓存记录与最后一次观察的 hook 数据，避免结果 hook_data 几乎为空。"""
    fresh_records = final_hook_data.get("records", [])
    merged_records = list(cached_records) + [r for r in fresh_records if r not in cached_records]
    return {"records": merged_records, "count": len(merged_records)}


def fallback_action(target_params: list[str] | None) -> Action:
    """AI 分析失败时的降级动作。

    有目标参数时走纯 Hook 模式提取；无目标参数时降级为短等待后重试，
    避免空操作 extract 空转。
    """
    targets = target_params or []
    if targets:
        return Action(
            action_type="extract",
            params={"param_name": targets[0]},
            reasoning="AI 分析失败，降级为纯 Hook 模式提取",
        )
    return Action(
        action_type="wait",
        params={"seconds": 2.0},
        reasoning="AI 分析失败且未配置目标参数，等待后重试",
    )


def list_checkpoint_snapshots(
    *,
    enabled: bool,
    task_id: str,
    store: CheckpointStore,
) -> list[dict[str, Any]]:
    """读取已保存的 checkpoint 列表（step + path），供结果汇总使用。"""
    if not enabled or not task_id:
        return []
    try:
        paths = store.list_checkpoints(task_id)
        result: list[dict[str, Any]] = []
        for p in paths:
            step = 0
            name = p.stem  # 形如 step-0007
            if name.startswith("step-"):
                try:
                    step = int(name[5:])
                except ValueError:
                    pass
            result.append({"step": step, "path": str(p)})
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# JS 分析辅助
# ---------------------------------------------------------------------------


def pick_best_fragment(
    analyzer: JSAnalyzer,
    fragments: list[JSFragment],
    target_params: list[str],
) -> AnalysisResult | None:
    """按置信度与目标参数命中率选最优分析结果。"""
    if not fragments:
        return None
    target = target_params[0] if target_params else ""
    best_result: AnalysisResult | None = None
    best_score = 0.0
    for frag in fragments:
        try:
            result = analyzer.analyze_fragment(frag)
        except Exception:
            continue
        score = result.confidence
        if target and any(target in inp for inp in result.inputs):
            score += 0.5
        if score > best_score:
            best_score = score
            best_result = result
    return best_result


async def fetch_script_fragments(
    scripts: list[str],
    allowed_domains: list[str] | None,
    *,
    max_bytes: int,
) -> list[JSFragment]:
    """异步拉取脚本源码并构造 JSFragment 列表（httpx.AsyncClient，不阻塞事件循环）。

    超过 ``max_bytes``、状态码非 200、内容为空或 URL 未通过 SSRF 校验的
    脚本一律跳过；单个脚本拉取失败不影响其余脚本。
    """
    import httpx

    fragments: list[JSFragment] = []
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_UA},
    ) as client:
        for url in scripts[:10]:
            if not is_safe_script_url(url, allowed_domains):
                continue
            try:
                resp = await client.get(url)
                if not is_safe_script_url(str(resp.url), allowed_domains):
                    continue
                if resp.status_code != 200 or not resp.text:
                    continue
                if len(resp.content) > max_bytes:
                    continue
                text = resp.text
                fragments.append(
                    JSFragment(
                        source=text,
                        url=url,
                        size=len(text),
                        is_minified=len(text.splitlines()) < 5,
                    )
                )
            except Exception:
                continue
    return fragments
