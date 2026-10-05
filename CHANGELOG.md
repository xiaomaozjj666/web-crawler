# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **SSRF：恢复库级 fetcher 的 DNS 解析复查默认开启**：`Fetcher`/`DynamicFetcher`/
  `StealthyFetcher`/`CamoufoxFetcher` 构造参数 `resolve_hosts` 默认值由 `False`
  恢复为基类声明的安全默认 `True`（与文档"库层默认开启"一致）——原默认下
  攻击者把自己的域名解析到 `169.254.169.254` 即可绕过静态检查直连云元数据，
  无需任何重绑定技巧。可选依赖用户如确需关闭，可显式传 `resolve_hosts=False`。
- **SSRF：非点分 IPv4 字面量归一化**：`is_private_ip` 现识别 inet_aton 兼容
  写法（`2130706433`/`127.1`/`0177.0.0.1`/`0x7f000001`）——Linux glibc 与
  libcurl 会把这些形式解析到对应 IPv4 地址，此前静态检查全部放行。
- **SSRF：httpx 兜底路径连接层 DNS 钉扎（TOCTOU 根治）**：新增
  `fetchers/_pin.py`，发送前自行解析并把 URL host 替换为已逐一校验的 IP，
  配合 httpcore `sni_hostname` 扩展保持 SNI/证书校验仍针对原始主机名——
  门禁校验的地址即连接的地址，check-then-connect 窗口归零。显式/环境代理
  路径自动跳过（解析由代理完成）。curl_cffi 主路径因上游未暴露按请求的
  `CURLOPT_RESOLVE` 暂无法钉扎，维持入口复查 + 短缓存缓解（边界已写入
  SECURITY.md）。
- **SSRF：MCP 浏览器捕获出口过滤**：`capture_network_requests` 对捕获记录
  做二次 host 校验（`_filter_private_egress`，按 host 缓存解析判定）——
  页面自驱动的请求指向非公网目标时记录整体打码并计数上报，不再回传内网
  响应内容；门禁只覆盖初始 URL 的边界已在工具文档与 SECURITY.md 声明。
- **httpx 兜底路径 brotli 解码错误**：仿真默认请求头广告了 `br`，但 httpx
  未装 brotli 包时收到 brotli body 无法解码（实测复现）；现按运行时实际
  可解压的编码收缩 `Accept-Encoding`（curl 路径原生支持不受影响）。
- **种子 URL 被静默丢弃**：`normalize_url` 拒绝不合法/内网种子 URL 时补
  warning 日志（含 Power Mode 提示）——此前只返回空串，日志仅见
  "no resources found"，无从排查。
- **pentest：HeaderChecker 重定向逐跳 SSRF 校验**：安全头检测关闭传输层自动
  重定向，改为手动逐跳跟随并对每跳重做 host 检查（私网/环回/链路本地拒绝），
  防止已授权公网目标 302 到内网/云元数据、把内网响应头带回报告；
  `allow_private_hosts=True` 可显式放行（与 fetcher 同名选项语义一致）。
- **pentest：目标 scheme 白名单**：`pentest_recon` 的 target 解析层拒绝
  `file://`/`ftp://` 等非 http(s) scheme（纵深防御，与 fetcher 入口对齐）。

### Changed
- **sync/async 镜像代码去重（第一批）**：spider 的 run()/stream() 抽出共享
  的 `_bootstrap_queue`/`_schedule_retry`/`_retry_backoff`/`_finalize_state`
  （队列引导、重试入队与退避公式、状态文件收尾双路径同源，消除改一边漏
  一边的分叉风险）；fetcher 抽出 `_advance_redirect` 重定向状态机、
  `_curl_session_kwargs` 会话参数与 `_build_httpx_client` h2 降级构建器
  （重定向循环 sync/async 各 ~30 行镜像收敛为共享推进逻辑）；image_captcha
  的 OCR/滑块/点选三族 LLM 消息构造与后处理抽为共享助手，5 对方法瘦身。
- **reverse_agent.py 拆分**：1801 → 1440 行，纯函数层抽出——
  `ai/_reverse_prompt.py`（think prompt 模板与四类摘要格式化，166 行）、
  `ai/_reverse_support.py`（Observation/Action/ReverseAgentConfig 数据类与
  通用纯助手，398 行）；reverse_agent.py 保留薄 re-export/wrapper 兼容层，
  全部历史导入路径与 patch 目标不变（已逐项核对测试引用）。
- **测试：reverse_agent 单元测试去样板与 sync/async 类合并**：
  `test_reverse_agent_unit.py` 以模块级 autouse fixture + `_track()` 登记
  统一关闭 agent，移除 201 处 `try/finally: agent.close()` 样板；补齐
  12 个此前 async 路径缺失、仅 sync 版有的测试场景后，将 12 对逐字重复的
  sync/async 测试类合并为单类（`_AsyncPageMock` 的自动 AsyncMock 已使两套
  mock 形态行为归一，保留各对断言并集），文件 3800 → 2897 行、净删 49 个
  冗余测试副本，覆盖率不变。
- **大爬取性能：页面 HTML 不再全量驻留内存**：扫描阶段每页 HTML 原先整体
  累积进 `_CrawlContext.page_html` 直到任务结束（万页 × 300KB ≈ 3GB 常驻堆）；
  现改为记录落盘路径与编码（`page_files`），后处理（离线重写/智能抽取/正文
  抽取）逐页从磁盘重读，峰值内存从"全部页面"降为"单页"。
- **大爬取性能：离线 HTML 重写替换表整次构建一次**：新增 `RewriteTable`，
  原 `rewrite_html` 每页都重建 URL→本地路径映射（每行一次 `Path.resolve()`
  文件系统调用），500 页 × 3000 资源 ≈ 150 万次 resolve；现在每资源一次、
  逐页只做字符串替换，后处理从分钟级回到秒级。坏行（saved_path 越界）改为
  跳过并告警，不再中断整轮重写。`rewrite_html` 保持原签名作为单页便捷入口。
- **大爬取性能：待扫队列成员检查 O(1)**：`page_queue`（deque）配套
  `queued_pages` 集合索引，新链接去重不再对队列做线性扫描——万页级爬取
  "越爬越卡"的主因之一。
- **大爬取性能：`--resume-crawl` 状态快照去 O(P²) 写放大**：资源 dict 形态
  增量缓存（`resource_dicts()`，不再每次快照全量 `asdict` 反射深拷贝），
  并叠加 2 秒最小保存间隔（队列耗尽的最终保存不受限）。
- **代理路径连接复用**：app 层 `_get_opener` 按代理 URL 缓存 opener（原实现
  挂代理时每次请求、每次重定向跳都重建）；库级 `Fetcher` 的 httpx 兜底路径
  同样按 proxy 缓存专用客户端，`close()`/`aclose()` 统一释放。
- **下载循环增量等待**：线程池调度从 `FIRST_EXCEPTION` + 全量字典每 0.5 秒
  重挂回调，改为增量 pending 集合 + `FIRST_COMPLETED`（任一完成立即处理，
  大批量下载时主线程不再空转）。
- **Spider `stream()` item 缓冲换 `deque`**：批量排空从 `list.pop(0)` 的
  O(n²) 降为 O(n)。
- **AI 逆向：webpack 模块正则编译缓存**：按 (模板, 别名) 缓存编译结果，
  数千模块的 bundle 不再重复编译数千次。
- **captcha 双模块补日志**：`ai/captcha.py` 与 `ai/image_captcha.py` 的 40 处
  吞错点全部插桩（探测未命中 debug、求解失败 warning），"验证码解不出"
  从此可诊断。
- **LLM：chat/achat 公共逻辑抽纯函数**：`_build_chat_payload` 与
  `_is_retryable_llm_error` 双路径共用，消除重试分类与请求体构造的镜像
  重复（防止 sync/async 行为分叉）。
- **CI 鲁棒性**：全部 9 个 job 加 `timeout-minutes`（默认 6 小时上限改为
  10-40 分钟）；release 工作流补 concurrency 组（防 tag 重推并行跑两个
  release run）；bench 回归检测失败时写入 step summary（保持咨询性质）；
  移除无人消费的 coverage.xml 输出。
- **测试**：后处理容错类 e2e 测试的 6 处 patch 目标从 facade 修正为实际
  读取名字的 `_crawler_post` 模块（模块拆分后原 patch 已失效，测试变空转）。
- **Web UI 实测体验优化**：采集 Tab 更名「网页采集」（原「网页资源采集器」），
  主按钮「开始整理」改为「开始采集」，19 个复选框按「抓取范围 / 下载行为 /
  内容处理」三组归类，并区分「断点续传」（分块续传大文件）与「断点续爬」
  （任务中断恢复）；任务历史状态徽章中文化（与筛选下拉一致）、分页窗口化
  （36 个页码按钮 → `1 2 … 48`）、删除二次确认；停止按钮在页面刷新或轮询
  失败后仍保持可用（任务仍在服务器侧运行）；首次启动提示会下载浏览器组件；
  进程退出时把遗留的 `running` / `paused` 任务标记为 `error`，不再积累
  永远无法结束的僵尸任务。
- **`.gitignore` 移除本机工具目录条目**：该目录改由本机全局忽略规则承担，
  仓库内不再维护。

## [0.5.1] - 2026-08-28

### Changed
- **四个千行级模块拆分**（内部结构重构，行为零变化）：
  `mcp/server.py` 1805→587 行（工具目录/工具 Mixin/stdio 传输/SSRF 门禁/
  序列化助手各自成模块）、`app/ui.py` 1458→114（状态/runner/HTTP 路由/
  helper）、`app/crawler.py` 1573→612（上下文/扫描/发现/下载/后处理/CLI）、
  `app/crawler_report.py` 1332→100（清单/上下文/Markdown/HTML/抽取）。
  原模块保留全部历史导入路径（re-export），测试断言零改动；补 7 个
  后处理异常兜底分支测试。
- **Dependabot 忽略 playwright >=1.61**：camoufox 0.5.5 依赖
  `playwright<1.61`，组合安装交集为空——忽略规则避免每周产生注定
  失败的升级 PR，待 camoufox 放开依赖后移除。

## [0.5.0] - 2026-08-28

### Added
- **Spider 按域名限速**：`Spider.per_domain_delay = {"域名": 秒数}` 类属性，
  同域相邻请求保证最小间隔（精确/子域后缀匹配，状态按匹配域名记账）；
  同步路径直接补足等待，`stream()` 中该域自动串行化（per-domain 锁）。
- **UI 逆向任务「停止」按钮即时生效**：`ReverseAgentConfig.should_stop`
  协作式中断回调由 UI 构造 agent 时透传（`job.stop_event.is_set`），
  Agent 在每步循环顶部检查，返回 True 立即中断并把结果标记为
  `stopped`——无需再等 Agent 自然结束。


### Removed
- **移除 Codecov 集成**：上传长期因仓库未配置 token 被拒（tokenless
  上传不可用），CI 步骤假成功、徽章显示异常；覆盖率门禁已由
  `--cov-fail-under`（92/97）在 CI 内强制执行，README 徽章改为静态
  覆盖率标签。


### Changed
- **回退 playwright 下限提升**：camoufox 0.5.5 要求 `playwright<1.61`，
  与 `all` extra 的 `>=1.62` 交集为空（两个 Dependabot PR 单独 CI 绿、
  合并后冲突）；约束恢复 `>=1.40,<2`，组合安装由 pip 自动选兼容版本。
- **测试升级为警告即错误的严格模式**：`filterwarnings` 首位设 `error`，
  任何未豁免告警直接判测试失败；修复全部 55 处资源泄漏（`crawler.py`
  提取 `_close_jsonl()`、4 处 ThreadingHTTPServer fixture 补
  `server.server_close()`），新增 37 处 `ResourceWarning` 断言；41 个
  静默告警清零，覆盖率恢复 **100%（minimal-deps）/ 99.95%（full-deps）**。
- **全量可选依赖纳入测试测量**：新增 `test-extras` CI job（安装
  `all+camoufox+captcha`），此前因缺 playwright/curl_cffi 被 skip 的
  123 个测试全部执行——全依赖下 2927 passed、覆盖率 **99.73%**；
  extras job 门禁 97%，标准 job 门禁 92%（`--cov-fail-under` 各设）。
- **修复 ui.py 迁移遗留的 sys.path 污染**：裸 `import db/crawler` 造成
  模块双加载与覆盖率无法归属，改正规绝对导入后 db.py 恢复测量。
- **数据库默认路径改到当前工作目录**：`crawler_data.db` 此前默认写入
  包目录（`src/web_crawler/`），pip 安装到 site-packages 后可能无写
  权限、数据也会随重装丢失；现默认落在启动时的当前工作目录
  （`CRAWLER_DB_PATH` 仍可覆盖）。
- **reverse_agent 完整 async 核心化**：`run()` 改为
  `asyncio.run(arun())` 薄包装（事件循环内调用给出明确报错），删除
  23 个 sync/async 成对方法中的全部 sync 版（`_observe`/`_act`/20 个
  动作方法等），主循环与动作执行只剩 async 单实现，文件 2643 →
  1801 行；128 个 sync 路径测试改写为 async 版（覆盖率 93.85%）。


## [0.4.0] - 2026-08-23

### Added
- **Spider 下载中间件**：`DownloaderMiddleware`（`process_request` 返回
  `Response` 可短路下载、抛 `IgnoreRequest` 丢弃请求；`process_response`
  变换响应），`Spider.middlewares` 类属性声明、按序执行；`IgnoreRequest`
  丢弃计入 `SpiderStats.requests_ignored`（新增统计字段）。
- **Spider item 管道**：`ItemPipeline.process_item` 链式变换/过滤回调产出
  的 item，抛 `DropItem` 或返回 `None` 丢弃；`Spider.item_pipelines` 声明。
  两者均从顶层导出。
- **Spider 失败重试**：`Spider.max_retries`（默认 `0` 保持旧行为）— 下载失败的
  请求按 `0.5 * 2^n` 秒指数退避（上限 8s）重新入队，`Request.retries` 字段
  现在被引擎真实消费；重试耗尽才计入 `requests_failed`。
- **Spider robots.txt 遵守**：`Spider.respect_robots`（默认 `False`）+
  `Spider.user_agent`，对回调产出的请求做 robots 检查（per-host 缓存，
  拉取失败保守视为允许且不再重复外呼）。
- **`DupeFilter` 指纹去重**：Spider 去重从裸 URL 集合升级为
  method + url + body 的 SHA1 指纹（同 URL 不同分页参数不再互相误杀），
  可经 `Spider(dupefilter=...)` 替换为自定义实现；从 `web_crawler` 顶层
  导出。
- **CI 测试矩阵**：test job 扩展为 Ubuntu × Python 3.10–3.14 +
  Windows × 3.12（此前仅 ubuntu/3.12 单点），`fail-fast` 关闭以完整暴露
  兼容性问题；增加 workflow `concurrency` 取消组；release 构建前先跑测试。
- **覆盖率门禁**：`[tool.coverage.report] fail_under = 85`（实测 ~96%，
  门禁值为 CI 各平台波动留余量），`[tool.coverage.run] source` 修正为
  `web_crawler + app`（此前仅 `app`，与 CI 命令行参数不一致）。
- **治理文件**：新增 `CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`。
- **文档站自动发布**：新增 `.github/workflows/docs.yml`，push master 时
  `mkdocs gh-deploy --strict` 发布到 gh-pages；修正 `mkdocs.yml` 的
  `edit_uri`（`edit/main` → `edit/master`）。
- **自动化配套**：`.pre-commit-config.yaml`（ruff lint + format）与
  `.github/dependabot.yml`（pip + github-actions 周更）。
- **`DynamicFetcher.get` / `async_get` 别名**：与 `Fetcher.get` 动词统一，
  `Spider` / `AIScrapeAgent` 等上层组件不再需要 `hasattr` 探测 fetcher 类型。
- **PEP 561 类型标记**：`src/web_crawler/py.typed` 与 `app/py.typed` 入包
  （`package-data` 登记），下游 mypy 可正常解析本库类型。
- **SQLite 任务历史与结果持久化**（`app/db.py`）：任务/结果双表、线程安全连接、
  WAL 模式，数据库默认位于项目根目录 `crawler_data.db`，可通过
  `CRAWLER_DB_PATH` 环境变量覆盖；进程退出时由 atexit 统一关闭全部连接。
  Web UI 新增历史 Tab 与历史任务面板（`/jobs`、`/reverse/jobs`），可查看
  历史任务状态、分页浏览采集结果并删除任务。
- **`ReverseAgentConfig.should_stop` 回调**：主循环每步检查该回调，返回
  `True` 时立即中断并发出 `agent.stopped` 事件；Web UI「停止」按钮已接线，
  可真正中断正在运行的 Agent。
- **`Fetcher.max_redirects` 参数**（默认 `5`）：限制跟随重定向的最大跳数，
  配合逐跳 scheme 校验防止被重定向引向内网。
- **`Fetcher.ja3_fingerprint` 参数**：替代旧 `ja4_fingerprint`（保留为兼容
  别名，传入时发出 `DeprecationWarning`），透传到 `curl_cffi` 的 `ja3`
  参数做细粒度 TLS 指纹定制。
- **MCP 工具参数校验与输入上限**：所有工具入参按 schema 校验类型/必填/
  枚举，`code` 等字段设 2,000,000 字符上限，非法参数返回明确错误而非
  抛晦涩异常。
- **Web UI `--allow-remote` 参数**：远程/容器绑定（非回环地址）需显式
  开启，控制面无鉴权、仅限可信网络；`Dockerfile` CMD 已启用。

### Changed
- **三套 robots.txt 实现收敛**：`RobotsPolicy` 提取到公共模块
  `web_crawler/robots.py`（附标准库默认拉取 `fetch_robots_text`），
  Spider 与 AIScrapeAgent 共用同一实现；`web_crawler.ai.agent.RobotsPolicy`
  保留 re-export，既有导入路径不变。
- **`app` 包迁入 `src/web_crawler/app/`**：安装时不再向 site-packages 污染
  顶层 `app` 命名空间；entry points、测试、启动脚本引用同步更新
  （`web-crawler` / `crawler-ui` 命令行为不变）。
- **Spider `stream()` 改为持续流式调度**：并发槽位空出即派发队首请求，
  慢请求不再阻塞后续请求的调度（原实现为整批 barrier，一批中最慢的
  请求会拖住整批）；暂停/取消/max_requests 语义保持不变。
- **`app/crawler.py` 巨型 `crawl()`（603 行）拆分**：拆为 `_seed_page_queue`
  / `_restore_state` / `_scan_pages` / `_filter_resources` / `_process_resource`
  / `_run_downloads` / `_post_process` / `_log_crawl_summary` 阶段函数，
  共享状态经 `_CrawlContext` 传递，行为逐行等价（530 个 app 层测试全过）。
- **`reverse_agent` run/arun 重复段合并**：状态重置（`_reset_run_state`）、
  checkpoint 加载（`_load_checkpoint_state`）、结果构造
  （`_merge_final_hook_data` / `_build_run_result`）四段逐行重复的样板
  提取为共享方法；顺带修复 sync `run()` 未重置 `_last_think_prompt` /
  `_last_think_completion` / `_last_llm_usage` 的状态泄漏。
- **全库 docstring/注释统一中文**（约 49 个源文件、600+ 条翻译）：
  消除同文件内 docstring 英文、注释中文的混杂状态；技术术语
  （CSS/HTTP/robots.txt/SSRF/JSON/TLS/JA3/Playwright 等）保留英文。
- **Spider 状态持久化移入 `finally`**：`run()` / `stream()` 的回调异常、
  消费方提前 break 等中断路径不再跳过 `_dump_state`，已排队请求不丢；
  `stream()` 的 `asyncio.gather` 改用 `return_exceptions=True` 后逐个重抛，
  保持"单个回调致命错误中止整个流"的既有语义。
- **全库统一 `ruff format`**：一次性格式化 48 个文件，配套
  `.git-blame-ignore-revs` 缓解 blame 污染；CI 增加
  `ruff format --check`。
- **LLM 调用指数退避重试**：`OpenAICompatibleProvider.chat` / `achat` 对
  429 / 5xx / httpx 传输层错误按 `2^n` 秒退避最多重试 3 次（上限 30s），
  替代原先的一次性调用。
- **Web UI 模板抽离**：`app/ui.py` 内嵌的 ~1400 行 HTML/CSS/JS 抽为
  `app/static/index.html`（package-data 打包携带，运行时读取，缺失时显示
  兜底页）。
- **`app/crawler.py` 巨型文件拆分**：拆分为 `crawler_models.py`（共享数据
  类）/ `crawler_net.py`（网络/解析/工具层）/ `crawler_report.py`（报告/
  格式层），`app.crawler` 保持全部属性兼容。
- **`app/db.py` 连接生命周期修复**：全局连接登记表 +
  `close_thread_connection()` / `close_all_connections()`，消除线程级
  SQLite 连接永不关闭产生的 16 个 `ResourceWarning`；删除死代码
  `finish_task`。
- **dev 依赖上界放宽**：`pytest` 上界 `<9` → `<10`（允许 9.0.3+，
  修复 PYSEC-2026-1845），`pytest-asyncio` 上界 `<1` → `<2`（与
  pytest 9 配套）；Changelog / LICENSE 链接指向默认分支 `master`。
- **依赖下界刷新**（Dependabot）：`httpx>=0.28.1`、`numpy>=2.2.6`、
  `pytest-asyncio>=1.4.0`、`pycryptodome>=3.23.0`、`ddddocr>=1.6.1`，
  CI actions 全量升级（setup-python v7 / codecov-action v7 等）。

### Fixed
- **reverse_agent 崩溃恢复 / 多标签页 / checkpoint 续跑**：主循环统一经
  `self._page` 取当前页并重新绑定恢复后的页面引用，崩溃恢复与
  `new_tab` / `switch_tab` / `close_tab` 不再操作已关闭的旧页；checkpoint
  改用 URL 稳定哈希生成 `task_id`（去掉 `time.time()`），断点续跑真实生效。
- **judge `verified` 严格布尔解析**：仅 `True` / `"true"` / `"1"` 视为通过，
  LLM 输出字符串 `"false"` 不再被 `bool()` 误判为 `True`（`verified` 是任务
  完成判定的唯一安全闸门）。
- **MCP Windows 超时与事件循环**：pentest 超时真实生效
  （`shutdown(wait=False, cancel_futures=True)` + DNS 超时）；SDK 路径
  async handler 经 `asyncio.to_thread` 不阻塞事件循环。
- **Web UI 日志与取消语义**：爬虫日志经自定义 `logging.Handler` 转发到任务
  面板（不再依赖进程级 stdout 重定向）；取消统一返回码 1 且各阶段短路。

### Fixed（测试稳定性）
- **POST 303 重定向测试在 Windows 上的偶发失败**：测试服务器的
  `do_POST` 从不读取请求体，未读数据残留使连接关闭时发 RST 而非 FIN，
  客户端偶发 `WinError 10053`；响应前先读完 `Content-Length` 字节根治。

### Security
- **`file://` SSRF 拦截**：`validate_url_scheme` 在每次抓取入口与每个重定向
  跳转前强制仅放行 http/https（拒绝 `file://` / `ftp://` / `data:` /
  `gopher:`），跨源重定向剥离 `Authorization` 头，`DynamicFetcher` 渲染入口
  同样校验。
- **MCP pentest 授权门禁**：`pentest_recon` 必须显式传
  `authorization_confirmed=true`，并默认拒绝私网/环回/链路本地/云元数据
  地址（`allow_private=true` 可显式放行）。
- **本地 API Origin/CSRF 防护**：Web UI 校验 `Origin`/`Referer` 与请求
  `Host` 同源（`null` Origin 一律拒绝），表单 POST 无法被跨站触发；
  `/open-output` 限定白名单目录，Cookie 不再写入任务配置。
- **`analyze_js` 同源限制**：Agent 服务端拉取脚本仅限同源/白名单域、
  非内网的 http(s) URL（拒绝重定向到内网），并设内容大小上限，防止
  SSRF 与内网响应数据外带。

### Removed
- **`REVIEW-REPORT.md` 移出仓库追踪**：内部审查过程文档不随开源仓库分发
  （本地文件保留，已加入 `.gitignore`）。

## [0.3.0] - 2026-07-29

### Added
- **Image captcha solver** (`web_crawler.ai.image_captcha.ImageCaptchaSolver`):
  recognizes three image-challenge families without requiring a browser —
  text OCR (4–8 char alphanumeric), slider gap localization (Pillow + numpy
  template matching), and click-order coordinate recognition (Vision-LLM).
  Backend fallback chain: local `ddddocr` (optional) → `numpy`/Pillow template
  match → LLM Vision (`gpt-4o` / Claude / Qwen-VL / DeepSeek-vision, negotiated
  via `provider.capabilities.vision`). All three methods exposed in both sync
  and async variants.
- **CaptchaSolver image challenge integration**: `CaptchaSolver` now accepts
  an optional `image_solver` parameter; when injected, `_solve_hcaptcha` /
  `_solve_recaptcha_v2` automatically attempt `_solve_image_challenge`
  (iframe screenshot → Vision-LLM click coordinates → simulated clicks →
  submit) after the regular checkbox+token-wait path fails. `_solve_geetest`
  uses `image_solver.solve_slider` to localize the puzzle gap offset, falling
  back to the legacy random 220–320 px drag when unavailable.
  `CaptchaManager(image_solver=...)` injects the solver into both default and
  custom `CaptchaSolver` instances.
- **ReverseAgentConfig.enable_image_captcha** (default `True`): when enabled,
  `ReverseAgent` constructs an `ImageCaptchaSolver(provider=self.provider)` and
  passes it to `CaptchaManager`, so image challenges are auto-solved in both
  `run` and `arun` loops without code changes.
- **Lightweight pentest toolkit** (`web_crawler.pentest` subpackage, 6 modules):
  pure-Python, dependency-free reconnaissance utilities inspired by PentAGI's
  tool-integration approach (no `nmap` / `sqlmap` / `dirb` external commands).
  - `PortScanner` — TCP connect scan with `ThreadPoolExecutor` concurrency,
    TOP-100 default port list, built-in service-name map (22→ssh, 443→https,
    3306→mysql, 6379→redis, 8080→http-proxy, …).
  - `DirBruter` — directory / file path brute force over a 60+-path default
    wordlist, pluggable extensions, status filter (200/204/301/302/401/403),
    HTML title extraction.
  - `SubdomainEnumerator` — DNS-based enumeration over an 80+-prefix default
    dictionary, concurrent `getaddrinfo` + `gethostbyname_ex` for CNAME chain.
  - `VulnScanner` — rule-based detection for SQL injection (5 payloads +
    `SQL syntax` / `ORA-` / `PG::` / `mysql_fetch` error signatures), XSS
    (3 payloads + reflected-content match), path traversal
    (`../../../etc/passwd` + `root:x:0:0` signature); optional LLM analysis
    when a provider is injected.
  - `HeaderChecker` — 8 security headers (HSTS / CSP / X-Frame-Options /
    X-Content-Type-Options / Referrer-Policy / Permissions-Policy /
    X-XSS-Protection / Set-Cookie flags), 0–16 score, A–F grade.
  - `PentestReport` — aggregates all five results, exposes `summary()` /
    `to_dict()` / `to_json()`. Compliance notice: authorized testing only.
- **MCP `solve_captcha_image` tool**: standalone image captcha recognition
  without a browser. Three modes (`text` / `slider` / `click`); images passed
  as base64 strings; auto-negotiates LLM Vision capability on the configured
  provider.
- **MCP `pentest_recon` tool**: aggregate pentest reconnaissance over a target
  host or URL. Pick any subset of `ports` / `dirs` / `subdomains` / `vulns` /
  `headers` checks; returns the full `PentestReport` dict in one call.
- **CLI `captcha-image` subcommand**: shell entry point for image captcha
  recognition. Reads image files from disk, base64-encodes, dispatches to
  `solve_captcha_image`. `--mode text|slider|click` plus `--image` /
  `--bg` / `--slider` / `--prompt` flags.
- **CLI `pentest` subcommand**: shell entry point for pentest reconnaissance.
  `web-crawler-reverse pentest example.com --checks ports,headers --ports 22,80,443`.
- **Interactive REPL** now lists `captcha-image` and `pentest` commands.
- **101 new tests**: 46 in `tests/test_image_captcha.py` (dataclass defaults,
  base64 conversion, JSON extraction, `llm_vision_available` negotiation,
  sync + async paths for text/slider/click, mock provider for LLM routes,
  local OCR/slider fallback when deps absent), 23 in `tests/test_captcha.py`
  (ImageCaptchaSolver injection into CaptchaSolver/CaptchaManager,
  `_solve_image_challenge` success + all failure paths, `_geetest_detect_offset`
  with mock screenshot), 32 in `tests/test_pentest.py` (all five pentest
  modules, fully mocked, no real network). Test count: 302+ → 400+.
- New `[project.optional-dependencies] captcha` extra (`ddddocr`, `numpy`)
  for users wanting local OCR + accelerated slider template matching;
  `Pillow` already provided by the `visual` extra.

### Changed
- `web_crawler.ai.__init__` lazy export list extended with 4 new symbols
  (`ImageCaptchaSolver`, `ImageSolverConfig`, `SliderSolution`,
  `ClickSolution`).
- `CaptchaSolver.__init__` signature extended with optional
  `image_solver: ImageCaptchaSolver | None = None` (backward-compatible).
- `CaptchaManager.__init__` signature extended with keyword-only
  `image_solver` parameter (backward-compatible).
- `solve_captcha` MCP tool description updated to reflect image-challenge
  auto-solving capability (no schema change).
- README highlights section adds "Image captcha solver" and "Lightweight
  pentest toolkit" entries; architecture tree extended with
  `ai/image_captcha.py` and the `pentest/` subpackage.
- Project version bumped to `0.3.0`. Dependency lower bounds raised to
  reflect tested-compatible baselines: `lxml>=5.2`, `curl_cffi>=0.9`,
  `camoufox[geoip]>=0.4.4`, `ddddocr>=1.5`, `ruff>=0.9`,
  `mkdocstrings[python]>=0.27`. Verified against latest stable releases
  (`curl_cffi 0.15.0`, `camoufox 0.5.4`, `ruff 0.16.0`, `mcp 2.0.0`,
  `pytest 9.1.1`, `mypy 2.3.0`, `lxml 6.1.1`, `numpy 2.5.1`,
  `Pillow 12.3.0`, `playwright 1.61.0`, `pydantic 2.13.4`).

### Removed
- `ai.budget` module (`BudgetTracker` / `TokenBudget` / `BudgetPolicy`) and
  all references in `reverse_agent` / `cli` / `__init__`.
- `ai.vision` module (`VisionObserver`) and all references.
- `.gitlab-ci.yml` (CI migrated to `.github/workflows/ci.yml`).
- `start.bat` (no references; equivalent to the `crawler-ui` entry point).
- `ReverseAgentConfig.budget_total` / `budget_per_step` fields.
- CLI `--budget-total` / `--budget-per-step` arguments.
- `test_ai.py` (2021 lines) split into 11 standalone test files.

## [0.2.2] - 2026-07-28

### Added
- **End-to-end test suite** (`tests/test_e2e_reverse_agent.py`): starts a
  local HTTP server with a real `__sign` encryption page, launches
  `CamoufoxFetcher` + `ReverseAgent` through a full observe→think→act loop
  using a `StubProvider` (no real LLM API key required). Validates Hook
  injection, network capture, and `sign` parameter extraction. Two
  `@pytest.mark.slow` tests auto-skip when Camoufox is absent; a third
  unit test verifies the `StubProvider` reply sequence without a browser.
- **Humanized input trajectory** (`ReverseAgentConfig.humanize_input`,
  default `True`): `click` now `hover`s the selector first, waits a random
  50–200 ms, then clicks; `type` `focus`es the selector, waits 100–300 ms,
  then types with a per-keystroke `delay=30–150 ms`. Both sync and async
  paths; mock objects that reject `delay=` auto-degrade via `TypeError`.
- **Multi-tab management** — 3 new actions: `new_tab` (open + navigate +
  switch active page; registers the original page as `"main"`),
  `switch_tab` (by `name` or `index`; calls `bring_to_front`), `close_tab`
  (closes; falls back to `main` if the active tab was closed). Both sync
  and async paths emit `browser.action` events. LLM prompt extended with
  the three action descriptions.
- **JA4 fingerprint customization** (`Fetcher.ja4_fingerprint` /
  `AsyncFetcher.ja4_fingerprint`): passes a JA3/JA4 TLS extension string
  through to `curl_cffi`'s `ja3` parameter, overriding the `impersonate`
  preset's default TLS fingerprint for fine-grained ClientHello control.
  Silently ignored under the httpx fallback. Both sync and async sessions
  honor the parameter.
- **API documentation site** (`mkdocs.yml` + `docs/`): MkDocs Material +
  mkdocstrings configuration with four pages — `index.md` (home / quick
  start), `architecture.md` (module tree + per-layer responsibilities +
  data flow), `reverse-agent.md` (full agent guide, all actions, all
  config fields), `api-reference.md` (auto-generated API docs for
  `Fetcher`, `Selector`, `ReverseAgent`, `Spider`, LLM providers). New
  `[project.optional-dependencies] docs` extra (`mkdocs`,
  `mkdocs-material`, `mkdocstrings[python]`).
- **Performance regression baseline** (`benchmarks.py`): built-in
  `BASELINE` dict of ms/op thresholds; new `--check-regression` flag
  (CI mode, exits 1 when any benchmark exceeds 1.2× the baseline),
  `--baseline PATH` (compare against a saved JSON baseline),
  `--save-baseline PATH` (persist current results). `check_regression`
  and `save_baseline` / `load_baseline` helpers exposed for programmatic
  use. `tests/test_benchmark_regression.py` smoke-tests the regression
  logic without asserting absolute numbers.
- **Slow test marker** registered in `pyproject.toml`
  (`markers = ["slow: ..."]`); the Camoufox e2e suite is marked
  `@pytest.mark.slow` so CI can deselect it via `-m "not slow"`.
- **16 new tests**: 5 JA4 fingerprint customization, 8 multi-tab
  management, 5 humanized input trajectory (sync + async), plus
  benchmark regression smoke tests. Total test count: 286 → 302+.
- **Browser interaction actions**: `ReverseAgent` now supports 6 real
  Playwright browser actions — `click`, `type`, `scroll`, `press`, `hover`,
  `select_option` — in both sync `run` and async `arun` paths. All actions
  use 10s timeout, auto-screenshot on failure, and emit `browser.action`
  events for UI. LLM prompt extended with action descriptions.
- **ActionGuard** gains 2 new rules: `no-dangerous-click` (blocks clicks on
  删除/delete/logout/退出/withdraw/提现/支付 buttons) and
  `no-selector-injection` (blocks JS injection in CSS selectors).
- **ConfidenceScorer** `_VALID_ACTIONS` extended with 6 browser action types
  and their required-parameter validation.
- **RunRecorder** compile method extended to compile 6 browser actions into
  replay scripts; also fixes a pre-existing indentation bug that prevented
  multi-step paths from generating valid Python.
- **SSE real-time push** (`GET /reverse/stream?id=...`): Server-Sent Events
  endpoint streams `snapshot` / `step` / `final` events to the browser,
  replacing 800ms HTTP polling. Frontend auto-degrades to polling on SSE
  error.
- **MCP progress token**: `reverse_engineer_url` now constructs a progress
  token, subscribes to ReverseAgent's EventBus `step.end` events, and
  actually calls `report_progress` to push progress notifications to MCP
  clients (Cursor / Claude Desktop).
- **Docker deployment**: `Dockerfile` (python:3.12-slim + Firefox ESR +
  Playwright deps), `.dockerignore`, `docker-compose.yml` (port 8765 +
  volume mounts + DEEPSEEK_API_KEY env), `.github/workflows/release.yml`
  (push `v*` tag triggers build + PyPI publish + GitHub Release).
- **`--host` CLI flag** for `app/ui.py` (default 127.0.0.1; use 0.0.0.0 in
  Docker).
- **26 new tests**: browser action execution (sync + async), confidence
  scoring, guard blocking, recorder compilation, Action parsing, prompt
  content. Test count: 260 → 286.
- **Single-model strategy**: `ReverseAgentConfig.budget_total` and
  `budget_per_step` default to `None` (budget disabled by default). UI
  removes "LLM rerank" / "LLM scoring" toggles. `BudgetPolicy.DOWNGRADE`
  documented as no-op under single-model strategy.
- **Screenshot capture** (`ReverseAgent.enable_screenshot`): every observation
  step and every think/act error path now saves a PNG to
  `reverse_screenshots/<task_id>_step<N>[_error].png`. Failures are swallowed
  (empty path returned) so the main loop never crashes on a screenshot error.
  Both sync `run` and async `arun` paths covered; `_screenshots` list and
  `_last_error_screenshot` are exposed in the result dict.
- **CLI `run` subcommand** (`web-crawler-reverse run`): one-shot agent
  execution that bypasses MCP and calls `ReverseAgent.run` directly. Supports
  `--enable-screenshot` / `--no-enable-screenshot`, `--save-script`,
  `--output`, and all budget/guard/checkpoint flags.
- **MCP `reverse_engineer_url`** now returns `budget_summary`,
  `last_confidence`, `checkpoints`, `screenshots`, and `error_screenshot`
  fields so upstream AI clients get full runtime state in one call.
- **Web UI enhancements** (app/ui.py):
  - Historical task list panel (`GET /reverse/jobs`) with click-to-load.
  - Screenshot gallery with click-to-zoom modal (`GET /reverse/screenshot`).
  - Task template buttons (Anti-Content / X-Bogus / _signature / generic).
  - Config import/export (`POST /reverse/config/export`,
    `POST /reverse/config/import`) with field normalization.
  - Clear-runtime-data button (`POST /reverse/clear`).
  - Adaptive polling: 800 ms while running, 3 s when idle.
  - Statistics cards (steps, avg step time, token rate, hook count, param
    hit rate).
  - Incremental event query (`GET /reverse/events?since=TS`).
  - Script download with Content-Disposition (`GET /reverse/script`).
  - `ReverseJobState` extended with `screenshots`, `error_screenshot`,
    `step_durations`, `events_since()`, `clear_runtime()`, `job_summary()`.
- **10 new tests**: screenshot success/disabled/error/failure/none-page/async
  (mock page.screenshot), CLI run arg parsing, defaults, mocked execution,
  save-script-to-file.
- **JS reverse-engineering agent** (`web_crawler.ai.reverse_agent.ReverseAgent`):
  Camoufox-driven browser observe→think→act loop（由大模型驱动）。
  Injects JS hooks (fetch / XHR / cookie / `crypto.subtle` / webpack / console),
  captures network traffic, splits webpack bundles, then asks the LLM to
  deobfuscate and reimplement signing algorithms in Python.
- **JS Hook library** (`web_crawler.ai.hooks.HookLibrary`) with 6 hooks and a
  combined-script generator; `collect_hook_data` sync/async helpers.
- **JSAnalyzer** (`web_crawler.ai.analyzer`) — webpack module extraction,
  signing-flow tracing, AI deobfuscation and algorithm reimplementation.
- **CaptchaManager** (`web_crawler.ai.captcha`) — detects Turnstile / hCaptcha /
  reCAPTCHA v2&v3 / 极验 GeeTest; only simulates normal user interaction (入口
  click / token-wait / humanized drag). Image challenges hand off to human.
- **CamoufoxFetcher** (`web_crawler.fetchers.camoufox`) — fingerprint-resistant
  Firefox via Camoufox + Playwright, with hook-injection and network-capture
  helpers.
- **MCP server** (`web_crawler.mcp.server.ReverseMCPServer`) — exposes 9 tools
  over JSON-RPC/stdio for AI clients (Claude Desktop / Cursor). Falls back to a
  manual stdio implementation when the `mcp` SDK is missing.
- **CLI** (`web-crawler-reverse`) with 10 subcommands plus an interactive REPL.
- New `[project.optional-dependencies]`: `camoufox`, `mcp`.
- New entry points: `web-crawler-mcp`, `web-crawler-reverse`.
- **Mainstream-agent alignment modules** (browser-use / Skyvern / PentAGI
  feature parity), all individually toggleable via `ReverseAgentConfig`:
  - `web_crawler.ai.dom_pruner.DomPruner` — DOM focus pruning (rule + LLM
    rerank, ≈80% token cut, Skyvern/browser-use style).
  - `web_crawler.ai.checkpoint.CheckpointManager` — step-end state
    persisted; resume after crash or interrupt (`CheckpointStore` keeps the
    last N snapshots, atomic writes).
  - `web_crawler.ai.budget.BudgetTracker` — per-step / per-call / global
    token caps with COMPRESS / DOWNGRADE / STOP policies; raises
    `BudgetExceeded` when hard-stop is configured.
  - `web_crawler.ai.confidence.ConfidenceScorer` — rule + LLM dual-path
    scoring; low-confidence actions trigger fallback.
  - `web_crawler.ai.guardrails.ActionGuard` — domain whitelist, blocks
    localhost / non-HTTPS / cross-origin / dangerous scripts; supports
    custom `GuardrailRule` and CONFIRM callbacks.
  - `ReverseAgent` now wires DomPruner / Checkpoint / Budget / Confidence /
    Guard into both sync `run` and async `arun` loops; `ContextCompressor`
    gains `force_compress` / `force_compress_async` for budget-driven compaction.

### Changed
- `_FakeBrowserPage` / `_FakeAsyncBrowserPage` test mocks extended with
  `goto` / `bring_to_front` / `close` / `on` methods to support the new
  multi-tab and humanize tests.
- `Fetcher` docstring updated to mention `ja4_fingerprint`.
- README adds "Multi-tab management & humanized input", "JA4 fingerprint
  customization", and "Documentation" sections; `--check-regression`
  flag documented under Development.
- `.gitlab-ci.yml` adds a `bench` stage (runs
  `python benchmarks.py --check-regression`) and a `docs` stage (runs
  `mkdocs build --strict`); `test` stage now runs
  `pytest -m "not slow"` to skip the Camoufox e2e suite.
- `pyproject.toml` `[tool.setuptools.packages.find]` extended to include
  `app` package.
- Web UI right panel replaced "Token budget" ring with "Task statistics"
  card (steps / avg step time / elapsed / hook count / network count).
- `app/ui.py` polling logic refactored: `pollReverse` split into
  `updateReverseUI` (reusable by SSE) + `appendReverseEvent` (incremental).
- `pyproject.toml`: bumped `[tool.mypy].python_version` to `"3.12"` (modern
  stubs like numpy 2.5 use PEP 695 `type` statements). `requires-python` still
  `>=3.10`; runtime compat is verified by the test suite.
- Removed `requirements.txt` / `requirements-dev.txt` — install via
  `pip install -e ".[dev]"` (or `.[mcp,dev]`, `.[all,dev]`). CI updated.
- README: refreshed architecture tree, added a "Mainstream-agent alignment"
  capability matrix, documented all installation extras.
- `web_crawler.ai.__init__` now lazily exports 15 alignment symbols
  (`DomPruner`, `PrunedDom`, `Checkpoint`, `CheckpointManager`,
  `CheckpointStore`, `BudgetTracker`, `TokenBudget`, `BudgetPolicy`,
  `BudgetExceeded`, `ConfidenceScorer`, `ConfidenceResult`, `ActionGuard`,
  `GuardrailResult`, `GuardrailAction`, `GuardrailRule`).
- **Single-model strategy**: `ReverseAgent` and every sub-component
  (Planner / Actor / Judge / DomPruner / ConfidenceScorer / JSAnalyzer)
  share the same `DeepSeekProvider(model="deepseek-v4-pro")` instance.
  The Web UI no longer exposes "LLM rerank" / "LLM scoring" toggles (they
  would imply a second model and contradict the single-model policy).
  `BudgetPolicy.DOWNGRADE` is documented as a no-op under this strategy;
  use `COMPRESS` (default) or `STOP` for budget overruns.

### Removed
- Web UI "Token budget" panel, budget fieldset, budget JS update logic,
  `ReverseJobState.budget_*` fields, budget event handlers.

## [0.2.1] — 2026-07-12

### Fixed
- UI: `rewrite_html` now has its own checkbox (was incorrectly wired to `strip_overlays`)
- `app/crawler.py`: `print()` → `_log.error()` for header parse errors
- `app/ui.py`: startup messages now use `logging` instead of `print()`

### Changed
- `playwright` and `curl_cffi` moved to `[project.optional-dependencies] all` — no longer forced dependencies
- Added `[project.scripts]` entry points: `web-crawler` and `crawler-ui`
- Updated LICENSE copyright to `xiaomaozjj666`
- Expanded `.gitignore` to cover runtime-generated files

## [0.2.0] — 2026-06-27

### Added
- **Lazy imports**: `import web_crawler` no longer forces `playwright` or
  `curl_cffi` to load. Public symbols are resolved via `__getattr__` on first
  access (Scrapling-style), so parser-only users pay no optional-dependency
  import cost.
- **`AsyncFetcher`** — a dedicated async-only fetcher class (Scrapling
  `AsyncFetcher` parity). Shares all config/retry/stealth logic with `Fetcher`
  but exposes only async methods (`get`/`post`/`request` are coroutines); no
  sync session is ever created.
- **Adaptive public API on `Selector`**:
  - `save(element, identifier)` — explicitly persist a fingerprint.
  - `retrieve(identifier)` — fetch a stored fingerprint record.
  - `relocate(element, threshold=)` — manually relocate an element by
    structural similarity against the current document.
- **DOM traversal** (Scrapling parity): `siblings`, `next`, `previous`,
  `path` (root-to-self ancestor chain).
- **Selector methods** (Scrapling parity): `find_by_regex`, `re`, `re_first`,
  `get_all_text`, `prettify`.
- **`ResultList` batch helpers** (Scrapling `Selectors` parity): `get`,
  `getall`, `css`, `xpath` (batch), `.length`, slice-safe typing, and
  property-style `.first` / `.last`.
- **Backward-compat aliases**: `Adaptor = Selector`, `Adaptors = Selectors`.
- **PEP 561**: `py.typed` marker shipped with the package.
- `pyproject.toml` metadata aligned with Scrapling: full `classifiers`,
  `keywords`, `authors`, `[project.urls]`.
- `CHANGELOG.md` introduced.

### Changed
- `Fetcher` backend imports (`curl_cffi`, `httpx`) are now deferred to session
  construction time instead of module import time.
- `first` / `last` on `ResultList` changed from callables to properties
  (Scrapling `Selectors.first` / `.last` parity). `css_first` / `xpath_first`
  still accept a `default` argument.
- `fetchers/__init__.py` lazily imports `DynamicFetcher` / `StealthyFetcher`
  (Playwright) so `from web_crawler.fetchers import Fetcher` stays cheap.

## [0.1.0] — initial release

- Adaptive `Selector` over `lxml` with fingerprinting + similarity relocation.
- `Fetcher` (curl_cffi TLS stealth, httpx fallback), `DynamicFetcher`
  (Playwright), `StealthyFetcher` (anti-bot / Cloudflare).
- `ProxyPool` with round-robin/random rotation and cooldown.
- `Spider` framework with callback dispatch, priority scheduling, dedup,
  domain filtering, and JSON pause/resume.
- Unified `Response` with `.css()` / `.xpath()` / `.json()` helpers.
