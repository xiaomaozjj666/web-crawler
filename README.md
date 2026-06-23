# 网页资源采集器 (Web Resource Crawler)

> 合规的网页资源下载工具 — 自动发现并下载网页中的图片、CSS、JS、视频、字体等资源。

## ✨ 特性

| 特性 | 说明 |
|------|------|
| 🚀 **并发下载** | 多线程并行下载，默认 8 线程，可自定义 |
| 🛡️ **自适应限速** | 自动处理 `429 Too Many Requests` + `Retry-After` |
| 🔍 **内容去重** | SHA256 内容哈希去重，相同文件不重复下载 |
| 📋 **多格式清单** | CSV / JSON / JSONL 三种格式的资源和失败清单 |
| 📄 **Sitemap 发现** | 自动解析 `/sitemap.xml` 发现页面 |
| 🔄 **断点续传** | HTTP Range 断点续传 + 爬取状态持久化 |
| 🗂️ **自动分类** | 按类型（图片/CSS/JS/视频/字体）自动归类存放 |
| 🎨 **离线页面** | 下载资源后用本地路径重写 HTML 实现离线浏览 |
| 🧹 **遮挡层移除** | 自动去除弹窗、遮罩、广告、付费墙等遮挡元素 |
| 📊 **智能提取** | 自动提取 OG 元数据、标题、正文、结构化数据 |
| 🔐 **AES 解密** | 可选支持加密 m3u8 分片的 AES-128 解密 |
| 🌐 **Web UI** | 内置浏览器控制面板，可视化操作 |

## 🚀 快速开始

### 安装

```bash
git clone <repo-url>
cd web-crawler
pip install -r requirements.txt
# 可选：AES 解密支持
pip install pycryptodome
```

### 命令行使用

```bash
# 最基本用法：列出页面资源
python app/crawler.py --url https://example.com

# 并发下载（16 线程），扫描 CSS 内资源
python app/crawler.py --url https://example.com --workers 16 --include-css-urls

# 扫描站内页面 + Sitemap
python app/crawler.py --url https://example.com --sitemap --same-domain --max-pages 50

# 下载视频资源
python app/crawler.py --url https://example.com/video --video-mode --expand-playlists

# 智能提取 + 正文提取
python app/crawler.py --url https://example.com --smart-extract --extract-text

# 保存/加载配置
python app/crawler.py --url https://example.com --save-config my_project.json
python app/crawler.py --load-config my_project.json
```

### Web UI 模式

```bash
python app/ui.py
# 或使用启动脚本
start.bat
```

打开浏览器访问 `http://127.0.0.1:8765`

## 📖 详细参数

### 基本参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--url` | 必填 | 起始页面 URL |
| `--out` | `D:\XIAOMAO\爬虫` | 输出目录 |
| `--workers` | `8` | 并发下载线程数 |
| `--delay` | `0.0` | 请求间隔（秒，自适应域名） |
| `--timeout` | `20` | 请求超时（秒） |
| `--retries` | `1` | 失败重试次数 |
| `--max-bytes` | `0` | 单文件大小上限（0=不限制） |
| `--encoding` | `auto` | 强制文本编码（如 utf-8, gbk） |
| `--user-agent` | `Mozilla/5.0 ...` | HTTP User-Agent |

### 页面发现

| 参数 | 说明 |
|------|------|
| `--crawl-pages` | 跟随站内页面链接 |
| `--max-pages` | 最大扫描页面数（默认 1） |
| `--same-domain` | 仅处理同域名 URL |
| `--sitemap` | 从 `/sitemap.xml` 发现页面 |
| `--resume-crawl` | 断点续爬 |

### 资源筛选

| 参数 | 说明 |
|------|------|
| `--include-css-urls` | 下载 CSS 中的 `url()` 和 `@import` 资源 |
| `--video-mode` | 生成视频资源清单 |
| `--video-only` | 仅处理视频相关资源 |
| `--expand-playlists` | 展开 m3u8/mpd 播放清单 |
| `--block-keyword` | 过滤含指定关键词的 URL（可重复） |
| `--respect-robots` | 遵守 robots.txt |
| `--dedup` | SHA256 内容去重 |

### 输出控制

| 参数 | 说明 |
|------|------|
| `--list-only` | 仅生成清单，不下载文件 |
| `--organize` | 按类型/页面标题自动分类存放 |
| `--rewrite-html` | 生成离线 HTML（资源路径重写为本地） |
| `--strip-overlays` | 移除遮挡/弹窗/广告元素 |
| `--resume` | 断点续传（HTTP Range） |
| `--decrypt` | AES-128 解密加密的 m3u8 分片 |

### 数据提取

| 参数 | 说明 |
|------|------|
| `--smart-extract` | 提取 OG 元数据、标题、结构化数据 |
| `--extract-text` | 提取可读正文内容 |
| `--save-config` | 保存配置到 JSON 文件 |
| `--load-config` | 从 JSON 文件加载配置 |

## 📂 输出结构

```
输出目录/
├── assets/                    # 下载的资源文件
│   ├── site.example.com/      # 按域名组织
│   │   ├── style.css
│   │   └── ...
│   └── ...
├── pages/                     # 原始 HTML 页面
├── offline_pages/             # 离线浏览 HTML（路径已重写）
├── extracted_text/            # 提取的正文（--extract-text）
├── resources_manifest.csv     # 资源清单 CSV
├── resources_manifest.json    # 资源清单 JSON
├── resources_manifest.jsonl   # 资源清单 JSONL
├── video_manifest.csv         # 视频资源清单（--video-mode）
├── failed_resources.csv       # 失败资源清单
├── run_report.json            # 运行报告
├── summary.txt                # 文本摘要
└── extracted_data.json        # 提取的结构化数据（--smart-extract）
```

## ⚠️ 合规说明

本工具仅用于下载**公开可访问**或**已获授权**的网页资源。它不会尝试绕过付费墙、登录验证、DRM、签名校验等访问控制机制。使用者需自行确保使用方式符合目标网站的条款和适用法律法规。

## 📝 许可

MIT License
