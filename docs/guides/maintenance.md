# 本地运行产物与数据保留

本文约定本仓库在本机运行时产生的数据**留什么、留多久、怎么安全清理**。
这些文件都在 `.gitignore` 内，不随仓库分发，也不参与测试与打包。

## `crawler_data.db`（Web UI 任务库）

**是什么**：Web UI / `crawler-ui` 的任务历史与采集结果库，SQLite + WAL 模式。
默认落在**启动时的当前工作目录** `crawler_data.db`（同目录还会有
`-wal` / `-shm` 两个伴生文件），可用 `CRAWLER_DB_PATH` 指向别处。表结构：
`tasks`（任务）、`results`（资源结果）、`page_extractions`（正文抽取）、
`resource_sources` / `resource_dependencies`（资源引用关系），后四张表都通过
`ON DELETE CASCADE` 挂在 `tasks` 上。

**保留策略（默认值，可按需调整）**

| 数据 | 保留策略 |
| --- | --- |
| 任务库文件 | 不随仓库分发；本机长期保留，体积通常 < 1 MB |
| 终态任务（`success` / `error` / `cancelled`） | 建议保留 **90 天**，之后按需清理 |
| 在途任务（`running` / `paused` / `cancelling`） | **永不自动清理**（可能仍有 worker 写入或等待恢复） |

**安全清理方式**：只做库内 `DELETE`，不要手工删文件。

```python
from web_crawler.app import db

# 删掉 90 天前已结束的任务及其结果（在途任务不受影响）
deleted = db.prune_finished_tasks(older_than_days=90)

# 需要立刻回收磁盘空间时（VACUUM 需独占访问，只在 UI 已停止/空闲时执行）
deleted = db.prune_finished_tasks(90, vacuum=True)
```

清理边界（实现与测试均已保证）：

- 只匹配 `success` / `error` / `cancelled` 三种终态；UI 正在跑任务时执行
  清理**不会**删掉在途记录，WAL + `busy_timeout=5000` 允许与运行中的任务并存。
- 判定时间取 `finished_at`，缺失时回退 `created_at`；保留天数必须为正。
- 关联的结果/正文/资源关系随任务级联删除，不会留下孤儿行。
- `vacuum=True` 需要独占锁，与正在写库的进程并发时可能报
  `database is locked`——日常清理用默认的纯 `DELETE` 即可，页空间会被后续写入复用。
- **不要**手工删除 `crawler_data.db` / `-wal` / `-shm`：运行中的进程持有连接，
  Windows 上会因占用失败，POSIX 上会让写入落到不可见的 inode，还可能丢失
  尚未 checkpoint 的事务。确需整库重置时，先停掉 UI，再删除这三个文件
  （下次启动会自动重建，历史全部丢失）。

## `.research/`（本地调研产物）

**是什么**：调研类任务的抓取产物（竞品文档 HTML / 正文 txt / 结构化
JSON·JSONL·CSV），仅供复盘，不参与运行。

**保留策略**

| 条件 | 动作 |
| --- | --- |
| 展开目录 | 单个调研任务超过 **30 天**或累计超过 **20 MB** 时归档 |
| 归档形式 | `.research/archive/research-artifacts-<日期>.zip` + `.research/MANIFEST.md` |
| 归档保留 | **1 年**；过期且确认无追溯需求后删除整个 `.research/` |

`MANIFEST.md` 记录顶层目录/文件类型清单、体积、时间跨度、归档 SHA-256 与
恢复命令，保证压缩后仍可追溯来源。

**归档方式**（幂等，先校验再删原件）：

```bash
cd .research
python - <<'PY'
import zipfile, pathlib
files = [p for p in pathlib.Path('.').rglob('*')
         if p.is_file() and 'archive' not in p.parts and p.name != 'MANIFEST.md']
out = pathlib.Path('archive') / 'research-artifacts-2026-11-26.zip'
out.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for p in files:
        zf.write(p, p.as_posix())
assert zipfile.ZipFile(out).testzip() is None   # 完整性校验
PY
```

**本次执行记录（2026-11-26）**：223 个文件、18.49 MB → 归档
`research-artifacts-2026-11-26.zip`（3.59 MB，SHA-256
`c769993c3fb9bce6583ef293e002e30c62c0dfd2333dead1af6e4c2ea65ffedd`），
展开目录已删除，恢复方式见 `MANIFEST.md`。

## 其它本地产物

| 目录 | 内容 | 策略 |
| --- | --- | --- |
| `site/` | `mkdocs build` 输出（约 10 MB） | 构建产物，可随时删除，重新构建即恢复 |
| `crawler_output/` | 抓取结果目录 | 按任务价值自行保留；确认不需要后删除 |
| `.checkpoints/` | 逆向 Agent 断点 | 任务结束后可按需删除 |
| `.screenshots/`、`reverse_screenshots/` | 调试截图 | 可随时删除 |
| `.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`.coverage` | 工具缓存 | 可随时删除，下一次运行自动重建 |

## 启动器与 Power Mode

`启动爬虫-满血版.cmd` 是**本机专用**的启动封装（已由 `.git/info/exclude`
排除，不在仓库内）。它默认走安全模式：私网 / 环回 / 链路本地 / 云元数据目标
一律拒绝；只有显式传 `--power` 才设置 `WEB_CRAWLER_POWER_MODE=1`，并打印
醒目警告与 3 秒倒计时。安全模式下会主动清空父进程可能遗留的
`WEB_CRAWLER_POWER_MODE`，避免"继承式"越权。

**不适合入库的原因**：它绑定本机 Python 版本探测与 Power Mode 开关，随仓库
分发会让使用者在不知情时获得内网访问能力；仓库统一以
`python -m web_crawler.app.ui --open` 为入口，Power Mode 必须由使用者显式开启。
环境变量用法与风险见 [隐身与网络](stealth.md) 的 Power Mode 小节与
[README · Power Mode](https://github.com/xiaomaozjj666/web-crawler#-power-mode个人全解锁默认关闭)。
