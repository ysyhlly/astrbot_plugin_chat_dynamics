"""Publish canonical shared assets as page-local files for AstrBot's sandbox."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "pages" / "app"
PAGES = ("console", "config", "memory", "replay", "learning", "setup")


def _asset_root(root: Path) -> Path:
    return root / "app" if (root / "app" / "shared").is_dir() else root


def asset_pairs(root: Path):
    """Yield canonical and published paths for assets managed by this script."""
    base = _asset_root(root)
    for page in PAGES:
        assets = ["plugin_nav.js", "plugin_nav.css", "theme.js", "theme.css", "base.css", "errors.js"]
        if page != "console":
            assets += ["shell.css", "shell.js", "api.js"]
        for name in assets:
            yield base / "shared" / name, base / page / name


def check_page_assets(root: Path) -> list[str]:
    """Report missing or divergent managed assets without writing files."""
    base = _asset_root(root)
    errors = []
    for source, target in asset_pairs(base):
        try:
            matches = source.read_bytes() == target.read_bytes()
        except OSError as exc:
            errors.append(f"cannot compare {target.relative_to(base)}: {type(exc).__name__}")
            continue
        if not matches:
            errors.append(f"shared asset out of sync: {target.relative_to(base)}")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check copies without writing")
    parser.add_argument("--root", type=Path, default=ROOT, help="pages directory")
    args = parser.parse_args(argv)
    if args.check:
        errors = check_page_assets(args.root)
        for error in errors:
            print(error, file=sys.stderr)
        return int(bool(errors))
    for source, target in asset_pairs(args.root):
        target.write_bytes(source.read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
