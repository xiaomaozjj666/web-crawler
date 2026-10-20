# 安全策略

## 项目定位

`web-crawler` 是面向开发者的**隐身网页采集与安全研究工具集**：采集 / 逆向能力用于你拥有或已授权的目标，内置的 `pentest` 子包用于你拥有或已书面授权的资产做授权安全测试。公开发布版本默认开启安全防护（拒绝私网 / 云元数据目标、协议白名单、DNS 解析复查），提供个人 Power Mode 开关在自有可控环境中全解锁 host 校验。所有能力仅供合法、已授权用途，详见 [README · 用途与边界](README.md#用途与边界)。

## 支持的版本

| 版本 | 支持状态 |
| ---- | -------- |
| 0.5.x（master） | ✅ 支持 |
| < 0.5.0 | ❌ 不再支持 |

## 报告漏洞

**请不要通过公开 Issue 报告安全漏洞。**

请使用 GitHub 的 [私密安全公告](https://github.com/xiaomaozjj666/web-crawler/security/advisories/new) 提交报告，包含：

- 问题类型（SSRF 绕过、注入、拒绝服务等）
- 复现步骤 / PoC
- 影响的模块与版本
- 你对修复的建议（如有）

我们会在 **7 天内**确认收到报告，并在修复完成后公开致谢（除非你要求匿名）。修复会通过 `[Security]` 条目记录在 `CHANGELOG.md`。

## 安全设计概览

以下是本项目内建的安全边界，报告漏洞或审查代码时可作参照：

### SSRF 防护（默认开启）

- 所有抓取入口（`Fetcher` / `AsyncFetcher` / `DynamicFetcher` / `StealthyFetcher` / `CamoufoxFetcher`、app 下载器与重定向）默认拒绝私网 / 环回 / 链路本地目标，包括云元数据地址（`169.254.169.254`）、CGNAT、IPv6 ULA / 链路本地、`localhost` / `*.local`。
- inet_aton 兼容的非点分 IPv4 字面量（`2130706433` / `127.1` / `0177.0.0.1` / `0x7f000001`）先归一化再比对——Linux glibc 与 libcurl 会把这些形式解析到对应 IPv4 地址，不归一化即构成静态检查绕过。IPv4-mapped IPv6（`::ffff:127.0.0.1`）显式解包后按 IPv4 复查（旧 CPython <3.12.4 的 `is_private` 对 mapped 地址判定有误）。归一化由 `_ssrf.normalize_ip_literal` 统一提供，fetchers 钉扎与 MCP 门禁（`mcp/_ssrf_gate.py`）共用同一份实现，杜绝两处判定漂移。
- 协议白名单：仅允许 `http` / `https`，`file://` 等其他协议在入口与每一跳重定向处一律拒绝（MCP pentest 工具的目标解析层同样只接受 http/https）。
- DNS 解析复查（`resolve_hosts`，库层默认开启）：解析主机名后逐地址核对拒绝段，解析失败按保守策略拒绝；判定结果带 60s TTL 缓存。
- 重定向逐跳重新校验，跨域跳转剥离 `Authorization` 头。

### DNS check-then-connect（TOCTOU）的边界与对策

门禁时的 DNS 校验与传输层发起连接时的解析是两次独立查询，低 TTL DNS 在两次查询之间翻转记录即构成重绑定。本项目按传输层能力分层处置，**如实声明**各路径的残余风险：

- **httpx 兜底路径（根治）**：发送前自行解析并把 URL 的 host 替换为已逐一校验的 IP（`fetchers/_pin.py`），配合 httpcore 的 `sni_hostname` 扩展保持 SNI 与证书校验仍针对原始主机名——"校验的地址"就是"连接的地址"，窗口归零。显式/环境代理路径不适用（解析由代理完成）。
- **curl_cffi 主路径（根治）**：发送前自行解析并逐 IP 复核（绕过 60s 判定缓存），把全族已校验 IP（A + AAAA 全覆盖，防 Happy Eyeballs 绕过）以 `CURLOPT_RESOLVE` 条目逐请求钉给 libcurl（0.16.3 的 `session.curl_options` 透传，`perform` 后自动清理、不跨请求累积）——连接强制使用已校验地址，URL / Host 头 / SNI / 证书校验保持原始主机名。curl_cffi 缺失或过旧时退化为仅发送前即时解析校验（窗口缩到单次请求内）。显式/环境代理路径不适用。
- **浏览器路径（缓解 + 出口过滤）**：Playwright 的 DNS 由浏览器自身完成，无法逐跳插桩；MCP 浏览器工具的门禁只覆盖初始 URL。`capture_network_requests` 对捕获记录做出口过滤（`_filter_private_egress`）——非公网目标的记录整体打码，防止页面驱动的内网请求把响应内容带回客户端；`get_page_scripts` / `solve_captcha` 的门禁仍只覆盖初始 URL，此为已知边界。
- pentest 扫描器（`HeaderChecker` 等）重定向为手动逐跳跟随并逐跳重做 host 校验（含 DNS 解析复查），不使用传输层自动重定向。

### 磁盘缓存信任模型（CacheMiddleware）

`CacheMiddleware` 将 GET 响应落盘到缓存目录（默认 `.web_crawler_cache`：`<sha256>.meta.json` 存元数据，`<sha256>.body` 存 zlib 压缩的响应体），按「同机可写即受信」的本地信任模型设计——目录与运行进程处于同一信任域；共享目录、多用户机器或从备份恢复的缓存文件可能被投毒，防护如下：

- **解压炸弹防护（两层上限）**：body 读取前先 `stat` 检查压缩文件大小，超过 `max_body_size`（默认 64MB，可配置）直接拒绝、不读入内存；解压经 `zlib.decompressobj()` 以 64KB 块分块喂入并累计输出长度，超限即刻中止——内存峰值被钳制在上限 + 一个块以内，几十 KB 压缩数据解压出数 GB 的 zlib bomb 单次命中即被拦截。超限条目按损坏处理：删除、记 warning、视为未命中。
- **原子写保证**：写入走「同目录临时文件 + `os.replace` 原子替换」（Windows / Linux 均原子），临时文件名附加 pid 与线程 id——并发写同一 key 互不踩踏、last-write-wins；读取方不会见到半成品文件，「先 body 后 meta」的落盘顺序保证 meta 存在即 body 已完整落盘。
- **命名隔离**：`purge()` 只删除匹配 `^[0-9a-f]{64}\.(meta\.json|body)$` 的自有命名文件（SHA-256 缓存键 + 固定后缀），不误删用户存放在缓存目录里的其他内容。
- **损坏条目自愈**：meta JSON 解析失败、版本不符、body 缺失、截断流或尾随垃圾的 zlib 流一律按未命中处理并就地清理，避免后续请求反复读到坏数据。
- **写侧边界**：默认仅缓存 2xx（`cache_4xx` 可选纳入 4xx），5xx 永不缓存——错误响应不会在 TTL 内被反复回放。
- **残余边界（如实声明）**：meta 为未签名 JSON，投毒者可构造字段全部合法的 meta（新鲜 `stored_at`、正确 `version`）绕过 TTL 与版本检查，`status` / `headers` 亦随之可被伪造；但 body 解压上限独立于 meta 生效，投毒的 body 仍被钳制在 `max_body_size` 以内。
- 共享缓存目录或备份来源不可信时，请勿对该目录启用离线回放（`offline` 模式未命中直接抛 `IgnoreRequest`，不会有网络请求兜底核对真实性）。

### 本地服务

- Web UI 默认仅绑定回环地址；`--allow-remote` 显式开启远程访问时由使用者自行承担暴露风险。
- Web UI 对所有入站入口（GET / POST / DELETE）先做 Host 头白名单校验再进路由，防 DNS rebinding（攻击者域名先解析到公网加载页面，TTL 过后重绑定到 127.0.0.1，浏览器携带该域名 Host 头直连本地 UI，仅比对 Origin == Host 无法拦截）：回环绑定（默认 `127.0.0.1:8765`）仅放行 `127.0.0.1` / `localhost` / `[::1]` 字面量及绑定地址自身；端口不匹配、无端口形式（仅服务真监听 80 时合法）、Host 头缺失（HTTP/1.0 客户端）与畸形形式（含 `/` 等路径分隔符、userinfo `@`、非括号 IPv6）一律 fail-closed 拒绝并返回 403。`--allow-remote` 远程绑定下访问主机名不可枚举、无法构建白名单，退化为「非空 + 形式合法 + 端口匹配」下限校验——rebinding 域名与正常远程访问在该模式下 Host 上不可区分，边界由使用者显式承担（与 Origin / CSRF 校验构成纵深防御，互不可替代）。
- 本地 API 具备 Origin / CSRF 校验；任务历史持久化时剔除 Cookie 等敏感请求头。
- MCP pentest 工具需要显式 `authorization_confirmed=true`，且对私网目标默认拒绝。

### 代理信任模型

当用户配置 `proxy=`（`Fetcher` / `DynamicFetcher` / app 下载器）或设置 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量时，DNS 解析由代理完成，本项目的 host 校验仍在入口生效（对原始 URL 的 host 做静态检查 + DNS 解析复查），但连接的实际目标由代理决定——**代理是信任边界**：

- 代理可见明文 URL 与 Host 头（HTTPS 经 `CONNECT` 隧道后代理不可见 body，但仍可见目标 host）。
- HTTP 代理可重定向到内网（代理自身的响应 302 到 `127.0.0.1`）；HTTPS 代理经 `CONNECT` 建立隧道后不重写目标。
- SOCKS5 代理不做 host 校验（SOCKS 协议只转发 IP:port），代理可把任意目标连接到内网。
- 显式/环境代理路径自动跳过 DNS 钉扎（`_pin.py` 的 TOCTOU 根治不适用，解析由代理完成）——门禁仅覆盖入口 URL 的静态检查 + DNS 解析复查，连接路径由代理掌控。

使用不受信代理时，host 校验不构成充分防护；请在可信网络环境中使用可信代理。

### LLM API key 处理

`ReverseAgent` / `AIExtractor` / `VisualExtractor` 等模块通过环境变量（`DEEPSEEK_API_KEY` / `LLM_API_KEY`）或构造参数接收 API key：

- key 不记入日志（`logging` 调用只输出 host / status / 错误消息，不输出 key）。
- key 不出现在异常消息中（`AIProviderError` 只说「缺 API key」不泄露 key 值）。
- `.env` 文件由 `python-dotenv` 可选加载（存在则读，不存在则跳过）；`.env` 已在 `.gitignore` 中，不会入库。
- key 传入后由调用方管理生命周期——库不持久化 key、不缓存到磁盘、进程结束后随对象回收。
- `ReverseAgent` 的 `RunRecorder` 事件日志与 checkpoint 只记录动作/观察/思考文本，不记录 HTTP 头中的 `Authorization`。

### Power Mode

`WEB_CRAWLER_POWER_MODE=1`（或 `allow_private_hosts=True`）会放行私网目标校验，**仅限在自有可控环境中使用**。协议白名单不受 Power Mode 影响。详见 [README](README.md#-power-mode个人全解锁默认关闭)。

## 负责任使用

本项目面向**合法授权**的数据采集与安全测试场景。使用前请遵守目标站点的服务条款与所在地法律法规；`web_crawler.pentest` 仅用于已获书面授权的安全测试。
