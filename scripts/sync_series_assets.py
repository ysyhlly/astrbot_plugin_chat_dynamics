"""Synchronize and verify cross-plugin shared series UI assets and reload_bootstrap.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]

SERIES_WEB_FILES = (
    "series-ui.css",
    "series-ui.js",
    "series-messages.js",
    "series-panels.js",
)
SERIES_PY_FILES = (
    "reload_bootstrap.py",
    "series_panels.py",
)
COMPANION_PLUGINS = (
    "astrbot_plugin_dynamics_learning",
    "astrbot_plugin_dynamics_memory",
    "astrbot_plugin_dynamics_world",
    "astrbot_plugin_visual_media",
)
PANEL_ONLY_PLUGINS = (
    "astrbot_plugin_dynamics_games",
    "astrbot_plugin_dynamics_horizons",
    "astrbot_plugin_group_chronicle",
)
WEB_TARGETS = (
    "astrbot_plugin_chat_dynamics/pages/app/shared",
    "astrbot_plugin_dynamics_learning/pages/review",
    "astrbot_plugin_dynamics_memory/pages/dashboard",
    "astrbot_plugin_dynamics_world/pages/world",
    "astrbot_plugin_visual_media/views/workshop",
    "astrbot_plugin_dynamics_games/pages/games",
    "astrbot_plugin_dynamics_horizons/pages/horizons",
    "astrbot_plugin_group_chronicle/pages/culture",
)
FORBIDDEN_DUPLICATE_DIRS = (
    "astrbot_plugin_chat_dynamics/pages/app/config",
    "astrbot_plugin_chat_dynamics/pages/app/console",
    "astrbot_plugin_chat_dynamics/pages/app/learning",
    "astrbot_plugin_chat_dynamics/pages/app/memory",
    "astrbot_plugin_chat_dynamics/pages/app/replay",
    "astrbot_plugin_visual_media/views/workshop/behavior",
    "astrbot_plugin_visual_media/views/workshop/memes",
    "astrbot_plugin_visual_media/views/workshop/studio",
    "astrbot_plugin_visual_media/frontend/public",
)


def _normalize_lf(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n")


def iter_pairs(workspace: Path, *, web_only: bool = False):
    """Yield (source, target) pairs for existing plugins in workspace."""
    chat_root = workspace / "astrbot_plugin_chat_dynamics"
    web_source_dir = chat_root / "pages" / "shared"
    for rel_dir in WEB_TARGETS:
        plugin_name = rel_dir.split("/", 1)[0]
        if not (workspace / plugin_name).is_dir():
            continue
        target_dir = workspace / rel_dir
        for filename in SERIES_WEB_FILES:
            yield web_source_dir / filename, target_dir / filename

    if web_only:
        return

    for plugin_name in COMPANION_PLUGINS:
        plugin_dir = workspace / plugin_name
        if not plugin_dir.is_dir():
            continue
        for filename in SERIES_PY_FILES:
            yield chat_root / filename, plugin_dir / filename

    for plugin_name in PANEL_ONLY_PLUGINS:
        plugin_dir = workspace / plugin_name
        if plugin_dir.is_dir():
            yield chat_root / "series_panels.py", plugin_dir / "series_panels.py"


def check_workspace(workspace: Path, *, web_only: bool = False) -> list[str]:
    """Return shared asset drift or hygiene errors across the series."""
    errors: list[str] = []
    for source, target in iter_pairs(workspace, web_only=web_only):
        try:
            src_bytes = source.read_bytes()
            tgt_bytes = target.read_bytes()
        except OSError as exc:
            errors.append(f"missing shared file {target.relative_to(workspace).as_posix()}: {type(exc).__name__}")
            continue
        if b"\r\n" in src_bytes:
            errors.append(f"CRLF found in canonical source: {source.relative_to(workspace).as_posix()}")
        if src_bytes != tgt_bytes:
            errors.append(f"shared file out of sync: {target.relative_to(workspace).as_posix()}")

    for rel_dir in FORBIDDEN_DUPLICATE_DIRS:
        dup_dir = workspace / rel_dir
        if not dup_dir.is_dir():
            continue
        for item in sorted(dup_dir.glob("series-*")):
            errors.append(
                f"duplicate series asset should use shared parent copy: {item.relative_to(workspace).as_posix()}"
            )

    all_plugins = ("astrbot_plugin_chat_dynamics",) + COMPANION_PLUGINS + PANEL_ONLY_PLUGINS
    for plugin_name in all_plugins:
        plugin_dir = workspace / plugin_name
        if not plugin_dir.is_dir():
            continue
        for obsolete in sorted(plugin_dir.rglob("series-panels.css")):
            if "node_modules" in obsolete.parts or ".git" in obsolete.parts:
                continue
            errors.append(f"obsolete @import wrapper remains: {obsolete.relative_to(workspace).as_posix()}")
        for html_or_js in sorted(plugin_dir.rglob("*")):
            if (
                not html_or_js.is_file()
                or html_or_js.suffix not in {".html", ".mjs"}
                or "node_modules" in html_or_js.parts
                or ".git" in html_or_js.parts
            ):
                continue
            text = html_or_js.read_text(encoding="utf-8", errors="ignore")
            if "series-panels.css" in text:
                errors.append(f"stale series-panels.css reference in {html_or_js.relative_to(workspace).as_posix()}")

    ui_css = workspace / "astrbot_plugin_chat_dynamics/pages/shared/series-ui.css"
    if ui_css.is_file() and "@import" in ui_css.read_text(encoding="utf-8", errors="ignore"):
        errors.append("series-ui.css must not contain render-blocking @import")

    return errors


def sync_workspace(workspace: Path, *, web_only: bool = False) -> None:
    """Normalize canonical sources to LF and publish to all companion targets."""
    chat_root = workspace / "astrbot_plugin_chat_dynamics"
    canonical_files = [chat_root / "pages" / "shared" / f for f in SERIES_WEB_FILES]
    if not web_only:
        canonical_files.extend(chat_root / f for f in SERIES_PY_FILES)
    for canonical in canonical_files:
        if canonical.is_file():
            canonical.write_bytes(_normalize_lf(canonical.read_bytes()))

    for source, target in iter_pairs(workspace, web_only=web_only):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without modifying files")
    parser.add_argument(
        "--web-only", action="store_true", help="sync or verify HTML page assets without copying Python helpers"
    )
    parser.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if args.check:
        errors = check_workspace(workspace, web_only=args.web_only)
        for err in errors:
            print(err, file=sys.stderr)
        return int(bool(errors))
    sync_workspace(workspace, web_only=args.web_only)
    errors = check_workspace(workspace, web_only=args.web_only)
    for err in errors:
        print(err, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
