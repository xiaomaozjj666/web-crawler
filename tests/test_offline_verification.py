"""Regression tests for checking a saved offline package without using the network."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from web_crawler.app import _offline_verify, crawler
from web_crawler.app._offline import OFFLINE_INDEX
from web_crawler.app._offline_verify import verify_offline


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_portable_package_follows_pages_and_stylesheets(tmp_path: Path) -> None:
    original = tmp_path / "original"
    _write(original, OFFLINE_INDEX, '<main><a href="site/start.html">Start</a></main>')
    _write(
        original,
        "offline_pages/site/start.html",
        '<link rel="stylesheet" href="../css/main.css"><a href="next.html">Next</a>'
        '<a href="https://example.com/about">Original site</a><img srcset="data:image/png,a 1x, ../../assets/icon%20large.svg 2x">',
    )
    _write(original, "offline_pages/site/next.html", '<a href="start.html#top">Start</a>')
    _write(original, "offline_pages/css/main.css", '@import "nested/theme.css";')
    _write(
        original,
        "offline_pages/css/nested/theme.css",
        '@import "../main.css"; p{background:url(../../../assets/bg.svg)}',
    )
    _write(original, "assets/icon large.svg", "<svg/>")
    _write(original, "assets/bg.svg", "<svg/>")

    moved = tmp_path / "moved folder"
    shutil.copytree(original, moved)
    initial = verify_offline(moved)
    assert initial["status"] == "passed"
    assert initial["documents_checked"] == 5
    assert initial["remote_resources"] == 0

    (moved / "assets/bg.svg").unlink()
    later = verify_offline(moved)
    assert later["status"] == "failed"
    assert later["missing"] == 1
    assert later["examples"] == [
        {
            "kind": "missing",
            "source": "offline_pages/css/nested/theme.css",
            "detail": "assets/bg.svg",
        }
    ]


def test_external_resources_are_counted_and_navigation_links_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(
        tmp_path,
        "offline_pages/page.html",
        '<link rel="canonical" href="https://example.com/page">'
        '<img src="https://example.com/image.svg?private=data">'
        '<a href="https://example.com/page">Page</a>'
        '<p style="background:url(//example.com/bg.svg)">Page</p>',
    )
    check = verify_offline(tmp_path)
    assert check["status"] == "limited"
    assert check["remote_resources"] == 2
    assert len(check["examples"]) == 2
    assert all(
        item["kind"] == "remote" and item["source"] == "offline_pages/page.html"
        for item in check["examples"]
    )
    assert "private=data" not in json.dumps(check)


def test_nested_document_and_css_syntax_expose_missing_dependencies(tmp_path: Path) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(
        tmp_path,
        "offline_pages/page.html",
        '<link rel="stylesheet" href="main.css">'
        '<svg><image href="missing-image.svg"/></svg>'
        '<svg><rect fill="url(palette.svg#gradient)"></rect></svg>'
        '<iframe src="ignored-fallback.html" srcdoc="&lt;img src=\'missing-frame.svg\'&gt;"></iframe>'
        '<object type="text/html" data="embedded.html"></object>'
        '<style>.one {background: image-set("missing-set.svg" 1x)}</style>',
    )
    _write(tmp_path, "offline_pages/embedded.html", '<img src="missing-embedded.svg">')
    _write(
        tmp_path,
        "offline_pages/main.css",
        '@import /* comment */ "missing-style.css"; @import url("theme");',
    )
    _write(tmp_path, "offline_pages/theme", r'.two {background: url("missing\2e svg")}')
    check = verify_offline(tmp_path)
    assert check["status"] == "failed"
    assert check["documents_checked"] == 5
    assert check["missing"] == 7
    assert {item["detail"] for item in check["examples"]} == {
        "offline_pages/missing-image.svg",
        "offline_pages/missing-frame.svg",
        "offline_pages/missing-set.svg",
        "offline_pages/missing-style.css",
        "offline_pages/missing.svg",
        "offline_pages/palette.svg",
        "offline_pages/missing-embedded.svg",
    }


def test_automatic_navigation_is_an_external_dependency(tmp_path: Path) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(
        tmp_path,
        "offline_pages/page.html",
        '<meta http-equiv="refresh" content="0; url=https://example.com/">',
    )
    check = verify_offline(tmp_path)
    assert check["status"] == "limited" and check["remote_resources"] == 1


def test_encoded_embed_path_and_prefixed_svg_are_checked(tmp_path: Path) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(tmp_path, "offline_pages/page.html", '<embed src="diagram%2esvg">')
    _write(
        tmp_path,
        "offline_pages/diagram.svg",
        '<svg:svg xmlns:svg="http://www.w3.org/2000/svg"><svg:image href="sprite.png"/></svg:svg>',
    )
    check = verify_offline(tmp_path)
    assert check["status"] == "failed"
    assert check["examples"] == [
        {
            "kind": "missing",
            "source": "offline_pages/diagram.svg",
            "detail": "offline_pages/sprite.png",
        }
    ]


def test_package_rejects_outside_paths_without_opening_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bundle"
    outside = _write(tmp_path, "outside.html", "<p>Outside</p>")
    _write(root, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(
        root,
        "offline_pages/page.html",
        f'<a href="../../outside.html">Other</a><iframe src="{outside.as_uri()}"></iframe>'
        '<img src="C:/outside.png">',
    )
    open_path = Path.open

    def assert_inside(path: Path, *args: object, **kwargs: object):
        assert path.is_relative_to(root)
        return open_path(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", assert_inside)
    check = verify_offline(root)
    assert check["status"] == "failed"
    assert check["outside"] == 3
    assert check["documents_checked"] == 2


def test_partial_and_empty_inputs_are_not_certified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert verify_offline(tmp_path)["status"] == "failed"
    _write(tmp_path, OFFLINE_INDEX, "<main>Empty</main>")
    assert verify_offline(tmp_path)["unverified"] == 1
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(tmp_path, "offline_pages/page.html", '<base href="https://example.com/"><p>Page</p>')
    assert verify_offline(tmp_path)["status"] == "limited"
    assert verify_offline(tmp_path, should_stop=lambda: True)["documents_checked"] == 0
    monkeypatch.setattr(_offline_verify, "_DOCUMENT_BYTES", 8)
    assert "读取上限" in verify_offline(tmp_path)["examples"][0]["detail"]


def test_limits_report_unchecked_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(tmp_path, "offline_pages/page.html", '<img src="icon.svg"><img src="other.svg">')
    _write(tmp_path, "offline_pages/icon.svg", "<svg/>")
    _write(tmp_path, "offline_pages/other.svg", "<svg/>")
    monkeypatch.setattr(_offline_verify, "_REFERENCES", 2)
    check = verify_offline(tmp_path)
    assert check["status"] == "limited"
    assert check["local_references"] == 2
    assert "引用检查数上限" in check["examples"][0]["detail"]

    monkeypatch.setattr(_offline_verify, "_REFERENCES", 1)
    truncated = verify_offline(tmp_path)
    assert truncated["status"] == "limited" and truncated["documents_checked"] == 1


def test_reading_corrupt_documents_marks_verification_limited(tmp_path: Path) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(
        tmp_path,
        "offline_pages/page.html",
        '<body><!-- decorative comment --><a href="#anchor">Anchor</a>'
        '<a href="?view=large">Same document</a><link rel="stylesheet" href="malformed.css">'
        '<iframe src="invalid.html"></iframe></body>',
    )
    _write(tmp_path, "offline_pages/malformed.css", "@import url(())")
    (tmp_path / "offline_pages/invalid.html").write_bytes(b"\xffbroken")
    check = verify_offline(tmp_path)
    assert check["status"] == "limited"
    assert check["documents_checked"] == 2
    assert {item["source"] for item in check["examples"]} == {
        "offline_pages/malformed.css",
        "offline_pages/invalid.html",
    }


def test_unresolved_and_malformed_references_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(tmp_path, "offline_pages/page.html", '<img src="https://["><img src="unresolved.svg">')
    resolve = Path.resolve

    def resolve_with_error(path: Path, *args: object, **kwargs: object) -> Path:
        if path.name == "unresolved.svg":
            raise OSError("filesystem refused lookup")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_error)
    check = verify_offline(tmp_path)
    assert check["status"] == "limited"
    assert [item["detail"] for item in check["examples"]] == ["引用格式无效", "无法解析文件位置"]


def test_unrecognized_refresh_syntax_is_not_a_pass(tmp_path: Path) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    _write(
        tmp_path,
        "offline_pages/page.html",
        '<meta http-equiv="refresh" content=".5; url=other.html">',
    )
    check = verify_offline(tmp_path)
    assert check["status"] == "limited" and check["unverified"] == 1


def test_missing_reference_precedes_external_examples(tmp_path: Path) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    externals = '<img src="https://example.com/picture">' * 30
    _write(tmp_path, "offline_pages/page.html", externals + '<img src="missing.svg">')
    check = verify_offline(tmp_path)
    assert check["remote_resources"] == 30
    assert check["examples"][0]["kind"] == "missing"
    assert len(check["examples"]) == 20


def test_cli_verifies_package_and_reports_nonzero_after_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, OFFLINE_INDEX, '<a href="page.html">Page</a>')
    page = _write(tmp_path, "offline_pages/page.html", "<p>Page</p>")
    monkeypatch.setattr(sys, "argv", ["web-crawler", "--verify-offline", str(tmp_path), "--json"])
    with pytest.raises(SystemExit) as accepted:
        crawler.main()
    assert accepted.value.code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"

    page.unlink()
    with pytest.raises(SystemExit) as rejected:
        crawler.main()
    assert rejected.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    monkeypatch.setattr(sys, "argv", ["web-crawler", "--verify-offline", str(tmp_path)])
    with pytest.raises(SystemExit) as text_result:
        crawler.main()
    assert text_result.value.code == 1
    assert "offline_pages/page.html" in capsys.readouterr().out


def test_cli_rejects_combining_check_with_network_crawl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["web-crawler", "--verify-offline", str(tmp_path), "--url", "https://example.com"],
    )
    with pytest.raises(SystemExit) as result:
        crawler.main()
    assert result.value.code == 2
    assert "standalone check" in capsys.readouterr().err
