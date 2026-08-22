# 安全策略

## 支持的版本

| 版本 | 支持状态 |
| ---- | -------- |
| 0.3.x（master） | ✅ 支持 |
| < 0.3.0 | ❌ 不再支持 |

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
- 协议白名单：仅允许 `http` / `https`，`file://` 等其他协议在入口与每一跳重定向处一律拒绝。
- DNS 解析复查：解析主机名后逐地址核对拒绝段，防 DNS 重绑定型 SSRF；判定结果带 TTL 缓存。
- 重定向逐跳重新校验，跨域跳转剥离 `Authorization` 头。

### 本地服务

- Web UI 默认仅绑定回环地址；`--allow-remote` 显式开启远程访问时由使用者自行承担暴露风险。
- 本地 API 具备 Origin / CSRF 校验；任务历史持久化时剔除 Cookie 等敏感请求头。
- MCP pentest 工具需要显式 `authorization_confirmed=true`，且对私网目标默认拒绝。

### Power Mode

`WEB_CRAWLER_POWER_MODE=1`（或 `allow_private_hosts=True`）会放行私网目标校验，**仅限在自有可控环境中使用**。协议白名单不受 Power Mode 影响。详见 [README](README.md#-power-mode个人全解锁默认关闭)。

## 负责任使用

本项目面向**合法授权**的数据采集与安全测试场景。使用前请遵守目标站点的服务条款与所在地法律法规；`web_crawler.pentest` 仅用于已获书面授权的安全测试。
