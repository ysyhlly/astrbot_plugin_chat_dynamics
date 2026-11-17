"""Contract tests for cross-plugin series-* asset deduplication and reload_bootstrap parity."""

from __future__ import annotations

from pathlib import Path

from scripts.sync_series_assets import (
    SERIES_PY_FILES,
    SERIES_WEB_FILES,
    WORKSPACE_ROOT,
    check_workspace,
    main,
    sync_workspace,
)


def test_live_workspace_series_assets_and_reload_bootstrap_in_sync():
    assert check_workspace(WORKSPACE_ROOT) == []
    assert main(["--workspace", str(WORKSPACE_ROOT), "--check"]) == 0


def test_sync_series_assets_detects_drift_duplicates_and_import_wrappers(tmp_path: Path):
    chat = tmp_path / "astrbot_plugin_chat_dynamics"
    world = tmp_path / "astrbot_plugin_dynamics_world"
    (chat / "pages/shared").mkdir(parents=True)
    (chat / "pages/app/config").mkdir(parents=True)
    (world / "pages/world").mkdir(parents=True)

    for name in SERIES_WEB_FILES:
        (chat / "pages/shared" / name).write_text(f"/* {name} */\n", encoding="utf-8")
    for name in SERIES_PY_FILES:
        (chat / name).write_text(f"# {name}\n", encoding="utf-8")

    sync_workspace(tmp_path)
    assert check_workspace(tmp_path) == []

    # 1. Drift in companion plugin
    (world / "reload_bootstrap.py").write_text("# drifted\n", encoding="utf-8")
    # 2. Duplicate series-* file inside app/config
    (chat / "pages/app/config/series-panels.js").write_text("// dup\n", encoding="utf-8")
    # 3. Obsolete @import wrapper & stale HTML reference
    (world / "pages/world/series-panels.css").write_text('@import url("./series-ui.css");\n', encoding="utf-8")
    (world / "pages/world/index.html").write_text(
        '<link rel="stylesheet" href="./series-panels.css">', encoding="utf-8"
    )

    errors = check_workspace(tmp_path)
    assert any("shared file out of sync: astrbot_plugin_dynamics_world/reload_bootstrap.py" in e for e in errors)
    assert any("duplicate series asset" in e for e in errors)
    assert any("obsolete @import wrapper" in e for e in errors)
    assert any("stale series-panels.css reference" in e for e in errors)
