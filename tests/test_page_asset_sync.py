from pathlib import Path

from scripts.check_release import validate_release
from scripts.sync_page_assets import asset_pairs, check_page_assets, main


def _pages(root: Path) -> Path:
    pages = root / "pages"
    for source, target in asset_pairs(pages):
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"canonical\n")
        target.write_bytes(b"canonical\n")
    return pages


def test_check_rejects_drift_without_changing_copies_and_sync_repairs_it(tmp_path):
    pages = _pages(tmp_path)
    target = pages / "replay" / "api.js"
    target.write_bytes(b"stale\n")
    assert main(["--root", str(pages), "--check"]) == 1
    assert target.read_bytes() == b"stale\n"
    assert main(["--root", str(pages)]) == 0
    assert main(["--root", str(pages), "--check"]) == 0


def test_release_check_includes_asset_drift(tmp_path):
    pages = _pages(tmp_path)
    (pages / "config" / "theme.js").write_bytes(b"stale")
    assert any("shared asset out of sync" in error for error in validate_release(tmp_path))


def test_check_reports_missing_source_or_copy(tmp_path):
    pages = _pages(tmp_path)
    (pages / "shared" / "theme.js").unlink()
    (pages / "today" / "api.js").unlink()
    errors = check_page_assets(pages)
    assert len(errors) == 7
    assert all("FileNotFoundError" in error for error in errors)


def test_console_specific_assets_are_not_managed(tmp_path):
    pages = _pages(tmp_path)
    (pages / "console" / "base.css").write_bytes(b"console-specific")
    assert check_page_assets(pages) == []
