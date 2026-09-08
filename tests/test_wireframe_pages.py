"""Smoke checks for UI wireframe pages (today / manners / memory / replay)."""

from __future__ import annotations

import compileall
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "pages"
I18N = ROOT / "i18n"
PLUGIN_I18N = ROOT / ".astrbot-plugin" / "i18n"

WIRE_PAGES = ("today", "manners", "memory", "replay")
REQUIRED_FILES = ("_page.json", "index.html", "app.js", "style.css", "plugin_nav.js")


@pytest.mark.parametrize("name", WIRE_PAGES)
def test_wireframe_page_files_exist(name: str):
    root = PAGES / name
    for filename in REQUIRED_FILES:
        assert (root / filename).is_file(), f"missing {name}/{filename}"
    page = json.loads((root / "_page.json").read_text(encoding="utf-8"))
    assert page["title"]["i18n_key"] == f"pages.{name}.title"
    assert page["description"]["i18n_key"] == f"pages.{name}.desc"
    html = (root / "index.html").read_text(encoding="utf-8")
    assert 'href="./base.css"' in html
    assert 'href="./shell.css"' in html
    assert 'href="./plugin_nav.css"' in html
    assert 'id="pluginSideNav"' in html
    assert "../shared/" not in html


def test_shared_assets_exist():
    shared = PAGES / "shared"
    for name in ("base.css", "shell.css", "api.js", "shell.js"):
        assert (shared / name).is_file()
    # Shared base is a console-style reuse, not a new skin.
    base = (shared / "base.css").read_text(encoding="utf-8")
    assert "--bg:" in base and "--cyan:" in base


def test_console_not_replaced_and_has_side_nav():
    html = (PAGES / "console" / "index.html").read_text(encoding="utf-8")
    assert "群聊动态控制台" in html
    assert 'id="channelList"' in html or "UMO" in html or "session" in html.lower()
    assert 'id="pluginSideNav"' in html
    assert "../today/" not in html
    assert "../shared/" not in html


@pytest.mark.parametrize("locale", ("zh-CN", "en-US"))
def test_i18n_keys_for_wireframe_pages(locale: str):
    flat = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
    nested = json.loads((PLUGIN_I18N / f"{locale}.json").read_text(encoding="utf-8"))
    for name in WIRE_PAGES:
        assert flat.get(f"pages.{name}.title")
        assert flat.get(f"pages.{name}.desc")
        assert nested["pages"][name]["title"]
        assert nested["pages"][name]["desc"]


def test_compileall_plugin_sources():
    ok = compileall.compile_dir(str(ROOT / "core"), quiet=1)
    assert ok
    ok_main = compileall.compile_file(str(ROOT / "main.py"), quiet=1)
    assert ok_main


def test_replay_page_is_no_longer_a_stub():
    html = (PAGES / "replay" / "index.html").read_text(encoding="utf-8")
    js = (PAGES / "replay" / "app.js").read_text(encoding="utf-8")
    assert "场景回放尚未实现" not in html
    assert "P2 占位" not in html
    assert 'id="replayRail"' in html
    assert 'apiGet("replay"' in js


def test_read_air_summary_exposes_thermometer_fields():
    from astrbot_plugin_chat_dynamics.core.dashboard import _read_air_summary

    class _P:
        presence_knob = "sensible"
        _metrics = {"speech_withheld": 3}
        decision_gate = None

    data = _read_air_summary(_P(), [])
    assert "thermometer" in data
    assert "confidence" in data
    assert "decisions" in data
    assert data["presence_knob"] == "sensible"
    assert data["empty"] is True
