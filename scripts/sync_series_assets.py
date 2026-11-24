"""Synchronize and verify cross-plugin shared series UI assets and reload_bootstrap.py."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]

SERIES_WEB_FILES = (
    "series-ui.css",
    "series-ui.js",
    "series-messages.js",
    "series-messages-en.js",
    "series-messages-ja.js",
    "series-messages-ru.js",
    "series-panels.js",
    "series-workspace.js",
)
SERIES_PY_FILES = (
    "reload_bootstrap.py",
    "series_panels.py",
    "series_workspace.py",
)
COMPANION_PLUGINS = (
    "astrbot_plugin_dynamics_learning",
    "astrbot_plugin_dynamics_memory",
    "astrbot_plugin_dynamics_world",
    "astrbot_plugin_visual_media",
    "astrbot_plugin_dynamics_games",
    "astrbot_plugin_dynamics_horizons",
    "astrbot_plugin_group_chronicle",
)
PANEL_ONLY_PLUGINS: tuple[str, ...] = ()
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
    "astrbot_plugin_chat_dynamics/pages/app/setup",
    "astrbot_plugin_visual_media/views/workshop/behavior",
    "astrbot_plugin_visual_media/views/workshop/memes",
    "astrbot_plugin_visual_media/views/workshop/studio",
    "astrbot_plugin_visual_media/frontend/public",
)

_LOCALE_MESSAGE_FILES = frozenset(
    {
        "series-messages.js",
        "series-messages-en.js",
        "series-messages-ja.js",
        "series-messages-ru.js",
    }
)
_MESSAGES_RE = re.compile(
    r"^(?P<header>/\*.*?\*/\s*)\nexport\s+(?P<decl>const\s+messages\s*=|default)\s*(?P<body>\{.*\})\s*;?\s*$",
    re.DOTALL,
)
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_SCAN_EXTS = frozenset({".html", ".js", ".mjs", ".ts", ".vue", ".py", ".json"})
_SCAN_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "tests",
        "integration",
        "integration_tests",
        "vendor",
    }
)


def _normalize_lf(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n")


def _unescape_unicode(text: str) -> str:
    if "\\u" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        code = int(match.group(1), 16)
        if 0xD800 <= code <= 0xDFFF:
            return match.group(0)
        return chr(code)

    return _UNICODE_ESCAPE_RE.sub(_replace, text)


def _collect_plugin_corpus(
    plugin_dir: Path, web_source_dir: Path, py_source_dir: Path
) -> str:
    chunks: list[str] = []
    for base, dirs, files in os.walk(plugin_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SCAN_SKIP_DIRS)
        base_path = Path(base)
        try:
            rel_base = base_path.relative_to(plugin_dir).as_posix()
        except ValueError:
            rel_base = ""
        # Skip chat_dynamics' own canonical pages/shared/ source folder so
        # canonical shared files don't short-circuit pruning on themselves.
        if (
            plugin_dir.name == "astrbot_plugin_chat_dynamics"
            and rel_base == "pages/shared"
        ):
            continue
        for fname in sorted(files):
            if (
                fname in _LOCALE_MESSAGE_FILES
                or fname == "package-lock.json"
                or fname.endswith(".min.js")
            ):
                continue
            fpath = base_path / fname
            if fpath.suffix not in _SCAN_EXTS:
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            chunks.append(_unescape_unicode(text))

    for extra_path in (
        web_source_dir / "series-ui.js",
        web_source_dir / "series-panels.js",
        web_source_dir / "series-workspace.js",
        py_source_dir / "series_panels.py",
        py_source_dir / "series_workspace.py",
    ):
        if extra_path.is_file():
            try:
                chunks.append(
                    _unescape_unicode(
                        extra_path.read_text(encoding="utf-8", errors="ignore")
                    )
                )
            except OSError:
                pass
    return "\n".join(chunks)


def _prune_series_messages(
    src_bytes: bytes,
    plugin_dir: Path,
    web_source_dir: Path,
    py_source_dir: Path,
    *,
    corpus_cache: dict[tuple[Path, Path, Path], str] | None = None,
    key_match_cache: dict[tuple[Path, str], bool] | None = None,
) -> bytes:
    """Prune series-messages*.js to keys referenced by the target plugin + shared chrome.

    Supports both ``export const messages = {...};`` and ``export default {...};``.
    Falls back to normalized source bytes if the canonical source is a loader stub.
    """
    normalized = _normalize_lf(src_bytes)
    text = normalized.decode("utf-8", errors="ignore")
    match = _MESSAGES_RE.match(text)
    if not match:
        return normalized
    try:
        catalog = json.loads(match.group("body"))
    except ValueError:
        return normalized
    if not isinstance(catalog, dict):
        return normalized

    cache_key = (plugin_dir, web_source_dir, py_source_dir)
    if corpus_cache is not None and cache_key in corpus_cache:
        corpus = corpus_cache[cache_key]
    else:
        corpus = _collect_plugin_corpus(plugin_dir, web_source_dir, py_source_dir)
        if corpus_cache is not None:
            corpus_cache[cache_key] = corpus
    if key_match_cache is None:
        pruned = {key: val for key, val in catalog.items() if key in corpus}
    else:
        pruned = {}
        for key, val in catalog.items():
            mk = (plugin_dir, key)
            hit = key_match_cache.get(mk)
            if hit is None:
                hit = key in corpus
                key_match_cache[mk] = hit
            if hit:
                pruned[key] = val
    header = match.group("header").strip()
    decl = match.group("decl").strip()
    payload = json.dumps(
        pruned, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    if decl == "default":
        return f"{header}\nexport default {payload};\n".encode("utf-8")
    return f"{header}\nexport const messages = {payload};\n".encode("utf-8")


def _resolve_source_dirs(workspace: Path, source_dir: Path | None) -> tuple[Path, Path]:
    chat_root = workspace / "astrbot_plugin_chat_dynamics"
    if source_dir is None:
        return chat_root / "pages" / "shared", chat_root
    web_src = (
        source_dir
        if (source_dir / "series-ui.js").is_file()
        else source_dir / "pages" / "shared"
    )
    py_src = source_dir
    return web_src, py_src


def iter_pairs(
    workspace: Path, *, web_only: bool = False, source_dir: Path | None = None
):
    """Yield (source, target, kind) or canonical sync pairs for existing plugins."""
    web_source_dir, py_source_dir = _resolve_source_dirs(workspace, source_dir)
    chat_root = workspace / "astrbot_plugin_chat_dynamics"

    if source_dir is not None and chat_root.is_dir():
        for filename in SERIES_WEB_FILES:
            yield web_source_dir / filename, chat_root / "pages" / "shared" / filename
        if not web_only:
            for filename in SERIES_PY_FILES:
                yield py_source_dir / filename, chat_root / filename

    for rel_dir in WEB_TARGETS:
        plugin_name = rel_dir.split("/", 1)[0]
        if not (workspace / plugin_name).is_dir():
            continue
        target_dir = workspace / rel_dir
        for filename in SERIES_WEB_FILES:
            yield web_source_dir / filename, target_dir / filename

    if web_only:
        return

    for plugin_name in COMPANION_PLUGINS + PANEL_ONLY_PLUGINS:
        plugin_dir = workspace / plugin_name
        if not plugin_dir.is_dir():
            continue
        for filename in SERIES_PY_FILES:
            yield py_source_dir / filename, plugin_dir / filename


def _expected_bytes(
    source: Path,
    target: Path,
    workspace: Path,
    web_source_dir: Path,
    py_source_dir: Path,
    *,
    corpus_cache: dict[tuple[Path, Path, Path], str] | None = None,
    key_match_cache: dict[tuple[Path, str], bool] | None = None,
) -> bytes:
    src_bytes = _normalize_lf(source.read_bytes())
    if target.name not in _LOCALE_MESSAGE_FILES:
        return src_bytes
    # Keep canonical chat_dynamics/pages/shared/series-messages*.js unpruned.
    rel = target.relative_to(workspace).as_posix()
    if rel.startswith("astrbot_plugin_chat_dynamics/pages/shared/"):
        return src_bytes
    plugin_name = rel.split("/", 1)[0]
    return _prune_series_messages(
        src_bytes,
        workspace / plugin_name,
        web_source_dir,
        py_source_dir,
        corpus_cache=corpus_cache,
        key_match_cache=key_match_cache,
    )


def _display_path(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


def check_workspace(
    workspace: Path, *, web_only: bool = False, source_dir: Path | None = None
) -> list[str]:
    """Return shared asset drift or hygiene errors across the series."""
    errors: list[str] = []
    web_source_dir, py_source_dir = _resolve_source_dirs(workspace, source_dir)
    corpus_cache: dict[tuple[Path, Path, Path], str] = {}
    key_match_cache: dict[tuple[Path, str], bool] = {}
    for source, target in iter_pairs(
        workspace, web_only=web_only, source_dir=source_dir
    ):
        try:
            src_bytes = source.read_bytes()
            tgt_bytes = target.read_bytes()
        except OSError as exc:
            errors.append(
                f"missing shared file {target.relative_to(workspace).as_posix()}: {type(exc).__name__}"
            )
            continue
        if b"\r\n" in src_bytes:
            errors.append(
                f"CRLF found in canonical source: {_display_path(source, workspace)}"
            )
        expected = _expected_bytes(
            source,
            target,
            workspace,
            web_source_dir,
            py_source_dir,
            corpus_cache=corpus_cache,
            key_match_cache=key_match_cache,
        )
        if expected != tgt_bytes:
            errors.append(
                f"shared file out of sync: {target.relative_to(workspace).as_posix()}"
            )

    for rel_dir in FORBIDDEN_DUPLICATE_DIRS:
        dup_dir = workspace / rel_dir
        if not dup_dir.is_dir():
            continue
        for item in sorted(dup_dir.glob("series-*")):
            errors.append(
                f"duplicate series asset should use shared parent copy: {item.relative_to(workspace).as_posix()}"
            )

    all_plugins = (
        ("astrbot_plugin_chat_dynamics",) + COMPANION_PLUGINS + PANEL_ONLY_PLUGINS
    )
    for plugin_name in all_plugins:
        plugin_dir = workspace / plugin_name
        if not plugin_dir.is_dir():
            continue
        for obsolete in sorted(plugin_dir.rglob("series-panels.css")):
            if "node_modules" in obsolete.parts or ".git" in obsolete.parts:
                continue
            errors.append(
                f"obsolete @import wrapper remains: {obsolete.relative_to(workspace).as_posix()}"
            )
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
                errors.append(
                    f"stale series-panels.css reference in {html_or_js.relative_to(workspace).as_posix()}"
                )

    ui_css = web_source_dir / "series-ui.css"
    if ui_css.is_file():
        stripped_css = re.sub(
            r"/\*[\s\S]*?\*/",
            "",
            ui_css.read_text(encoding="utf-8", errors="ignore"),
        )
        if "@import" in stripped_css:
            errors.append("series-ui.css must not contain render-blocking @import")

    return errors


def sync_workspace(
    workspace: Path, *, web_only: bool = False, source_dir: Path | None = None
) -> None:
    """Normalize canonical sources to LF and publish to all companion targets."""
    web_source_dir, py_source_dir = _resolve_source_dirs(workspace, source_dir)
    canonical_files = [web_source_dir / f for f in SERIES_WEB_FILES]
    if not web_only:
        canonical_files.extend(py_source_dir / f for f in SERIES_PY_FILES)
    for canonical in canonical_files:
        if canonical.is_file():
            canonical.write_bytes(_normalize_lf(canonical.read_bytes()))

    # First pass ensures chat_dynamics canonical sources and other plain shared
    # files are written before computing pruned series-messages*.js outputs.
    pairs = list(iter_pairs(workspace, web_only=web_only, source_dir=source_dir))
    for source, target in pairs:
        if target.name in _LOCALE_MESSAGE_FILES:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_normalize_lf(source.read_bytes()))

    corpus_cache: dict[tuple[Path, Path, Path], str] = {}
    key_match_cache: dict[tuple[Path, str], bool] = {}
    for source, target in pairs:
        if target.name not in _LOCALE_MESSAGE_FILES:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = _expected_bytes(
            source,
            target,
            workspace,
            web_source_dir,
            py_source_dir,
            corpus_cache=corpus_cache,
            key_match_cache=key_match_cache,
        )
        target.write_bytes(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="verify without modifying files"
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="sync or verify HTML page assets without copying Python helpers",
    )
    parser.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="optional directory providing canonical series_panels.py, reload_bootstrap.py, and series-* web files",
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    source_dir = args.source_dir.resolve() if args.source_dir else None
    if args.check:
        errors = check_workspace(
            workspace, web_only=args.web_only, source_dir=source_dir
        )
        for err in errors:
            print(err, file=sys.stderr)
        return int(bool(errors))
    sync_workspace(workspace, web_only=args.web_only, source_dir=source_dir)
    errors = check_workspace(workspace, web_only=args.web_only, source_dir=source_dir)
    for err in errors:
        print(err, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
