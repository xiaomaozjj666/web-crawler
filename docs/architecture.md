# 架构说明

`web-crawler` 采用分层架构：核心库（`src/web_crawler/`）+ 应用层（`app/`）。
核心库对外暴露统一公开 API，所有重依赖（`playwright` / `curl_cffi` / `camoufox`）
均通过 `__getattr__` 懒加载，`import web_crawler` 不会强制加载任何可选依赖。

## 模块树

```
src/web_crawler/          # 核心库
  _types.py               # TextHandler / Attrs / ResultList
  compat.py               # 可选依赖探测（graceful degradation）
  response.py             # 统一 Response（selector helpers, meta, urljoin）
  crawler.py              # 同域异步爬虫（robots.txt 感知）
  parser/
    selector.py           # Selector + Adaptors（CSS/XPath/text/similarity/adaptive）
    adaptive.py           # compute_fingerprint + similarity_score + best_match
    storage.py            # AdaptiveStorage（线程安全 SQLite 存指纹）
    visual.py             # VisualExtractor（PixelRAG 风格截图瓦片 VLM）
  fetchers/
    _base.py              # BaseFetcher（共享配置 + response 构建）
    fetcher.py            # Fetcher + AsyncFetcher（curl_cffi TLS 隐身, httpx 兜底, JA4 定制）
    dynamic.py            # DynamicFetcher（Playwright JS 渲染）
    stealthy.py           # StealthyFetcher（反爬 / Cloudflare）
    camoufox.py           # CamoufoxFetcher（抗指纹 Firefox）
    proxy.py              # ProxyPool（轮换 + 冷却）
  spider/
    spider.py             # Spider + Request + SpiderStats（暂停/续跑）
  ai/                     # AI 辅助抓取 + JS 逆向套件
    llm.py                # LLMProvider（OpenAI 兼容，默认 DeepSeek）
    extractor.py          # AIExtractor（CSS 选择器生成 + 自愈）
    agent.py              # AIScrapeAgent（robots 感知礼貌爬虫）
    hooks.py              # JS Hook 库（fetch/XHR/cookie/crypto/webpack/console）
    analyzer.py           # JSAnalyzer（webpack 模块抽取 + AI 反混淆）
    captcha.py            # CaptchaManager（检测 + 仅人类化处理）
    image_captcha.py     # ImageCaptchaSolver（OCR / slider 缺口 / click 点选，LLM Vision + ddddocr/Pillow 降级）
    reverse_agent.py      # ReverseAgent（observe→think→act 循环）
    vision.py             # VisionObserver（Vision-LLM 截图感知双模态）
    planner.py            # Planner/Actor 双脑分离 + 周期重规划
    loop.py               # LoopDetector + ContextCompressor（循环检测 + 历史压缩）
    judge.py              # TaskJudge（done 二次验证，防止 LLM 幻觉）
    watchdog.py           # EventBus + Heartbeat + CrashRecovery（崩溃自愈）
    recorder.py           # RunRecorder（成功路径编译为确定性脚本）
    schema.py             # SchemaValidator（结构化抽取 Pydantic 校验）
    dom_pruner.py         # DomPruner（DOM 焦点裁剪，Skyvern/browser-use 风格）
    checkpoint.py         # CheckpointManager（断点续跑 + 状态持久化）
    budget.py             # BudgetTracker（Token 预算管理，单步/全局/单次）
    confidence.py         # ConfidenceScorer（动作置信度评分，规则 + LLM 双路径）
    guardrails.py         # ActionGuard（危险动作护栏，白名单 + 跨域拦截）
  pentest/                # 轻量渗透辅助工具集（纯 Python，无外部命令依赖）
    port_scanner.py       # PortScanner（TCP connect 扫描，TOP-100 端口）
    dir_bruter.py         # DirBruter（目录/文件路径爆破）
    subdomain.py          # SubdomainEnumerator（DNS 子域名枚举）
    vuln_scanner.py       # VulnScanner（SQLi/XSS/路径穿越规则检测）
    header_check.py       # HeaderChecker（8 项 HTTP 安全头检测 + A-F 评级）
    report.py             # PentestReport（聚合所有结果 + summary/to_dict/to_json）
  mcp/                    # MCP server + CLI
    server.py             # ReverseMCPServer（JSON-RPC over stdio）
    cli.py                # web-crawler-reverse 命令行
  py.typed                # PEP 561 类型标记
app/                      # 应用层
  crawler.py              # 资源下载器（并发、续传、去重、UI 驱动）
  ui.py                   # 本地 Web UI（SSE 实时推送）
tests/                    # pytest 测试套件
benchmarks.py             # 解析器/fetcher 微基准 + 内置基线 + 回归检测
mkdocs.yml                # API 文档站点配置
demo.py / demo.bat        # 交互式使用 demo
```

## 各层职责

### 核心库 `src/web_crawler/`

#### 解析层 `parser/`

- **`Selector`** 基于 `lxml` + `cssselect`，对齐 Scrapling `Adaptor` API：
  - `css` / `css_first` / `xpath` / `xpath_first` / `re` / `re_first`
  - `find_by_regex` / `get_all_text` / `prettify`
  - 完整 DOM 遍历：`parent` / `children` / `siblings` / `next` / `previous` / `path`
- **`AdaptiveStorage`** 线程安全 SQLite，持久化元素指纹
- **`compute_fingerprint`** 把元素结构特征编码为可比较向量
- **`similarity_score`** 比较两指纹相似度，用于站点改版后重定位

#### 抓取层 `fetchers/`

- **`Fetcher`** 主力 HTTP fetcher：`curl_cffi` 重放真实浏览器 TLS/JA3 指纹与 HTTP/2 帧序
  - 支持 `impersonate="chrome131"` 等浏览器预设
  - **JA4 指纹定制**：`ja4_fingerprint` 参数透传到 `curl_cffi` 的 `ja3` 参数，
    覆盖预设的 TLS 扩展顺序，做细粒度 TLS 指纹定制
  - `curl_cffi` 缺失时自动降级到 `httpx`（带 warning，无指纹能力）
- **`AsyncFetcher`** 纯异步 fetcher，与 `Fetcher` 共享配置但只暴露 async 方法
- **`DynamicFetcher`** Playwright/Chromium 渲染，按选择器等待，按资源类型屏蔽
- **`StealthyFetcher`** 反爬专用：注入指纹补丁 JS、人类化鼠标/滚动，处理 Cloudflare
- **`CamoufoxFetcher`** 抗指纹 Firefox（Camoufox + Playwright），用于 JS 逆向 Agent
- **`ProxyPool`** 轮询/随机代理池，失败冷却

#### Spider 层 `spider/`

- **`Spider`** 回调驱动爬虫框架：
  - 优先级调度、域名过滤、URL 去重
  - JSON 状态序列化，支持暂停/续跑
  - `Request` 封装 URL + callback + meta

#### AI 层 `ai/`

##### LLM 基础

- **`LLMProvider`** Protocol，OpenAI 兼容 chat-completions 接口
- **`DeepSeekProvider`** 默认 provider，指向 DeepSeek-V4-Pro
- **`OpenAIProvider`** OpenAI 官方预置
- 仅依赖 `httpx`，无额外 SDK

##### AI 抽取

- **`AIExtractor`** 自然语言 schema → CSS 选择器，带自愈
- **`AIScrapeAgent`** fetch + extract 编排，遵守 robots.txt、429/503 退避

##### JS 逆向 Agent

- **`ReverseAgent`** observe→think→act 主循环
- **`HookLibrary`** 6 个 JS Hook：fetch / XHR / cookie / `crypto.subtle` / webpack / console
- **`JSAnalyzer`** webpack 模块抽取、签名流追踪、AI 反混淆与算法重写
- **`CaptchaManager`** 检测 Turnstile / hCaptcha / reCAPTCHA v2&v3 / 极验 GeeTest；仅模拟正常用户
- **`VisionObserver`** Vision-LLM 截图感知，处理 DOM 混淆 / Canvas 渲染页面

##### 主流 Agent 能力对齐

下列模块对齐 browser-use / Skyvern / PentAGI / LangGraph 等生产级 Agent 框架：
均通过 `ReverseAgentConfig` 字段单独开关。

| 能力 | 模块 | 作用 |
| --- | --- | --- |
| DOM 焦点裁剪 | `DomPruner` | 规则 + LLM 重排，仅保留加密相关元素（≈80% token 削减） |
| 断点续跑 | `CheckpointManager` | 步末状态持久化；崩溃/中断后从最近 checkpoint 恢复 |
| Token 预算 | `BudgetTracker` | 单步/单次/全局上限，COMPRESS/DOWNGRADE/STOP 策略 |
| 动作置信度 | `ConfidenceScorer` | 规则 + LLM 双路径评分；低置信触发 fallback |
| 动作护栏 | `ActionGuard` | 域名白名单，拦截 localhost/非 HTTPS/跨域/危险脚本 |
| Planner/Actor 分离 | `Planner` | 高层子目标规划 + 周期重规划 |
| 循环检测 | `LoopDetector` | 页面状态指纹；重复状态自动重规划 |
| 上下文压缩 | `ContextCompressor` | 历史滚动摘要；预算溢出时 `force_compress` |
| 任务裁决 | `TaskJudge` | 独立 LLM 验证 `done`，防幻觉成功 |
| 成功路径录制 | `RunRecorder` | 把成功 trace 编译为确定性 Python 脚本 |
| 事件总线 + 看门狗 | `watchdog` | 发布/订阅事件、心跳卡死检测、浏览器崩溃自愈 |
| 结构化 schema | `SchemaValidator` | Pydantic 结果校验 + 自动修复提示 |

##### 浏览器交互与多标签页

- 6 个真实 Playwright 动作：`click` / `type` / `scroll` / `press` / `hover` / `select_option`
- 3 个多标签页动作：`new_tab` / `switch_tab` / `close_tab`，维护 `name → page` 映射
- **人类化输入轨迹**：`humanize_input=True` 时
  - `click` 先 `hover` 移动鼠标，随机延迟 50–200 ms 后再点击
  - `type` 先 `focus`，思考停顿 100–300 ms，再用 `delay=30–150ms` 逐键输入

### 应用层 `app/`

- **`crawler.py`** 并发资源下载器：续传、去重、sitemap 发现
- **`ui.py`** 本地 Web UI，支持 [资源采集器] 与 [JS 逆向 Agent] 双 Tab，
  SSE 实时推送 `/reverse/stream`，截图画廊，任务模板

## 单模型策略

`ReverseAgent` 与所有子组件共享同一个 `DeepSeekProvider(model="deepseek-v4-pro")` 实例。
不存在按组件路由模型、不存在 LLM-as-judge 重排、不存在能力协商切换 provider。
`BudgetPolicy.DOWNGRADE` 在单模型策略下为 no-op；超预算用 `COMPRESS`（默认）或 `STOP`。

## 数据流

```
                ┌─────────────────────────────────────────┐
                │              ReverseAgent.run            │
                │  ┌─────────────────────────────────┐    │
                │  │   1. observe(page) → Observation │   │
                │  │      - hook_data (JS 注入捕获)    │   │
                │  │      - network_requests           │   │
                │  │      - scripts / dom_summary      │   │
                │  └────────────┬────────────────────┘    │
                │               ▼                          │
                │  ┌─────────────────────────────────┐    │
                │  │   2. think(observation) → Action │   │
                │  │      LLM 决定下一步动作           │   │
                │  │      Guard 校验 / Confidence 评分 │   │
                │  └────────────┬────────────────────┘    │
                │               ▼                          │
                │  ┌─────────────────────────────────┐    │
                │  │   3. act(page, action)           │   │
                │  │      - click/type/scroll/...     │   │
                │  │      - new_tab/switch_tab/...    │   │
                │  │      - inject_hook/analyze_js    │   │
                │  └────────────┬────────────────────┘    │
                │               ▼                          │
                │   LoopDetector / Judge / Budget          │
                │   循环到 done 或 max_steps               │
                └─────────────────────────────────────────┘
```
