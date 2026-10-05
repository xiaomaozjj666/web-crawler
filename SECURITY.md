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
- inet_aton 兼容的非点分 IPv4 字面量（`2130706433` / `127.1` / `0177.0.0.1` / `0x7f000001`）先归一化再比对——Linux glibc 与 libcurl 会把这些形式解析到对应 IPv4 地址，不归一化即构成静态检查绕过。
- 协议白名单：仅允许 `http` / `https`，`file://` 等其他协议在入口与每一跳重定向处一律拒绝（MCP pentest 工具的目标解析层同样只接受 http/https）。
- DNS 解析复查（`resolve_hosts`，库层默认开启）：解析主机名后逐地址核对拒绝段，解析失败按保守策略拒绝；判定结果带 60s TTL 缓存。
- 重定向逐跳重新校验，跨域跳转剥离 `Authorization` 头。

### DNS check-then-connect（TOCTOU）的边界与对策

门禁时的 DNS 校验与传输层发起连接时的解析是两次独立查询，低 TTL DNS 在两次查询之间翻转记录即构成重绑定。本项目按传输层能力分层处置，**如实声明**各路径的残余风险：

- **httpx 兜底路径（根治）**：发送前自行解析并把 URL 的 host 替换为已逐一校验的 IP（`fetchers/_pin.py`），配合 httpcore 的 `sni_hostname` 扩展保持 SNI 与证书校验仍针对原始主机名——"校验的地址"就是"连接的地址"，窗口归零。显式/环境代理路径不适用（解析由代理完成）。
- **curl_cffi 主路径（缓解）**：其 requests 式 API 未暴露按请求的 `CURLOPT_RESOLVE`（0.16.3 核实），无法在连接层钉扎；依赖入口解析复查 + 60s 判定缓存把窗口缩到有限时长。待上游暴露该能力后跟进。
- **浏览器路径（缓解 + 出口过滤）**：Playwright 的 DNS 由浏览器自身完成，无法逐跳插桩；MCP 浏览器工具的门禁只覆盖初始 URL。`capture_network_requests` 对捕获记录做出口过滤（`_filter_private_egress`）——非公网目标的记录整体打码，防止页面驱动的内网请求把响应内容带回客户端；`get_page_scripts` / `solve_captcha` 的门禁仍只覆盖初始 URL，此为已知边界。
- pentest 扫描器（`HeaderChecker` 等）重定向为手动逐跳跟随并逐跳重做 host 校验（含 DNS 解析复查），不使用传输层自动重定向。

### 本地服务

- Web UI 默认仅绑定回环地址；`--allow-remote` 显式开启远程访问时由使用者自行承担暴露风险。
- 本地 API 具备 Origin / CSRF 校验；任务历史持久化时剔除 Cookie 等敏感请求头。
- MCP pentest 工具需要显式 `authorization_confirmed=true`，且对私网目标默认拒绝。

### Power Mode

`WEB_CRAWLER_POWER_MODE=1`（或 `allow_private_hosts=True`）会放行私网目标校验，**仅限在自有可控环境中使用**。协议白名单不受 Power Mode 影响。详见 [README](README.md#-power-mode个人全解锁默认关闭)。

## 负责任使用

本项目面向**合法授权**的数据采集与安全测试场景。使用前请遵守目标站点的服务条款与所在地法律法规；`web_crawler.pentest` 仅用于已获书面授权的安全测试。
