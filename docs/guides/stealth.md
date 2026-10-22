# 隐身与网络指南

本页讲清楚 `web-crawler` 的网络栈：TLS 指纹隐身、会话池、代理轮换，以及
默认开启的 SSRF 防护边界。API 细节见
[API 参考 · 核心抓取](../api-reference.md#fetchers)。

## 为什么需要 TLS 指纹

服务器与 WAF 可以从 TLS ClientHello（JA3 指纹）与 HTTP/2 帧序识别出
「这不是真浏览器」——即使 UA 伪装得再像。`Fetcher` 基于 `curl_cffi`
重放真实浏览器的 TLS/JA3 指纹与 HTTP/2 帧序，让请求在网络层与 Chrome
难以区分：

```python
from web_crawler import Fetcher

with Fetcher(impersonate="chrome131") as f:
    resp = f.get("https://example.com")
```

- `impersonate`：浏览器预设（默认 `"chrome131"`）；
- `ja3_fingerprint`：JA3 指纹字符串，覆盖预设做细粒度 TLS 扩展定制
  （仅 `curl_cffi` 后端生效）；
- `http2=True`：HTTP/2（默认开启）；
- `max_redirects=5`：重定向跳数上限，每一跳都会重新校验协议白名单，
  跨源跳转剥离 `Authorization` 头。

!!! note "httpx 兜底"
    未安装 `curl_cffi` 时自动降级到 `httpx` 并发出告警——功能可用，
    但指纹隐身能力已禁用。生产环境建议安装 `pip install -e ".[all]"`。

异步场景用 `AsyncFetcher`（纯异步 API 面）；单个 `Fetcher` 实例也暴露
`async_get` / `async_request` 方法，同时服务同步与异步调用方。

## 会话池：指纹随会话轮换

`SessionPool` 池化 `curl_cffi` 会话并让 TLS 指纹随会话轮换，适合长时间
大量抓取——单一指纹更容易被对端聚合计数：

```python
from web_crawler import Fetcher, SessionPool

with SessionPool(max_size=8, strategy="round_robin") as pool:
    with Fetcher(session_pool=pool, impersonate="chrome131") as f:
        for url in urls:
            resp = f.get(url)
        # 多个请求复用池化会话；指纹按 round_robin / random 轮换
```

池行为：LIFO 复用（优先复用刚归还的热会话）、池上限与临时超额、空闲回收、
连续失败淘汰与预热。传入的池会自动绑定 fetcher 的会话工厂（超时 / 校验 /
HTTP2 配置随之生效，`impersonate` 参数退化为池降级时的兜底指纹）。
DNS 钉扎（见[下文](#dns-pinning)）在池化会话上同样生效。

## 代理轮换

`ProxyPool` 支持轮询 / 随机两种策略与按代理失败次数冷却：

```python
from web_crawler import Fetcher, ProxyPool

pool = ProxyPool(
    ["http://u:p@proxy1:8080", "http://proxy2:3128"],
    strategy="round_robin",
    max_failures=3,  # 连续失败 3 次后进入冷却
    cooldown=60,  # 冷却 60 秒后再放回轮换
)
with Fetcher(proxy=pool, impersonate="chrome131") as f:
    resp = f.get("https://example.com")
```

!!! warning "代理是信任边界"
    配置代理后 DNS 解析由代理完成：本库的 host 校验仍在入口生效（对原始
    URL 做静态检查 + DNS 解析复查），但连接的实际目标由代理决定。HTTP
    代理可把流量重定向进内网；SOCKS5 代理不做 host 校验。请只在可信网络
    中使用可信代理，详见
    [SECURITY.md · 代理信任模型](https://github.com/xiaomaozjj666/web-crawler/blob/master/SECURITY.md)。

## JS 渲染与反反爬

三个浏览器后端按「重」的程度递增：

### DynamicFetcher — Playwright/Chromium

渲染 JS 重页面，支持按资源类型屏蔽（加速加载）与按选择器等待：

```python
from web_crawler import DynamicFetcher

with DynamicFetcher(headless=True, wait_selector="div.content") as f:
    resp = f.fetch("https://spa.example.com")
```

首次使用需安装浏览器：`playwright install chromium`。

### StealthyFetcher — 反反爬

在 `DynamicFetcher` 基础上注入指纹补丁 JS、人类化鼠标 / 滚动轨迹，
并对 Cloudflare 挑战做尽力而为的处理：

```python
from web_crawler import StealthyFetcher

with StealthyFetcher(google_search=True, humanize=True) as f:
    resp = f.fetch("https://protected.example.com")
```

### CamoufoxFetcher — 抗指纹 Firefox

基于 [Camoufox](https://camoufox.com/)（抗指纹 Firefox），是 JS 逆向 Agent
的默认浏览器。需要 `pip install -e ".[camoufox]"`。

## 安全边界 {: #safety }

本库公开发布版本**默认开启 SSRF 防护**，这是安全默认值而非能力上限。

- **私网 / 环回 / 链路本地目标一律拒绝**：包括云元数据
  `169.254.169.254`、CGNAT、IPv6 ULA / 链路本地、`localhost` / `*.local`，
  覆盖全部 fetcher 入口与 app 下载器。非点分 IPv4 字面量（`2130706433` /
  `127.1` / `0177.0.0.1` / `0x7f000001`）先归一化再比对。
- **协议白名单**：仅允许 `http` / `https`。`file://` / `ftp://` / `data:`
  等在入口与每一跳重定向处一律拒绝，且不受 Power Mode 影响。
- **DNS 解析复查（默认开启）**：对主机名先做一次解析、逐地址核对拒绝段，
  防 DNS 重绑定型 SSRF；判定结果带 60 秒 TTL 缓存，性能开销可忽略。
  `Fetcher(resolve_hosts=False)` 可关闭（app 层入口始终开启）。

触发拒绝时抛 `UnsafeTargetError`（`WebCrawlerError` 子类）。

### DNS 钉扎与 TOCTOU 根治 {: #dns-pinning }

门禁时校验 DNS 与连接时解析是两次独立查询，低 TTL 记录翻转即构成重绑定。
本库按传输层能力分层处置：

- **httpx 兜底路径**：发送前自行解析并把 URL host 替换为已校验 IP，
  配合 `sni_hostname` 扩展保持 SNI / 证书校验针对原始主机名——
  「校验的地址」就是「连接的地址」，窗口归零（`fetchers/_pin.py`）。
- **curl_cffi 主路径**：发送前逐 IP 复核（绕过判定缓存），把全族已校验 IP
  （A + AAAA 全覆盖）以 `CURLOPT_RESOLVE` 逐请求钉给 libcurl——连接强制
  使用已校验地址，URL / Host 头 / SNI 保持原始主机名。
- **浏览器路径（缓解）**：Playwright 的 DNS 由浏览器完成，无法逐跳插桩，
  为已知边界。

### Power Mode（默认关闭）

在**自己的可信环境**（本机 / 内网 / 开发容器）需要访问内网服务或云元数据时，
可显式开启个人 Power Mode——仅放行 host 校验，`http/https` 白名单始终保留：

```bash
set WEB_CRAWLER_POWER_MODE=1        # Windows
export WEB_CRAWLER_POWER_MODE=1     # Linux / macOS
```

代码内等价：`Fetcher(allow_private_hosts=True)`。

!!! danger "风险提示"
    Power Mode 会绕过 SSRF 防护。公开部署、共享服务器、处理不可信网页时
    请保持默认关闭。详见
    [README · Power Mode](https://github.com/xiaomaozjj666/web-crawler#-power-mode个人全解锁默认关闭)。

## 下一步

- [Spider 爬虫框架指南](spider.md) — 把这些 fetcher 接入爬虫框架
- [AI 能力指南](ai.md) — LLM 辅助抽取与 JS 逆向 Agent
