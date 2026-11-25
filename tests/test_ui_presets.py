"""控制台采集预设：预设与冲突提示引用的字段必须真实存在于采集表单。

预设脚本按 name 查找表单控件，字段改名后会静默失效，这里做静态防线。
"""

from __future__ import annotations

import re

from web_crawler.app import _ui_helpers as ui


def _crawler_form_names() -> set[str]:
    page = ui.PAGE
    start = page.index('<form id="crawler-form">')
    end = page.index("</form>", start)
    return set(re.findall(r'name="([a-z_]+)"', page[start:end]))


def _preset_script() -> str:
    page = ui.PAGE
    start = page.index("var BASE = {")
    return page[start : page.index("window.__crawlPresetSync = sync", start)]


def test_preset_buttons_cover_all_presets():
    script = _preset_script()
    declared = set(re.findall(r"^\s{6}(\w+): \{ checks:", script, re.MULTILINE))
    buttons = set(re.findall(r'data-preset="(\w+)"', ui.PAGE))
    assert declared == buttons == {"single", "mirror", "markdown", "video"}


def test_preset_fields_exist_in_form():
    names = _crawler_form_names()
    script = _preset_script()
    base = script[: script.index("};")]
    referenced = set(re.findall(r"(\w+): (?:true|false)", script)) | set(
        re.findall(r"(\w+): (?:true|false)", base)
    )
    moot = re.search(r"MOOT_WHEN_LIST_ONLY = \[(.*?)\]", script).group(1)
    referenced |= set(re.findall(r"'(\w+)'", moot))
    missing = referenced - names
    assert not missing, f"预设引用了表单中不存在的字段：{sorted(missing)}"


def test_single_preset_matches_form_defaults():
    """首次打开页面时应自动高亮「单页资源」：BASE 必须与表单默认勾选一致。"""
    page = ui.PAGE
    form = page[page.index('<form id="crawler-form">') : page.index("</form>")]
    base = _preset_script()
    base = base[: base.index("};")]
    for name, value in re.findall(r"(\w+): (true|false)", base):
        tag = re.search(rf'<input type="checkbox" name="{name}"[^>]*>', form)
        assert tag, name
        assert ("checked" in tag.group(0)) == (value == "true"), name
