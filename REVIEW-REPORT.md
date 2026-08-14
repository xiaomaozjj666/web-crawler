# Web-Crawler 全量审查测试报告

- 日期：2026-08（Python 3.14.6 / Windows，commit 3c815cc）
- 范围：`src/web_crawler` 全部 53 个源文件 + `app/` 3 个源文件 + 全部测试
- 方法：全量 pytest + 覆盖率、ruff、mypy、benchmark 回归、git 密钥历史扫描、6 路并行深度代码审查（子代理交叉验证，关键论断已实测复核）

---

## 一、测试与静态检查结果

| 项目 | 结果 |
|---|---|
| pytest 全量 | ✅ **2521 passed, 3 skipped**（33.45s，含 slow 测试） |
| 覆盖率 | ✅ **99%**（9818 语句，仅 24 未覆盖；`app` 99% / `web_crawler` 99%） |
| ruff lint | ✅ 全绿（src + app + tests） |
| mypy | ⚠️ `src/web_crawler` 通过（CI 门禁范围）；**`app/` 有 20 个错误**（CI 不检查 app，见缺口 #1） |
| benchmarks --check-regression | ✅ 通过（全部指标低于 1.2× 基线） |
| mkdocs build --strict | ⏭️ 本地未装 docs 依赖，未验证（CI 会装） |
| 密钥扫描 | ✅ `.env` 未被 git 跟踪，git 历史无 `sk-` 密钥泄漏；`.gitignore` 覆盖完整（128 个跟踪文件，无产物误入库） |

**3 个 skip 原因**：`Crypto.Cipher` 未安装（pycryptodome 可选依赖）；2 个 Windows 下 Playwright e2e 限制（CI 为 ubuntu，会执行）。

**测试警告（13 个）**：`ResourceWarning: unclosed database`（sqlite3 连接）来自 `app/db.py` 的线程级连接从不显式关闭（见 app 模块发现），以及 `dynamic.py` 的 AsyncMock 协程未 await（已在 pyproject 中声明为 mock 限制）。

---

## 二、审查发现统计

**1 🔴 Critical / 21 🟠 High / 34 🟡 Medium / 27 🔵 Low / 14 💡 Suggestion（共 97 条）**

| 模块 | 🔴 | 🟠 | 🟡 | 🔵 | 💡 |
|---|---|---|---|---|---|
| 核心库（parser/spider/crawler） | 0 | 2 | 3 | 6 | 3 |
| fetchers | 1 | 1 | 4 | 6 | 2 |
| AI 逆代理解析循环 | 0 | 6 | 8 | 4 | 2 |
| AI LLM/验证码/分析 | 0 | 2 | 8 | 4 | 2 |
| MCP / pentest | 0 | 4 | 6 | 3 | 2 |
| app 应用层 | 0 | 6 | 5 | 4 | 3 |

---

## 三、🔴 Critical

```
[🔴 Critical] src/web_crawler/fetchers/fetcher.py:501（get/request）与 dynamic.py:210（fetch）— 无任何 URL scheme 校验，可读任意本地文件（SSRF）
Cause: Fetcher 对 URL 不做 scheme/host 校验，直接交给 curl_cffi（libcurl）或 Playwright。
libcurl 默认启用 FILE 协议；默认 follow_redirects=True 且无 max_redirects 上限，重定向可被引向内网。
爬虫 URL 多来自页面提取的链接（攻击者可控），是典型 SSRF 面。
【已实测复核】Fetcher().get('file:///C:/Windows/win.ini') 成功返回 92 字节本地文件内容。
Fix: ① 请求前强制 scheme 白名单（http/https only，拒绝 file/ftp/data/gopher）；
② 提供可选 host 校验（拒绝私网/环回/链路本地 IP，防 DNS rebinding 需解析后二次校验）；
③ 重定向逐跳重新校验 + 暴露 max_redirects；④ DynamicFetcher 的 _render_page 入口同样校验。
```

---

## 四、🟠 High（21 条）

### 4.1 核心库（2）

```
[High] spider.py:195-244 — _dump_state 未序列化 Request.body/retries，暂停恢复后 POST/PUT 静默变成无 body 的 GET
Fix: 用 base64/latin-1 持久化 body，_load_state 还原，并持久化 retries。

[High] spider.py:327-332/435-440 — 状态文件生命周期未与 pause/resume 挂钩：max_requests 提前结束会向 CWD 写状态文件；全新运行完成时 path.unlink() 会删除用户既有的暂停状态文件
Fix: 仅 _paused 或显式传 state_file/resume=True 时 dump；只删除本次运行自己创建的文件。
```

### 4.2 fetchers（1）

```
[High] fetcher.py:180,196-201 — httpx 回退路径默认 http2=True 且未装 h2 时首次请求直接抛 ImportError，"透明降级"不成立（已实测：httpx 0.28.1 无 h2 → Client(http2=True) → ImportError）
Fix: try/except ImportError 降级为 http2=False 并警告，或先用 find_spec("h2") 探测。
```

### 4.3 AI 逆代理解析循环（6）

```
[High] reverse_agent.py:505-512/876-888/2051-2062 — 崩溃恢复永远无效：循环持有已关闭的旧 page 局部变量，_try_recover_page 只更新 self._page；恢复后继续在关闭页上操作，每步烧一次浏览器重启
Fix: _try_recover_page 返回 (bool, page)，循环内重新绑定；或循环统一经 self._page 取页。

[High] reverse_agent.py:1690-1715/1743-1756/1772-1799 — 多标签页功能整体失效：new_tab/switch_tab/close_tab 只改 self._page，循环仍操作旧页；close_tab 后循环用已关闭页
Fix: 循环体统一改用 self._page；补"new_tab → 下一轮 observe 是新页"的循环级集成测试。

[High] checkpoint.py:233,254-258 + reverse_agent.py:359-365,452 — 断点续跑默认永远不生效：task_id 为空时 load_latest 直接返回 None；跨进程 id 用 time.time() 生成必不相同
Fix: 用 url 稳定哈希做 task_id（去掉 time.time()），run() 开头赋值；补真实存储层 resume 集成测试。

[High] recorder.py:321 — extract 成功后编译产物必然产生 Python 语法错误（{param_name!r} 渲染出 'sign' 嵌套单引号），replay 脚本永远跑不起来；测试只做子串断言从不 compile
Fix: 用 _py_string_literal 代替 {param_name!r}；加 compile(产物) 端到端测试。

[High] guardrails.py:280-346 + reverse_agent.py:1690-1715 — 护栏可被 new_tab 绕过：URL 检查规则只匹配 action_type=="navigate"，new_tab 同样执行 page.goto 却不检查
Fix: URL 校验抽公共函数，navigate 与 new_tab 统一调用；补绕过用例。

[High] reverse_agent.py:1873-1905 — analyze_js 服务端拉取任意 script_urls（页面可控 + LLM 输出）：SSRF + 内网响应内容直接发给 LLM provider（数据外带）
Fix: analyze_js URL 过同源/域名白名单 + 内网拦截；内容大小上限；拒绝重定向到内网。
```

### 4.4 AI LLM/验证码/分析（2）

```
[High] judge.py:134/180 — verified 字段用 bool() 强转，LLM 输出字符串 "false" 被判为 True，任务完成判定被静默翻转（verified 是唯一安全闸门）
Fix: 严格布尔解析（raw is True or str 小写 == "true"）；补回归测试。

[High] extractor.py:168-175/189-212 — LLM 生成的非法 CSS 让 extract() 直接崩溃（SelectorSyntaxError 无 try/except），一条坏选择器中断整个抽取
Fix: _apply 按字段 try/except，失败记为 missing 走自愈；补非法 CSS 测试。
```

### 4.5 MCP / pentest（4）

```
[High] mcp/server.py:908-1006 — pentest_recon 无授权门禁：仅描述文字声明合规，零 enforcement（无白名单/私网拦截/确认/限速），任何 MCP 客户端或提示注入诱导的 LLM 可对任意目标扫描
Fix: 默认拒绝私网/环回/云元数据地址，强制 authorization_confirmed=true 参数。

[High] mcp/server.py:163-177（调用点 711/746/842/1017/1038）— URL 工具无 scheme/主机校验，page.goto 支持 file:// → 浏览器 SSRF + 本地文件暴露
Fix: 入口统一校验仅放行 http/https，默认拒绝私网/环回与含 userinfo 的 URL。

[High] mcp/server.py:992-1003 — Windows 下 pentest 超时形同虚设：ThreadPoolExecutor with 退出 shutdown(wait=True) 阻塞等扫描跑完，超时后扫描继续且 MCP 服务器被挂起；DNS 无超时
Fix: shutdown(wait=False, cancel_futures=True) + DNS/连接真实超时；测试补耗时断言。

[High] mcp/server.py:1064-1079 — SDK 路径 async handler 里同步调用 handle_tool，浏览器/LLM/300s 扫描阻塞事件循环，并发工具调用全被冻结
Fix: await asyncio.to_thread(...)，并为共享 sync Playwright fetcher 加锁。
```

### 4.6 app 应用层（6）

```
[High] app/crawler.py:2737-2743,2879 — 主线程取消路径 break 后仍执行 post-processing 并返回 0，取消退出码不一致且不中止工作
Fix: break 后跳过后处理统一返回取消码；补测试。

[High] app/crawler.py:2465-2470,2541-2577 — 页面扫描阶段取消后仍进入并发下载阶段（阶段间无 should_stop 守卫）
Fix: 每个阶段入口统一检查 should_stop 并短路。

[High] app/ui.py:1816 + crawler.py:61-66 — UI 日志面板捕获不到爬虫日志：logging.basicConfig 在 import 时绑定原始 stderr，run_job 的 redirect_stdout/stderr 对 logging 无效，且并发任务下进程级 redirect 会串线
Fix: 给 crawler._log 挂自定义 logging.Handler 转发到 job.append，废弃进程级 redirect。

[High] app/ui.py:1975-1980,2458-2467 — Reverse Agent "停止"按钮是假的：stop_event 未传入 agent，src 侧无任何 stop 检查，会跑完所有步骤才标 cancelled
Fix: ReverseAgentConfig 增加 stop_event/should_stop 回调并在每步循环检查。

[High] app/crawler.py:503-525,718-721 — SSRF 防护可被重定向绕过：_is_safe_hostname 只校验初始 URL，urllib 自动跟随的重定向目标无二次校验，DNS 重绑定也放行
Fix: 自定义 HTTPRedirectHandler 对每次重定向后 URL 重新校验（stealth 路径同样处理）。

[High] app/ui.py:2372-2418 — 本地 Web API 无鉴权/CSRF：表单 POST 为简单请求可跨站触发 /run、DELETE /jobs/<id>、/open-output（os.startfile 可启动可执行文件）；Cookie/Authorization 明文写入 tasks.config 并经 GET /jobs/<id> 回传
Fix: 校验 Origin/CSRF token、强制 127.0.0.1、config 入库前剔除 header 且 API 不回传、/open-output 限定白名单目录。
```

---

## 五、🟡 Medium（34 条）

### 核心库（3）
- `spider.py:217` — `_dump_state` 无 `default=`，meta 含 bytes 等不可序列化内容时暂停路径 TypeError 崩溃
- `crawler.py:133-147` — 链接去重用原始绝对 URL：#fragment/大小写/默认端口差异导致重复抓取，ftp:// 等非 http(s) 链接被放行
- `selector.py:530-534 + adaptive.py:130-145` — 自适应重定位对每个元素算完整指纹（含子树 itertext），大页面 O(N²)；已持久化的 tag 字段未用于预筛

### fetchers（4）
- `fetcher.py:455-460,330-335` — 未知 kwargs（auth/cookies/max_redirects）被静默丢弃，调用方"成功"但参数不生效
- `proxy.py:77-90 + fetcher.py:365-367,490-492` — 代理失败计数累计永不归零；**mark_success 在 src 下零调用**，连接错误既不 mark_failed 也不轮换，死代理被原地重试
- `dynamic.py:519-529` — close() 用 new_event_loop 清理 async 句柄必然失败（跨事件循环），chromium 进程静默泄漏；测试的 AsyncMock 不感知 loop 掩盖了问题
- `camoufox.py:156-191` — screenshot_tiles 继承 dynamic 的 new_context 覆盖 UA/locale/viewport，与 Camoufox 自身"保留指纹"策略冲突，截图路径破坏隐身

### AI 逆代理解析循环（8）
- `watchdog.py:165-178` — Heartbeat 卡死检测从未被调用（check_stall 无调用点），LLM 卡死无检测
- `planner.py:79-84` — Plan.advance() 无任何调用点，子目标永不推进，"双脑分离"只剩 prompt 装饰
- `reverse_agent.py:2030,1159` — _network_log 跨步累积永不清理，SPA 长会话内存 O(请求数)，且 network_count 漂移使循环检测失准
- `reverse_agent.py:1379,1391,1275-1278` — arun 关键路径内嵌同步阻塞 IO（analyze_js 最多 150s 阻塞事件循环、同步 captcha、同步 chat 回退）
- `reverse_agent.py:1294-1301,1328-1331` — 无 target_params 时 fallback 是空操作，LLM 全挂时每步空转烧满 max_steps
- `reverse_agent.py:1361,1419` — 未知 action_type 静默返回 None 无审计；confidence._VALID_ACTIONS 缺少 prompt 承诺的 new_tab/switch_tab/close_tab
- `reverse_agent.py:73-117,2137-2186` — 提示注入面：页面可控的 hook header 值无长度上限原样进 prompt，system prompt 无对抗性约束
- `guardrails.py:180-202` — CONFIRM 规则无 on_confirm 时默认放行（与文档"默认拒绝"矛盾）；async on_confirm 永不被 await

### AI LLM/验证码/分析（8）
- `llm.py:270-306 + image_captcha.py:55` — LLM 调用无重试/退避；`max_retries` 是死配置（定义后从未引用）
- `analyzer.py:360` — inputs 被模型返回成字符串时拆成单字符列表
- `llm.py:479 vs 458-460` — AnthropicProvider 端点与自身 docstring 不一致（OpenAI 兼容路径拼接错误）
- `agent.py:107-121,227-231` — robots.txt 用重型 fetcher 拉取（render=True 时启动整个浏览器），且绕过 _throttle 限速
- `agent.py:218-225` — Retry-After 无上限，恶意站点可让爬虫休眠数天
- `judge.py:194-236` 等 — 页面不可信内容直接拼入 LLM prompt，无转义/定界，影响 verified 判定
- `captcha.py:324-327` — 未注入 image_solver 时极验仍随机拖拽，与"不尝试绕过、交人工"模块契约矛盾
- `captcha.py:322-329` — 极验缺口偏移坐标系与拖拽起点不一致（按钮半宽被系统性加进距离，过冲）

### MCP / pentest（6）
- `server.py:604-632,1214-1242` — 两条传输路径均无工具参数校验（缺参 KeyError、类型错误晦涩）
- `server.py:759-818,847-906,946-948,1008-1020` — 无输入大小/数量/范围限制（code/base64/wait_time/ports/max_steps）
- `server.py:631-632` — 通用异常把完整 traceback 返回给客户端，泄露绝对路径
- `header_check.py:46-53 / report.py:54-64 / server.py:929-935,1006` — 报告回传原始 Set-Cookie/Authorization 值；URL userinfo 凭据进 base_url
- `server.py:591-600,643,667-689` — progress 通知机制是死代码（SDK 路径从不发送）
- `server.py:653-662` — run_config 手工克隆仅复制 6 个字段静默丢弃其余配置；MCP 路径默认弹可见浏览器（headless=False 与 fetcher 不一致）

### app（5）
- `ui.py:2400-2418` — 暂停在后处理/退避 sleep 阶段无效；/pause、/cancel 直接写 DB 可能覆盖已完成任务终态
- `crawler.py:992-1006,1182-1195,2786-2839` — 后处理无顶层 try，rewrite_html 越界 ValueError 使任务标 error 且残留半成品
- `crawler.py:683-697,729` — 非 resume 下载整文件驻留内存，stealth 路径 max_bytes 在整包缓冲后才校验
- `crawler.py:2470,2528-2539` — page_queue.pop(0) O(n) + 每扫一页全量重写状态文件，大 max_pages 二次方
- `ui.py:1764-1785,1894-1914` — 表单无服务端校验：workers=100000 可线程爆炸、非法数字 SystemExit 未捕获

---

## 六、🔵 Low（27 条，要点）

- **核心库**：`ensure_list` 对生成器行为与 docstring 不符；robots.txt single-flight 缺失；visual.py sync/async 重复 25 行；visual base_url 未强制 https（http 下 API key 明文）；spider allowed() 用含端口 netloc 比较误拒 `example.com:8080`；AdaptiveStorage.close() 非幂等、模块级默认存储从不关闭
- **fetchers**：httpx 回退+代理时每请求新建 client 零复用；screenshot_tiles 无 tiles 上限（超长页数百张截图）；_parse_proxy 对无 scheme/IPv6/百分号凭据解析错误；Fetcher.close() 丢弃未关 async 会话；共享 curl Session 无并发保护；`ja4_fingerprint` 参数实际透传 ja3（命名不一致）
- **逆代理解析循环**：`_resume_from` 跨 run 泄漏；run/arun 约 360 行复制且状态重置不一致；checkpoint step-10000 字典序排序错乱；截图从不清理（跨任务累积）
- **AI LLM/验证码**：4 处复制贪婪 JSON 正则（`\{.*\}` 吞掉尾部花括号）；judge/dom_pruner/image_captcha sync/async 大段重复（本次 bool 强转 bug 两处都有）；_normalize_messages 缺 role 时 KeyError；多模态 content 列表未归一化（image_captcha join 时 AttributeError）；extractor 空字符串字段永久判 missing 自愈白跑；captcha hcaptcha/recaptcha 分支 query_selector 无异常保护
- **MCP/pentest**：port_scanner 多地址族 last-writer-wins；XSS payload 条件不匹配导致 `javascript:` 永远不可达；未执行 header 检查时 summary 输出 header_score=0/F（误导）；mcp/__init__ eager import 拉入全链路重依赖
- **app**：取消任务 exit_code=0 与 status="cancelled" 矛盾落库；模块级 _opener/_stealth_fetcher 跨任务共享互相关闭对方 TLS 会话；_format_bytes KB 档不可达；import_results 全有或全无（JSONL 坏行整体失败）；db 线程级连接从不关闭（ResourceWarning 来源）

## 七、💡 Suggestion（14 条，要点）

- adaptive similarity_score attrs 只比键不比值（注释误导）；crawler 单次 gather 创建数百协程（批大小应取 min(max_concurrency, 预算)）；spider Request.retries 是死字段
- `ai/__init__.py` 未导出 select_provider/OpenAIProvider 等公共 API；dom_pruner LLM 重排只升分不降分且按 DOM 序取候选；test_schema 的 pydantic 缺失分支测试是无效测试
- `_extract_json` 4 份重复实现应收敛；StateFingerprint 信息量不足（误报+漏报循环检测）；reverse_agent 截图文件名需 sanitize
- MCP/pentest 测试补 SDK 路径集成覆盖与耗时断言；hook records 增加 redact 开关
- **app**：crawler.py(3060 行)/ui.py(2644 行) 巨型文件建议拆分，ui.py 内嵌 ~1300 行 HTML/CSS/JS 抽为模板；测试固化了取消退出码不一致的错误行为（三处断言互相矛盾）；`--save-config` 把含 Cookie 的 header 明文写配置文件

---

## 八、系统性主题与修复优先级

### 主题 1：SSRF / URL 校验缺失（贯穿 4 个模块）
fetchers（🔴 file:// 已实测）、MCP 工具（🟠）、app 重定向绕过（🟠）、guardrails IP 编码绕过（🟡）、analyze_js 任意 URL（🟠）。
→ 建议建立统一 URL 校验工具（scheme 白名单 + 私网/环回/云元数据拦截 + 重定向逐跳校验），四处复用。

### 主题 2：reverse_agent 循环级状态脱钩（self._page vs 局部 page）
崩溃恢复、多标签页、close_tab 三个功能因此全部实际失效；recorder 编译产物必挂、checkpoint 续跑是死功能——"功能齐全但集成未验证"。
→ 补 3 个循环级集成测试（真实 page 桩 + 恢复、new_tab 后 observe 目标、compile 产物校验），再修根因。

### 主题 3：UI 控制面语义（app）
取消/暂停语义不一致、停止按钮是假的、日志捕获不到、本地 API 无鉴权。
→ 统一取消码契约 + 阶段守卫 + logging.Handler 转发 + Origin 校验。

### 修复优先级建议
1. **P0**：fetchers file:// SSRF（🔴，任意文件读取）；MCP pentest 无门禁 + URL 无校验（🟠）；app 本地 API 无鉴权（🟠）
2. **P1**：reverse_agent 崩溃恢复/多标签页失效 + checkpoint 续跑死功能（🟠×3）；recorder 产物必挂（🟠）；judge verified 布尔强转（🟠）
3. **P2**：代理故障闭环（mark_success 零调用，🟡）；UI 日志管线（🟠）；重定向 SSRF 校验（🟠）
4. **P3**：app mypy 20 错误修复并把 app 纳入 CI typecheck；巨型文件拆分；死配置清理（max_retries/advance/check_stall/retries）

### 流程缺口
1. **CI typecheck 只跑 `mypy src/web_crawler`，不检查 `app/`** —— 20 个错误因此长期漏网，建议 CI 增加 `mypy app`
2. 测试系统性盲区：方法级 mock 全覆盖但缺循环级/集成级验证（本次 3 个 High 全因此漏网）；部分测试断言与真实行为矛盾（取消码三处互相矛盾、recorder 子串断言、超时测试无耗时断言）

---

## 九、修复状态（2026-08 第二轮：全量修复完成 ✅）

6 路并行修复（各模块审查子代理带上下文直接修复）+ 2 路补覆盖 + 收尾协调，最终验证全绿：

| 验证项 | 修复前 | 修复后 |
|---|---|---|
| pytest 全量 | 2521 passed | **2851 passed, 3 skipped**（+330 测试，连续 2 轮全绿） |
| 覆盖率 | 99%（24 行未覆盖） | **100%**（10848 语句 0 未覆盖） |
| ruff | 全绿 | ✅ 全绿（src + app + tests） |
| mypy | src 通过；**app 20 错误** | ✅ **src + app 零错误**（CI typecheck 已纳入 app） |
| benchmark 回归 | 通过 | ✅ 通过 |

### 修复清单（按模块）

**fetchers（🔴→✅）**：file:// SSRF 已实测拦截（`validate_url_scheme` 入口强制 + 手动逐跳重定向校验 + max_redirects 上限 + 跨源剥 Authorization）；httpx 缺 h2 透明降级；未知 kwargs 显式 TypeError；代理故障闭环（mark_failed/mark_success/冷却清零/死代理轮换）；close() 跨事件循环清理改为告警保留引用；Camoufox 截图路径不再破坏指纹；_parse_proxy 兼容无 scheme/IPv6/百分号凭据；screenshot_tiles 加 max_tiles；ja3 参数更名（ja4 兼容别名）。

**核心库（🟠→✅）**：spider 暂停序列化 body(base64)/retries + 状态文件生命周期修复 + meta 序列化容错；URL 规范化去重（fragment/大小写/默认端口/ftp 丢弃）；adaptive 按 tag 预筛（消除 O(N²)）；ensure_list 透传生成器；robots single-flight；allowed() 剥离端口；storage.close() 幂等。

**AI 逆代理解析循环（6🟠→✅）**：崩溃恢复/多标签页循环页引用重绑定（根治）；checkpoint 稳定 task_id 使断点续跑真实生效；recorder 编译产物安全字面量 + compile 自检；护栏 new_tab 绕过封堵（URL 校验统一）；analyze_js 同源/白名单/大小上限；act 失败不再误标上一步；watchdog check_stall 接入；Plan.advance() 接通；_network_log 清理；hook 值截断 + prompt 对抗提示；CONFIRM 无回调降级 DENY；IP 编码绕过封堵；**新增 `ReverseAgentConfig.should_stop` 回调（app "停止"按钮已接线，真中断）**。

**AI LLM/验证码（2🟠→✅）**：judge verified 严格布尔解析（"false" 不再为 True）；extractor 非法 CSS 隔离走自愈；LLM 重试退避（429/5xx/网络错误）；Anthropic 端点修正；robots 轻量拉取 + 限速；Retry-After 上限；不可信字段 JSON 转义定界；极验不盲拖 + 半宽修正 + 坐标钳制；图片尺寸/体积上限 + 降采样坐标还原；dom_pruner 精简序列化；.env 搜索范围收敛；**新建 `ai/_jsonutil.py` 统一括号配平 JSON 解析（6 处重复收敛）**。

**MCP/pentest（4🟠→✅）**：pentest_recon 授权门禁 + 私网/环回/云元数据拦截；URL 工具 scheme 校验；Windows 超时真实生效（shutdown wait=False + cancel_futures + DNS 超时）；SDK 路径 to_thread 不阻塞事件循环；工具参数校验 + 输入上限；traceback 不外泄；Set-Cookie/Authorization/userinfo 脱敏；progress 通知真实推送；run_config 全量继承 + headless=True；mcp/__init__ 懒加载；port_scanner 多地址族合并；vuln_scanner payload 修复。

**app（6🟠→✅）**：取消语义统一（返回码 1 + 各阶段守卫）；UI 日志经 logging.Handler 转发（不再丢日志/串线）；"停止"按钮真中断（should_stop 接线）；SSRF 重定向逐跳校验；本地 API Origin 校验 + 拒绝路径消费请求体（修复 WinError 10053 连接重置）+ /open-output 白名单 + Cookie 不入库；暂停覆盖终态修复；后处理独立容错；流式 .part 落盘 + Content-Length 预检；deque + 状态节流；表单范围校验；_format_bytes PB 分支真 bug 修复；**mypy app 20 错误清零**。

### 收尾修复（审查后的追加发现）
- `_jsonutil.extract_json` 统一 6 处重复 JSON 解析（planner/reverse_agent 同步收敛）
- CSRF 拒绝路径不消费请求体 → Windows 下连接重置（WinError 10053）——已修复并连续 2 轮全量验证

### 遗留项（第三轮：全部解决 ✅）

- ✅ **巨型文件拆分**：`app/crawler.py` 3213 → **1419 行**，拆为 `crawler_models.py`（40 行，共享数据类）/ `crawler_net.py`（590 行，网络/解析/工具）/ `crawler_report.py`（1335 行，报告/格式）；`app.crawler` 保持全部属性兼容（测试 125+ 处 `patch.object(cr, ...)` 全部生效，全量 2858 passed 与基线一致）；`app/ui.py` 内嵌 ~1400 行 HTML/CSS/JS 抽为 **`app/static/index.html`**（package-data 打包携带，运行时读取，缺失兜底页）。
- ✅ **db.py 线程级连接不关闭**：全局连接登记表 + `close_thread_connection()`/`close_all_connections()` + atexit 统一关闭，**16 个 ResourceWarning 全部消除**（`-W error::ResourceWarning` 下测试通过）；死代码 `finish_task` 删除；mypy `annotation-unchecked` note 消除。

### 第三轮最终验证（2026-08）

| 验证项 | 结果 |
|---|---|
| pytest 全量 | ✅ **2858 passed, 3 skipped**（连续多轮全绿） |
| 覆盖率 | ✅ **100%**（10923 语句 0 未覆盖，57 个源文件全 100%） |
| ruff | ✅ 全绿（src + app + tests） |
| mypy | ✅ src + app 零错误（57 files） |
| benchmark 回归 | ✅ 通过 |
| ResourceWarning | ✅ 0（此前 16 个） |

---

## 十、体验端优化（第四轮：Web 控制台 + CLI + 文档 ✅）

### Web 控制台前端（app/static/index.html + ui.py）

- 🔴 **修复前端 script 从未执行的致命 bug**：URL 校验正则 `\\/\\/` 中未转义斜杠终止正则字面量 → 整段脚本 SyntaxError（采集器交互此前全部失效）；已修正 `\/\/` 并经 node --check 双 script 语法验证
- 🔴 **暂停→继续后按钮卡死**：poll 的 running 分支恢复「暂停」按钮（暂停/继续/终态三态统一重算）
- 🔴 **深色模式历史 Tab 断裂**：补定义 `--bg-card`/`--text-muted` 变量（此前永远回退白底）
- 🔴 **SSE 主路径截图不渲染**：final 事件补 `renderScreenshots`（此前仅轮询降级路径能出图）
- 🔴 **历史/结果 Tab 存储型 XSS**：url/content_type/status 等插值点统一 `escapeHtml`（此前直接拼 innerHTML，恶意站点可触发脚本）
- 🟡 历史 Tab 三个 fetch 链错误兜底（加载失败内联提示）；错误反馈持久化（操作失败写入日志区，不再被 poll 覆盖）；总耗时终态固定值（`finished_at` 字段，running 才显示动态）；移动端 Tab 栏 flex-wrap；截图卡片键盘操作（tabindex/Enter）+ 全局 Esc 关闭弹窗
- 🔵 favicon（消除 404 噪音）、fmtSize GB 档、状态筛选补 paused、0 条不渲染页码、max_bytes=0 提示、首帧状态徽章矛盾修复、max_bytes hint
- 💡 状态徽章 aria-live、初始主题读 prefers-color-scheme（可选）

### CLI / 脚本

- 🟠 **Docker 启动即失败修复**：ui.py 新增 `--allow-remote` 显式放行非回环绑定（Dockerfile CMD 与 compose 同步），控制面默认仍只绑回环
- 🟠 **--save-config → --load-config 往返崩溃修复**：load 时以 parser 默认值为底合并（缺省字段不再 AttributeError）、saver 补全 include_pattern/exclude_pattern/proxy/stealth/impersonate、缺文件 exit 2；新增 2 个往返回归测试
- 🟡 JSONL 清单**逐条实时追加**（每下载完成即写一行，不再结束时一次性写入），写失败 best-effort 降级
- 🟡 demo.py `max_requests` 死属性 → `run(max_requests=3)`；demo.bat/start-ui.bat 重写 Python 探测（先验证解释器再单次运行，保留 stderr 与退出码）；mcp/cli.py docstring 示例修正为真实子命令、无子命令 exit 0→2；Fetcher docstring 补 max_redirects/ja3_fingerprint
- 🔵 `/reverse/events` 死端点删除（含 3 个测试）；退出码语义对齐（0 成功 / 1 取消 / 2 配置错误）

### 文档（README / docs / CHANGELOG / .env.example / Docker）

- CHANGELOG `[Unreleased]` 由 "(none)" 补记为 Added/Changed/Fixed/Security 四小节（约 25 个提交的变更）
- README/docs：ja4→ja3 改名、补 max_redirects、api_key 注入表述修正、mypy 命令对齐 CI、--lang→--language、结构树补齐拆分模块、Budget 残留清除、验证码合规表述统一（不伪造凭证/不绕过付费墙）、容器浏览器限制注明、--allow-remote 说明
- .env.example 补 LLM_API_KEY / CRAWLER_DB_PATH

### 第四轮最终验证

| 验证项 | 结果 |
|---|---|
| pytest 全量 | ✅ **2858 passed, 3 skipped** |
| 覆盖率 | ✅ **100%**（10931 语句 0 未覆盖，57 源文件全 100%） |
| ruff | ✅ 全绿（src + app + tests + demo + benchmarks） |
| mypy | ✅ src + app 零错误（57 files） |
| benchmark 回归 | ✅ 通过 |
| 前端 JS | ✅ node --check 双 script 语法通过 |

---

*本报告由 6 路并行深度审查汇总生成；🔴/🟠 级发现中，file:// 任意文件读取、httpx http2 ImportError、judge bool 强转等关键论断均已本机实测复核。修复后全量验证：2858 passed / 覆盖率 100% / ruff 全绿 / mypy src+app 零错误 / benchmark 通过 / 无 ResourceWarning。*
