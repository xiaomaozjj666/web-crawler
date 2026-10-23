"""startproject 脚手架测试：项目名校验、目录守卫、文件生成与 CLI 入口。

全部离线：create_project 只写 tmp_path，无网络、无真实抓取。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web_crawler.app import startproject as sp
from web_crawler.exceptions import ConfigError


class TestValidateProjectName:
    """项目名校验：合法标识符放行，非法/关键字/路径穿越拒绝。"""

    @pytest.mark.parametrize(
        "name",
        ["demo", "demo_proj", "Demo2", "_private", "my_crawler", "中文项目"],
    )
    def test_valid_names(self, name: str) -> None:
        """合法 Python 标识符（含 Unicode 字母）通过。"""
        assert sp.validate_project_name(name) == name

    @pytest.mark.parametrize(
        "name",
        ["", "   ", "1abc", "has-dash", "has dot", "has/slash", "has\\slash", "../evil", "a..b"],
    )
    def test_invalid_names(self, name: str) -> None:
        """非标识符（含路径穿越形态）抛 ConfigError。"""
        with pytest.raises(ConfigError):
            sp.validate_project_name(name)

    @pytest.mark.parametrize("name", ["class", "import", "None", "lambda"])
    def test_keywords_rejected(self, name: str) -> None:
        """Python 关键字不能当项目名。"""
        with pytest.raises(ConfigError, match="关键字"):
            sp.validate_project_name(name)

    def test_strips_surrounding_whitespace(self) -> None:
        """首尾空白被剥掉后再校验。"""
        assert sp.validate_project_name("  demo  ") == "demo"


class TestBuildProjectFiles:
    """文件映射纯函数：内容真实可用（语法可编译、玩法齐全）。"""

    def test_file_set(self) -> None:
        """生成的相对路径集合恰为约定的 4 个文件。"""
        files = sp.build_project_files("demo")
        assert set(files) == {
            Path("spiders") / "example_spider.py",
            Path("main.py"),
            Path("requirements.txt"),
            Path("README.md"),
        }

    def test_spider_template_shows_both_styles(self) -> None:
        """spider 模板同时演示基础 Spider 与 CrawlSpider 规则玩法。"""
        content = sp.build_project_files("demo")[Path("spiders") / "example_spider.py"]
        for token in ("class TitleSpider(Spider)", "class SiteCrawlSpider(CrawlSpider)"):
            assert token in content
        assert "start_urls" in content
        assert "LinkExtractor(allow=" in content
        assert "Rule(" in content
        assert "callback=" in content
        # 目标站点用稳定的教学站点组合
        assert "https://example.com/" in content
        assert "https://www.iana.org/" in content

    def test_main_template_assembles_explicitly(self) -> None:
        """main.py 模板用构造参数风格组装 Fetcher + feed_output。"""
        content = sp.build_project_files("demo")[Path("main.py")]
        assert "Fetcher(impersonate=" in content
        assert "feed_output=" in content
        assert "python main.py" in content
        assert "settings.py" in content  # 明示与 Scrapy 全局配置的差异

    def test_requirements_one_line(self) -> None:
        """requirements.txt 是 stealth-web-crawler[all] 一行。"""
        content = sp.build_project_files("demo")[Path("requirements.txt")]
        assert content.strip() == "stealth-web-crawler[all]"

    def test_readme_three_steps_and_project_name(self) -> None:
        """README 含 3 步上手说明，项目名注入标题与目录结构。"""
        content = sp.build_project_files("my_proj")[Path("README.md")]
        assert "# my_proj" in content
        assert "pip install" in content
        assert "python main.py" in content
        assert "start_urls" in content

    def test_python_templates_compile(self) -> None:
        """两个 .py 模板是语法合法的 Python 源（可被解释器接受）。"""
        files = sp.build_project_files("demo")
        for rel in (Path("spiders") / "example_spider.py", Path("main.py")):
            compile(files[rel], str(rel), "exec")

    def test_no_unsubstituted_placeholder(self) -> None:
        """模板渲染后不留 ``$`` 占位符。"""
        for content in sp.build_project_files("demo").values():
            assert "$" not in content


class TestCreateProject:
    """目录守卫与写盘行为。"""

    def test_creates_expected_tree(self, tmp_path: Path) -> None:
        """生成 spiders/ 目录与 4 个文件，返回项目根路径。"""
        root = sp.create_project("demo", base_dir=tmp_path)
        assert root == tmp_path / "demo"
        assert (root / "spiders" / "example_spider.py").is_file()
        assert (root / "main.py").is_file()
        assert (root / "requirements.txt").is_file()
        assert (root / "README.md").is_file()

    def test_writes_utf8_and_lf(self, tmp_path: Path) -> None:
        """所有文件 UTF-8 可解码（含中文注释）且行尾为 LF。"""
        root = sp.create_project("demo", base_dir=tmp_path)
        for path in root.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                assert b"\r" not in raw, f"{path.name} 应为 LF 行尾"
                raw.decode("utf-8")  # 解码失败即测试失败

    def test_existing_nonempty_dir_rejected(self, tmp_path: Path) -> None:
        """目标目录已存在且非空 → ConfigError，且原内容不被触碰。"""
        target = tmp_path / "demo"
        target.mkdir()
        (target / "keep.txt").write_text("原有数据", encoding="utf-8")
        with pytest.raises(ConfigError, match="拒绝覆盖"):
            sp.create_project("demo", base_dir=tmp_path)
        assert (target / "keep.txt").read_text(encoding="utf-8") == "原有数据"
        assert not (target / "main.py").exists()

    def test_existing_empty_dir_allowed(self, tmp_path: Path) -> None:
        """目标目录已存在但为空 → 允许生成。"""
        target = tmp_path / "demo"
        target.mkdir()
        root = sp.create_project("demo", base_dir=tmp_path)
        assert (root / "main.py").is_file()

    def test_existing_file_rejected(self, tmp_path: Path) -> None:
        """同名路径是文件而非目录 → ConfigError。"""
        (tmp_path / "demo").write_text("占位", encoding="utf-8")
        with pytest.raises(ConfigError, match="不是目录"):
            sp.create_project("demo", base_dir=tmp_path)

    def test_invalid_name_rejected_before_touching_disk(self, tmp_path: Path) -> None:
        """非法项目名在写盘前即失败，不产生任何目录。"""
        with pytest.raises(ConfigError):
            sp.create_project("1bad", base_dir=tmp_path)
        assert not (tmp_path / "1bad").exists()

    def test_default_base_dir_is_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """base_dir 缺省时在当前目录生成。"""
        monkeypatch.chdir(tmp_path)
        root = sp.create_project("demo")
        assert root == tmp_path / "demo"
        assert (root / "main.py").is_file()


class TestMain:
    """CLI 入口：退出码、指引输出与 argparse 行为。"""

    def test_success_prints_next_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """成功生成退出 0，stdout 打印 cd / pip install / python main.py 指引。"""
        monkeypatch.chdir(tmp_path)
        assert sp.main(["demo"]) == 0
        out = capsys.readouterr().out
        assert "cd demo" in out
        assert "pip install" in out
        assert "python main.py" in out
        assert (tmp_path / "demo" / "main.py").is_file()

    def test_invalid_name_returns_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """非法项目名退出 2，stderr 给出中文错误。"""
        monkeypatch.chdir(tmp_path)
        assert sp.main(["1bad"]) == 2
        assert "错误" in capsys.readouterr().err

    def test_nonempty_target_returns_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同名非空目录二次生成退出 2。"""
        monkeypatch.chdir(tmp_path)
        assert sp.main(["demo"]) == 0
        assert sp.main(["demo"]) == 2

    def test_missing_argument_exits_2(self) -> None:
        """缺位置参数 → argparse 报错退出 2。"""
        with pytest.raises(SystemExit) as exc_info:
            sp.main([])
        assert exc_info.value.code == 2

    def test_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--help 正常打印用法并退出 0。"""
        with pytest.raises(SystemExit) as exc_info:
            sp.main(["--help"])
        assert exc_info.value.code == 0
        assert "startproject" in capsys.readouterr().out
