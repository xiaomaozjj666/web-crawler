# JS 逆向 Agent

`ReverseAgent` 通过 **observe → think → act** 自主循环定位网页前端动态生成的加密参数
（如 `Anti-Content`、`X-Bogus`、`_signature` 等），并让 LLM 反混淆并重写签名算法。

## 工作流程

1. 启动 `CamoufoxFetcher`（默认 `headless=False`，便于人工介入）
2. 创建新的浏览器上下文，在导航前通过 `add_init_script` 注入 Hook 脚本
3. 进入循环：
   - **观察**：收集 Hook 捕获数据、网络请求、页面脚本、验证码检测、DOM 摘要
   - **思考**：把观察结果交给 DeepSeek-V4-Pro 决定下一步动作
   - **行动**：执行 AI 决定的动作（注入 Hook / 分析 JS / 等待 /
     提取参数 / 处理验证码 / 浏览器交互 / 多标签页 / 完成）
4. 达到 `max_steps` 或 AI 返回 `done` 时停止
5. 返回包含 `success`、`target_params_found`、`analysis`、
   `hook_data`、`steps`、`history` 的结果字典

## 入口

### MCP server（接入 Claude Desktop / Cursor）

```bash
pip install -e ".[mcp]"
export DEEPSEEK_API_KEY=sk-...        # Windows: set DEEPSEEK_API_KEY=...
web-crawler-mcp                       # 通过 stdio 跑 JSON-RPC
```

在 AI 客户端的 MCP 配置里注册：

```json
{
  "mcpServers": {
    "js-reverse": {
      "command": "web-crawler-mcp",
      "env": {"DEEPSEEK_API_KEY": "sk-..."}
    }
  }
}
```

`reverse_engineer_url` 工具一次调用返回完整运行时状态：
`analysis`、`compiled_script`、`judge_result`，外加
`last_confidence`、`checkpoints`、`screenshots`、`error_screenshot`。

### CLI（脚本与交互式 REPL）

```bash
web-crawler-reverse https://example.com --target-params anti_content sign
web-crawler-reverse analyze script.js            # 反混淆 JS 片段
web-crawler-reverse webpack bundle.js            # 抽 webpack 模块
web-crawler-reverse reimplement algo.js --language python
web-crawler-reverse capture https://example.com --wait 8
web-crawler-reverse interactive                  # REPL：输入 `tools` 列工具
```

### `run` 子命令（完整 Agent，不走 MCP）

`run` 直接构造 `ReverseAgent` 并调用 `run()`，暴露全部 guard /
checkpoint / screenshot 标志，可保存成功路径脚本到文件，并把完整 JSON
结果（`last_confidence`、`checkpoints`、`screenshots`、
`error_screenshot`）写到 stdout 或 `--output`：

```bash
web-crawler-reverse run \
  --url https://target.example.com \
  --task "提取 Anti-Content 签名参数" \
  --target-params anti_content,sign \
  --max-steps 20 --headless \
  --enable-checkpoint \
  --min-confidence 0.4 \
  --enable-screenshot \
  --save-script ./out/sign.py \
  --output ./out/result.json
```

### Python API

```python
from web_crawler import ReverseAgent, ReverseAgentConfig, DeepSeekProvider

config = ReverseAgentConfig(
    headless=True,
    hooks=["fetch_hook", "xhr_hook", "cookie_hook", "crypto_subtle_hook"],
    target_params=["anti_content", "sign"],
    max_steps=20,
    humanize_input=True,
    enable_screenshot=True,
    enable_checkpoint=True,
)
provider = DeepSeekProvider(model="deepseek-v4-pro")
agent = ReverseAgent(config=config, provider=provider)
try:
    result = agent.run("https://target.example.com", task="提取签名参数")
    print(result["success"], result["target_params_found"])
    print(result.get("compiled_script", ""))
finally:
    agent.close()
```

## 支持的动作

### 基础动作

| 动作 | 说明 | 关键参数 |
| --- | --- | --- |
| `navigate` | 导航到新 URL | `url` |
| `inject_hook` | 注入新 Hook | `hooks`: `["fetch_hook", ...]` |
| `analyze_js` | 分析捕获的 JS | `script_urls`, `target_params` |
| `wait` | 等待一段时间 | `seconds` |
| `extract` | 从 Hook 数据提取目标参数 | `param_name` |
| `solve_captcha` | 处理验证码 | （无） |
| `done` | 任务完成 | `success`, `summary` |

### 浏览器交互动作（6 个）

| 动作 | 说明 | 关键参数 |
| --- | --- | --- |
| `click` | 点击元素 | `selector`, `button` |
| `type` | 输入文本（默认先清空） | `selector`, `text`, `clear` |
| `scroll` | 滚动页面或元素 | `x`, `y`, `selector`（可选） |
| `press` | 按键 | `key`, `selector`（可选） |
| `hover` | 鼠标悬停 | `selector` |
| `select_option` | 下拉选择 | `selector`, `value` |

所有交互动作统一 10s 超时；超时抛 `TimeoutError` 由外层 `_act` 捕获。
失败时自动保存截图（`enable_screenshot=True`），并发 `browser.action` 事件供 UI 订阅。

### 多标签页动作（3 个）

| 动作 | 说明 | 关键参数 |
| --- | --- | --- |
| `new_tab` | 新建标签页并导航 | `url`, `name`（可选，默认 `tab_N`） |
| `switch_tab` | 切换到指定标签页 | `name` 或 `index` |
| `close_tab` | 关闭指定标签页 | `name` |

`new_tab` 时主页面自动以 `"main"` 为键登记到内部 `_tabs` 映射；
`close_tab` 关闭的是当前活跃标签时，`self._page` 自动回退到 `main`。

## 人类化输入轨迹

`ReverseAgentConfig.humanize_input=True`（默认开启）时：

- **`click`**：先 `page.hover(selector)` 移动鼠标，再 `time.sleep(uniform(0.05, 0.2))`，最后 `page.click`
- **`type`**：先 `page.focus(selector)`，再 `time.sleep(uniform(0.1, 0.3))`，
  最后用 `page.type(text, delay=randint(30, 150))` 逐键输入

某些 mock 对象不支持 `delay` 参数，会自动 `TypeError` 退化为不带 `delay` 的调用。
异步路径 `_humanize_click_async` / `_humanize_type_async` 与同步等价。

## 主流 Agent 能力对齐

下列能力均通过 `ReverseAgentConfig` 字段单独开关，全部对齐生产级 Agent 框架
（browser-use / Skyvern / PentAGI / LangGraph）：

| 能力 | 配置字段 | 默认 | 说明 |
| --- | --- | --- | --- |
| DOM 焦点裁剪 | `dom_prune_max_chars` | `0`（禁用） | 规则 + LLM 重排，仅保留加密相关元素 |
| 断点续跑 | `enable_checkpoint` | `False` | 步末状态持久化；崩溃后从最近 checkpoint 恢复 |
| 动作置信度 | `min_confidence` | `0.4` | 规则 + LLM 双路径评分；低置信触发 fallback |
| 动作护栏 | `enable_guard` | `True` | 域名白名单，拦截 localhost/非 HTTPS/跨域/危险脚本 |
| Planner/Actor | `planner_interval` | `5` | 高层子目标规划 + 周期重规划 |
| 循环检测 | `loop_threshold` | `3` | 页面状态指纹；重复状态自动重规划 |
| 上下文压缩 | `max_history` | `25` | 历史滚动摘要；超出 `max_history` 时自动压缩 |
| 任务裁决 | `enable_judge` | `True` | 独立 LLM 验证 `done`，防幻觉成功 |
| 成功路径录制 | `enable_recorder` | `True` | 把成功 trace 编译为确定性 Python 脚本 |
| 截图捕获 | `enable_screenshot` | `True` | 每步观察 + 错误路径保存 PNG |
| 人类化输入 | `humanize_input` | `True` | click 先 hover、type 逐键随机延迟 |

## 错误处理

- **浏览器崩溃**：`CrashRecovery` 尝试重启一次
- **Hook 注入失败**：记录到 history 后继续
- **AI 分析失败**：降级为纯 Hook 模式（仅靠 Hook 数据推进循环）
- **截图失败**：吞掉异常，返回空路径，主循环不中断
- **超时**：所有交互动作 10s 超时，超时由外层 `_act` 捕获并记录

## 合规说明

Agent 仅识别验证码挑战并模拟正常用户交互（OCR / 滑块 / 点选图片挑战由
`ImageCaptchaSolver` 自动识别），**不**伪造登录凭证、**不**绕过付费墙。
当页面返回 401/403 或出现无法处理的挑战时，Agent 会停止并返回"转人工处理"状态。

## 单模型策略

`ReverseAgent` 与所有子组件（Planner / Actor / Judge / DomPruner /
ConfidenceScorer / JSAnalyzer）共享同一个 `DeepSeekProvider(model="deepseek-v4-pro")` 实例。
不存在按组件路由模型、不存在 LLM-as-judge 重排、不存在能力协商切换 provider。
