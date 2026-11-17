"""Regression tests for astrbot_plugin_dynamics_learning v0.13.1 optimizations."""
from __future__ import annotations

from pathlib import Path

from astrbot_plugin_dynamics_learning.core.review_queue import query_queue
from astrbot_plugin_dynamics_learning.core.store import Store
from astrbot_plugin_dynamics_learning.core.workbench import library
from astrbot_plugin_dynamics_learning.vendor.group_relations.store import RelationStore

LEARNING_ROOT = Path(__file__).resolve().parents[2] / "astrbot_plugin_dynamics_learning"


def test_learning_sqlite_stores_apply_wal_normal_and_busy_timeout(tmp_path: Path) -> None:
    db_path = tmp_path / "learning.sqlite3"
    store = Store(db_path)
    try:
        mode = store.db.execute("PRAGMA journal_mode").fetchone()[0].lower()
        sync = store.db.execute("PRAGMA synchronous").fetchone()[0]
        timeout = store.db.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode == "wal"
        assert sync == 1  # NORMAL
        assert timeout == 5000

        queue_res = query_queue(store, ["qq:GroupMessage:1"], {}, {}, {}, [])
        assert queue_res["pending_total"] == 0

        lib_res = library(store, ["qq:GroupMessage:1"])
        assert lib_res["total"] == 0
    finally:
        store.close()

    rel_store = RelationStore(tmp_path / "relations.sqlite3")
    with rel_store._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_learning_review_frontend_lazy_imports_network_and_isolates_cards() -> None:
    app_js = (LEARNING_ROOT / "pages" / "review" / "app.js").read_text(encoding="utf-8")
    style_css = (LEARNING_ROOT / "pages" / "review" / "style.css").read_text(encoding="utf-8")
    index_html = (LEARNING_ROOT / "pages" / "review" / "index.html").read_text(encoding="utf-8")

    assert "import {RelationNetwork} from './network.js" not in app_js
    assert "import('./network.js?v=0.8.0')" in app_js
    assert "content-visibility:auto" in style_css
    assert 'href="./series-ui.css?v=series.3"' in index_html
    assert "series-panels.css" not in index_html
