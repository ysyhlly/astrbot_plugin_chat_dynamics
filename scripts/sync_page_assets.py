"""Publish canonical shared assets as page-local files for AstrBot's sandbox."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "pages"
PAGES = ("console", "config", "today", "manners", "memory", "replay")


def main():
    for page in PAGES:
        assets = ["plugin_nav.js", "plugin_nav.css", "theme.js", "theme.css"]
        if page != "console":
            assets += ["base.css", "shell.css", "shell.js", "api.js"]
        for name in assets:
            (ROOT / page / name).write_bytes((ROOT / "shared" / name).read_bytes())


if __name__ == "__main__":
    main()
