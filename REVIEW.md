# 评审指南

本文件定义本仓库的评审优先级与可跳过范围。

## 最可能失效的地方

1. **SSRF 主机判定与 DNS 钉扎的三处实现漂移** — `src/web_crawler/_ssrf.py` 的网段与主机名判定、`src/web_crawler/fetchers/_pin.py` 的发送前即时解析与 `CURLOPT_RESOLVE` 钉扎、`src/web_crawler/mcp/_ssrf_gate.py` 的 MCP 入口门禁共享同一套语义。近期刚修过 `*.localhost` 后缀精确匹配（去掉残留子串匹配）与 FAQ 默认值对齐；任何一处单独改判定规则都会造成门禁绕过或误杀。重点核对非点分 IPv4 归一化、IPv4-mapped IPv6 解包、解析失败 fail-closed、60 秒判定缓存与逐请求复核、显式代理路径跳过钉扎的边界声明。

2. **静态轨迹编译与离线回放** — `src/web_crawler/spider/static_replay.py` 是近期新增路径：只接受成功的 GET `navigate` 快照与 CSS `extract`，编译期必须拒绝未知动作、非 2xx 快照和歧义选择器；写入前要脱敏 header、query 与 body；回放依赖 `src/web_crawler/spider/cache.py` 的磁盘缓存与持久化去重，重复运行必须零新增 item。重点看输出目录非空拒绝、`replay.json` 与生成的 `spider.py` 之间契约一致、低置信度 item 的隔离路径、UTF-8 读写。

3. **跨平台编码与路径** — Windows 在 CI 矩阵中有独立验证点；近期连续修复了非 UTF-8 代码页打印中文崩溃（`src/web_crawler/__main__.py`、`src/web_crawler/mcp/server.py` 的 `_ensure_utf8_stdio`）、测试中裸 `read_text` 缺显式编码、截图残留落盘位置。新增代码里任何未显式 `encoding="utf-8"` 的文本读写、`subprocess` 的 `text=True`、`Path.home()` 与 `os.replace` 相关假设都要核对。

4. **MCP 传输层双实现** — `src/web_crawler/mcp/_transport.py` 同时维护 SDK 1.x 装饰器路径与 2.x 构造回调路径，另带手写 JSON-RPC 降级。只有安装 `.[mcp]` 时才会真实走 SDK 分支（CI 的 `test-extras` 与夜间任务负责覆盖）；判据 `hasattr(Server, "list_tools")`、进度通知的 meta 形态（对象与 TypedDict 两种）很容易在一条路径上通过、另一条路径失效。

5. **Spider 调度与状态持久化** — `src/web_crawler/spider/spider.py` 集中了优先级队列、`DupeFilter`、域级并发信号量、双层重试（传输层 `fetcher.retries` 与响应级 `retry_http_status`）、robots `Crawl-delay` 以及 JSON 暂停续跑。域槽位计数、退避与入队顺序、resume 状态兼容是回归高发区。

6. **本地 Web UI 的 Host 白名单** — `src/web_crawler/app/_ui_http.py` 对 GET / POST / DELETE 统一先校验 Host 再进路由，回环绑定只放行 `127.0.0.1` / `localhost` / `[::1]`，`--allow-remote` 退化为形式校验。新增端点若绕过 `_is_host_allowed`，或静态文件路径未规范化，会重新打开 DNS rebinding 与路径穿越面。

## 重点看

- `src/web_crawler/_ssrf.py`、`src/web_crawler/fetchers/_pin.py`、`src/web_crawler/mcp/_ssrf_gate.py`
- `src/web_crawler/spider/static_replay.py`、`src/web_crawler/spider/cache.py`
- `src/web_crawler/mcp/_transport.py`
- `src/web_crawler/app/_ui_http.py`
- `src/web_crawler/spider/spider.py`

## 可以跳过

以下目录存在于本地工作区，但已被 `.gitignore` 排除，不会出现在 PR diff 中：`site/`（MkDocs 构建输出）、`src/stealth_web_crawler.egg-info/`、`src/web_crawler.egg-info/`、`crawler_output/`、`reverse_screenshots/`、`.venv/`。仓库没有 lock 文件、vendor 目录或测试快照目录；除上述生成物外仍需看全量 diff。

## 评审规则

- 每条发现必须带 `file:line` 证据，不接受凭命名或猜测。
- 标严重度：Important / Nit / Pre-existing；区分本次引入与既有问题。
- 先报真实 bug，再报可读性；最多 3 条 nit。
- 已提过的发现不重复；第 3 轮起只审新增 diff，并请作者重新考虑方案。
- 每条发现必须被处理：修复，或在回复中说明为何不修。
- 安全类发现未解决前不得合并。

## 本地验证命令（与 CI 一致）

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src/web_crawler
pytest -m "not slow" --cov=web_crawler --cov-fail-under=92
```

涉及浏览器、TLS 指纹或可选依赖的改动，另装 `.[all,dev,camoufox,captcha,mcp]`，覆盖率门禁按 97 执行；夜间任务会跑全部测试（含 `slow` 标记）并执行 `pip-audit --skip-editable`。
