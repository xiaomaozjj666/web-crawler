# 贡献指南

感谢你对 web-crawler 的关注！欢迎以任何形式参与贡献：提交问题、修复 Bug、新增功能、改进文档。

## 开发环境搭建

1. 克隆仓库并进入目录：

   ```bash
   git clone https://github.com/xiaomaozjj666/web-crawler.git
   cd web-crawler
   ```

2. 创建并激活虚拟环境（要求 Python 3.10+）：

   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux / macOS
   source .venv/bin/activate
   ```

3. 安装开发依赖（含测试、静态检查、类型检查工具链）：

   ```bash
   pip install -e ".[dev]"
   ```

   如需运行涉及浏览器渲染或 TLS 隐身的测试，另装 `.[all]` / `.[camoufox]` 可选组。

4. （可选）安装 pre-commit 钩子，提交前自动执行 lint 与格式化：

   ```bash
   pip install pre-commit
   pre-commit install
   ```

## 开发工作流

### 代码质量要求

提交前请确保以下三项全部通过（CI 会执行同样检查）：

```bash
ruff check .                     # 静态检查，零错误
mypy src/web_crawler app         # 类型检查，零错误
pytest -m "not slow"             # 测试全绿（跳过慢速集成测试）
```

- 新增或修改的公开 API 需带 docstring（项目以中文为主）。
- 新功能必须附带测试；修 Bug 请先写一个能复现问题的失败测试。
- ruff 的 ignore 列表（见 `pyproject.toml`）中每条例外都写明了理由，新增例外同样需要注释说明。

### 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 格式，与仓库现有历史保持一致：

```
<type>: <简要描述>

feat: Spider 支持按域名并发隔离
fix: 重定向跨域时未剥离 Authorization 头
docs: 补充 AdaptiveSelector 使用示例
refactor: ...
test: ...
chore: ...
```

### 变更日志

面向用户的变更（新功能、行为变化、Bug 修复、安全修复）请同步更新 `CHANGELOG.md` 的 `[Unreleased]` 小节，遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 分类（Added / Changed / Fixed / Removed / Security）。

### 提交 Pull Request

1. 从 `master` 切出特性分支（如 `feat/domain-concurrency`）。
2. 完成开发，确保上述三项质量检查通过。
3. PR 描述中说明动机、改动点与验证方式；关联相关 Issue。
4. CI 通过后等待 review，通过后合并。

## 测试说明

- 测试套件位于 `tests/`，运行时依赖 `conftest.py` 提供的本地 HTTP 服务器 fixture，无需访问外网。
- 标记为 `@pytest.mark.slow` 的慢速测试（如 Camoufox 端到端套件）默认被 CI 排除，本地可用 `pytest -m slow` 单独运行。
- 覆盖率门禁由 `pyproject.toml` 的 `[tool.coverage.report] fail_under` 控制，请勿让覆盖率下降。

## 安全相关贡献

涉及安全边界的改动（SSRF 防护、URL 校验、本地服务鉴权、Power Mode 行为）请额外谨慎：

- 参考 `docs/architecture.md` 中的安全设计说明。
- 默认拒绝、显式放开是本项目安全机制的基本原则。
- 若你发现了安全漏洞而非想修复它，请按 [SECURITY.md](SECURITY.md) 的流程私密披露，不要开公开 Issue。

## 行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
