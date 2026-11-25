"""Batch 4 regression tests: series_panels legacy conflicts/epoch guard, offline verification, theme sync, and CI hard gates."""

from __future__ import annotations

import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics import series_panels
from scripts import check_series_ci, run_checks, sync_series_assets, verify_series_integration

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_conflicts_cover_all_consolidated_series_plugins():
    by_id = {legacy_id: owner for legacy_id, _label, owner in series_panels.LEGACY_CONFLICTS}
    assert by_id["astrbot_plugin_self_learning"] == "astrbot_plugin_dynamics_learning"
    assert by_id["steam_status_monitor_V3"] == "astrbot_plugin_dynamics_games"
    assert "steam_status_monitor_V3" in series_panels.LEGACY_GAME_IDS
    assert by_id["astrbot_plugin_livingmemory"] == "astrbot_plugin_dynamics_memory"
    assert by_id["astrbot_plugin_worldbook"] == "astrbot_plugin_dynamics_world"
    assert by_id["astrbot_plugin_dynamic_life_state"] == "astrbot_plugin_dynamics_world"
    assert by_id["astrbot_plugin_stealer"] == "astrbot_plugin_visual_media"
    assert by_id["astrbot_plugin_gemini_image_generation"] == "astrbot_plugin_visual_media"
    assert by_id["astrbot_plugin_meme_manager"] == "astrbot_plugin_visual_media"

    # End-to-end presence() detects newly added legacy conflict
    rows = [
        SimpleNamespace(name="astrbot_plugin_dynamics_games", activated=True, star_cls=object()),
        SimpleNamespace(name="steam_status_monitor_V3", activated=True, star_cls=object()),
        SimpleNamespace(name="astrbot_plugin_dynamics_world", activated=True, star_cls=object()),
        SimpleNamespace(name="astrbot_plugin_worldbook", activated=True, star_cls=object()),
    ]
    ctx = SimpleNamespace(get_all_stars=lambda: rows, get_config=lambda: {})
    snap = series_panels.presence(ctx, "astrbot_plugin_chat_dynamics", force_refresh=True)
    assert snap["legacy_conflict"] is True
    conflict_ids = {item["id"] for item in snap["legacy_conflicts"]}
    assert {"steam_status_monitor_V3", "astrbot_plugin_worldbook"} <= conflict_ids


@pytest.mark.asyncio
async def test_series_panels_status_skips_stale_cache_write_on_concurrent_invalidation(monkeypatch):
    calls = 0
    ctx = SimpleNamespace(get_config=lambda: {"dashboard": {"username": "admin"}})

    def _presence_with_concurrent_invalidation(context, identity, *, force_refresh=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Simulate another plugin invalidating presence cache mid-flight inside to_thread
            series_panels.invalidate_presence_cache(context=context)
        return {"schema_version": 1, "current": identity, "plugins": [], "seq": calls}

    monkeypatch.setattr(series_panels, "presence", _presence_with_concurrent_invalidation)
    monkeypatch.setattr(series_panels, "json_response", lambda data, status_code=200: (status_code, data))

    async def _fake_admin(context, req, *, check_csrf=False):
        return "admin"

    monkeypatch.setattr(series_panels, "require_series_admin", _fake_admin)
    monkeypatch.setattr(
        series_panels,
        "request",
        SimpleNamespace(method="GET", query={}, headers={}),
    )

    owner = SimpleNamespace(context=ctx)
    panel = series_panels.SeriesPanels(owner, "astrbot_plugin_chat_dynamics")
    series_panels.invalidate_presence_cache(context=ctx)

    code1, body1 = await panel.status()
    assert code1 == 200 and body1["data"]["seq"] == 1
    # Because epoch moved mid-flight, call #1 was NOT cached under the new epoch -> call #2 must recompute!
    code2, body2 = await panel.status()
    assert code2 == 200 and body2["data"]["seq"] == 2
    assert calls == 2
    # Call #2 completed with stable epoch -> call #3 hits cache!
    code3, body3 = await panel.status()
    assert code3 == 200 and body3["data"]["seq"] == 2
    assert calls == 2


def test_run_checks_includes_all_rc6_to_rc10_and_governance_suites():
    expected = {
        "test_r1_r2_r3_optimizations",
        "test_provider_budget",
        "test_private_chat_chain",
        "test_poke_optimization",
        "test_reply_modes",
        "test_reply_prompt_assembly",
        "test_console_telemetry_templates_ui",
        "test_static_contracts",
        "test_web_api_auth_csrf",
        "test_series_panels",
        "test_narrative_storage_p0_3",
        "test_batch3_hotpath_cache_budget",
        "test_batch4_ci_and_series_gates",
    }
    backend_set = set(run_checks.BACKEND)
    assert expected <= backend_set
    for name in run_checks.BACKEND:
        assert (REPO_ROOT / "tests" / f"{name}.py").is_file(), f"missing test file for {name}"


@pytest.mark.asyncio
async def test_verify_series_integration_runtime_contracts_work_offline(tmp_path):
    # Verify runtime contracts execute cleanly against our repo checkout (offline, without AstrBot).
    errors = await verify_series_integration.check_cross_plugin_runtime_contracts(REPO_ROOT.parent)
    assert errors == []


def test_companion_theme_js_is_null_safe_and_synced_by_sync_series_assets(tmp_path):
    canonical_theme = REPO_ROOT / "pages" / "shared" / "series-theme.js"
    assert canonical_theme.is_file()
    raw = canonical_theme.read_bytes()
    assert b"\r\n" not in raw, "canonical series-theme.js must use LF line endings"
    text = raw.decode("utf-8")
    # Null guards for both standard and dynamics_world DOM conventions
    assert "if (standardToggle)" in text
    assert "if (standardLabel)" in text
    assert "if (worldToggle)" in text
    assert "if (worldLabel)" in text
    assert "if (statusNode)" in text

    # Test sync_workspace + check_workspace on isolated temp workspace
    chat = tmp_path / "astrbot_plugin_chat_dynamics"
    games = tmp_path / "astrbot_plugin_dynamics_games"
    world = tmp_path / "astrbot_plugin_dynamics_world"
    (chat / "pages/shared").mkdir(parents=True)
    (games / "pages/games").mkdir(parents=True)
    (world / "pages/world").mkdir(parents=True)

    for name in sync_series_assets.SERIES_WEB_FILES:
        (chat / "pages/shared" / name).write_text(f"/* {name} */\n", encoding="utf-8")
    (chat / "pages/shared/series-theme.js").write_bytes(raw)
    for name in sync_series_assets.SERIES_PY_FILES:
        (chat / name).write_text(f"# {name}\n", encoding="utf-8")

    sync_series_assets.sync_workspace(tmp_path)
    assert sync_series_assets.check_workspace(tmp_path) == []
    assert (games / "pages/games/theme.js").read_bytes() == raw
    assert (world / "pages/world/theme.js").read_bytes() == raw

    # Drift in companion theme.js is detected by check_workspace
    (world / "pages/world/theme.js").write_text("// drifted\n", encoding="utf-8")
    errors = sync_series_assets.check_workspace(tmp_path)
    assert any("astrbot_plugin_dynamics_world/pages/world/theme.js" in err for err in errors)

    # Node.js VM superset verification (covers noRoot, noDataset, readyState='complete', ?theme=dark, null storage event, and #dwTheme)
    node = shutil.which("node")
    if node:
        chk = subprocess.run([node, "--check", str(canonical_theme)], capture_output=True, text=True, check=False)
        assert chk.returncode == 0, chk.stderr
        harness = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
function makeEl(id, withQuery) {
  const attrs = {};
  const listeners = {};
  const el = {
    id, attrs, textContent: '', onclick: null,
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k]; },
    addEventListener(t, fn) { listeners[t] = listeners[t] || []; listeners[t].push(fn); },
    click() { if (el.onclick) el.onclick(); for (const fn of listeners.click || []) fn(); },
  };
  if (withQuery) el.querySelector = (sel) => withQuery[sel] || null;
  return el;
}
function run(opts) {
  const elements = {};
  if (opts.hasToggle) elements['theme-toggle'] = makeEl('theme-toggle', opts.toggleChild);
  if (opts.hasLabel) elements['theme-label'] = makeEl('theme-label');
  if (opts.hasWorld) elements['dwTheme'] = makeEl('dwTheme', opts.worldChild);
  if (opts.hasStatus) elements['dwThemeStatus'] = makeEl('dwThemeStatus');
  const docL = {}, winL = {}, store = new Map(Object.entries(opts.storage || {}));
  let replaced = null;
  const doc = {
    readyState: opts.readyState || 'loading',
    documentElement: opts.noRoot ? null : { dataset: opts.noDataset ? null : {}, style: opts.noStyle ? null : {} },
    getElementById(id) { return elements[id] || null; },
    addEventListener(t, fn) { docL[t] = docL[t] || []; docL[t].push(fn); },
  };
  const win = { addEventListener(t, fn) { winL[t] = winL[t] || []; winL[t].push(fn); } };
  const localStorage = opts.throwStorage
    ? { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('SecurityError'); } }
    : { getItem(k) { return store.has(k) ? store.get(k) : null; }, setItem(k, v) { store.set(k, String(v)); } };
  const location = { search: opts.search || '', href: 'https://example.test/p' + (opts.search || '') };
  const history = { state: null, replaceState(_s, _t, u) { replaced = String(u); } };
  vm.runInNewContext(source, { document: doc, window: win, localStorage, location, history, URL, URLSearchParams });
  return { doc, elements, store, getUrl: () => replaced,
    ready: () => (docL.DOMContentLoaded || []).forEach(fn => fn()),
    fireStorage: (ev) => (winL.storage || []).forEach(fn => fn(ev)) };
}
// 1) noRoot / noDataset / null storage event must never throw
{
  const s1 = run({ noRoot: true });
  s1.ready();
  s1.fireStorage(null);
  const s2 = run({ noDataset: true, noStyle: true });
  s2.ready();
  s2.fireStorage({ key: 'chat_dynamics_ui', newValue: 'night' });
}
// 2) readyState='complete' binds immediately without querySelector on mock toggle + ?theme=dark fallback
{
  const s = run({ hasToggle: true, hasLabel: true, readyState: 'complete', search: '?theme=dark' });
  assert.strictEqual(s.doc.documentElement.dataset.uiTheme, 'night');
  assert.strictEqual(s.elements['theme-label'].textContent, '夜间模式');
  assert.strictEqual(typeof s.elements['theme-toggle'].onclick, 'function');
  s.elements['theme-toggle'].onclick();
  assert.strictEqual(s.doc.documentElement.dataset.uiTheme, 'day');
  assert.strictEqual(s.store.get('chat_dynamics_ui'), 'day');
}
// 3) dynamics_world #dwTheme + .dw-theme-label + #dwThemeStatus on throwStorage
{
  const dwLabel = makeEl('dw-label');
  const s = run({ hasWorld: true, worldChild: { '.dw-theme-label': dwLabel }, hasStatus: true, throwStorage: true, readyState: 'complete' });
  assert.strictEqual(dwLabel.textContent, '日间模式');
  s.elements['dwTheme'].click();
  assert.strictEqual(s.doc.documentElement.dataset.uiTheme, 'night');
  assert.strictEqual(dwLabel.textContent, '夜间模式');
  assert.ok(s.elements['dwThemeStatus'].textContent.length > 0);
}
"""
        res = subprocess.run([node, "-e", harness, str(canonical_theme)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"node harness failed: {res.stderr}\n{res.stdout}"


def test_check_series_ci_gates_on_repo_archives_and_requirements(tmp_path):
    # 1) Chat Dynamics itself passes single-plugin CI gates (metadata, ci.yml, requirements)
    assert check_series_ci.check_plugin_repo(REPO_ROOT) == []
    assert (REPO_ROOT / ".github" / "workflows" / "ci.yml").is_file()

    # 2) Archive hygiene: clean single-top-dir passes; flat root & leaked dev files fail
    clean_zip = tmp_path / "clean.zip"
    with zipfile.ZipFile(clean_zip, "w") as zf:
        zf.writestr("astrbot_plugin_demo/main.py", "x = 1\n")
        zf.writestr("astrbot_plugin_demo/metadata.yaml", "name: demo\n")
    assert check_series_ci.check_archive_hygiene(clean_zip, "astrbot_plugin_demo") == []

    dirty_zip = tmp_path / "dirty.zip"
    with zipfile.ZipFile(dirty_zip, "w") as zf:
        zf.writestr("main.py", "x = 1\n")
        zf.writestr(".github/workflows/ci.yml", "name: CI\n")
        zf.writestr("package-lock.json", "{}\n")
        zf.writestr("tools/build_release.py", "# build\n")
        zf.writestr("core/mod.py.bak", "# bak\n")
    archive_errors = check_series_ci.check_archive_hygiene(dirty_zip, "astrbot_plugin_demo")
    assert any("expected single top-level directory" in e for e in archive_errors)
    assert any(".github" in e for e in archive_errors)
    assert any("package-lock.json" in e for e in archive_errors)
    assert any("build_release.py" in e for e in archive_errors)
    assert any("mod.py.bak" in e for e in archive_errors)

    # 3) AST top-level third-party import vs requirements.txt gate (incl. PyJWT[crypto] extra & lazy func imports)
    fake_repo = tmp_path / "astrbot_plugin_fake"
    fake_repo.mkdir()
    (fake_repo / "main.py").write_text(
        "import json\n"
        "import rank_bm25\n"
        "import jwt\n"
        "from cryptography.hazmat.primitives import hashes\n"
        "try:\n"
        "    import optional_pkg\n"
        "except ImportError:\n"
        "    optional_pkg = None\n"
        "def lazy_loader():\n"
        "    import undeclared_lazy_pkg\n"
        "    return undeclared_lazy_pkg\n",
        encoding="utf-8",
    )
    (fake_repo / "requirements.txt").write_text("# empty\n", encoding="utf-8")
    req_errors = check_series_ci.check_requirements_coverage(fake_repo)
    assert any("rank_bm25" in e for e in req_errors)
    assert any("jwt" in e for e in req_errors)
    assert any("cryptography" in e for e in req_errors)
    assert not any("optional_pkg" in e for e in req_errors)
    assert not any("undeclared_lazy_pkg" in e for e in req_errors)

    (fake_repo / "requirements.txt").write_text(
        "rank-bm25>=0.2.2\nPyJWT[crypto]>=2.8,<3\n",
        encoding="utf-8",
    )
    assert check_series_ci.check_requirements_coverage(fake_repo) == []
    assert threading.current_thread() is threading.main_thread()
