"""Validate the files and metadata required for a public plugin release.

The release check deliberately uses only the Python standard library so it can
run in a clean checkout before development dependencies are installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

try:
    from .sync_page_assets import check_page_assets
except ImportError:  # direct ``python scripts/check_release.py`` execution
    from sync_page_assets import check_page_assets


_VERSION_RE = re.compile(r"^\s*version\s*:\s*['\"]?([^'\"\s]+)", re.MULTILINE)
_REPO_RE = re.compile(r"^\s*repo\s*:\s*['\"]?(.*?)['\"]?\s*$", re.MULTILINE)


def _required_paths(root: Path) -> tuple[str, ...]:
    return (
        "__init__.py",
        "main.py",
        "core",
        "pages/console/index.html",
        "pages/console/app.js",
        "pages/console/style.css",
        "pages/console/_page.json",
        "i18n",
        ".astrbot-plugin/i18n",
        "_conf_schema.json",
        "metadata.yaml",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "requirements.txt",
    )


def _read_metadata(root: Path) -> tuple[str, str]:
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    version_match = _VERSION_RE.search(metadata)
    repo_match = _REPO_RE.search(metadata)
    if not version_match:
        raise ValueError("metadata.yaml is missing version")
    if not repo_match:
        raise ValueError("metadata.yaml is missing repo")
    return version_match.group(1).strip(), repo_match.group(1).strip()


def plugin_version(root: Path) -> str:
    """Return the canonical plugin version from metadata.yaml."""
    version, _ = _read_metadata(root)
    return version


def _check_schema(root: Path) -> list[str]:
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    pipeline = schema.get("pipeline_mode", {})
    if pipeline.get("options") != ["filter", "exclusive"]:
        errors.append("pipeline_mode.options must be ['filter', 'exclusive']")
    for key in ("reply_provider", "vibe_provider"):
        if key not in schema or schema[key].get("default") != "":
            errors.append(f"{key} must exist with an empty default")
    for key in ("vibe_llm_enabled", "shadow_mode", "console_show_message_content"):
        if schema.get(key, {}).get("default") is not False:
            errors.append(f"{key} must default to false")
    decision = schema.get("decision_mode", {})
    if decision.get("default") != "legacy":
        errors.append("decision_mode must default to legacy")
    if decision.get("options") != ["legacy", "persona_model"]:
        errors.append("decision_mode.options must be ['legacy', 'persona_model']")
    return errors


def validate_release(root: Path, *, allow_empty_repo: bool = False) -> list[str]:
    """Return human-readable release errors; an empty list means valid."""
    errors: list[str] = []
    for relative in _required_paths(root):
        path = root / relative
        if not path.exists():
            errors.append(f"missing required path: {relative}")
        elif relative == "core" and not any(path.glob("*.py")):
            errors.append("core must contain Python runtime modules")

    errors.extend(check_page_assets(root / "pages"))

    try:
        version, repo = _read_metadata(root)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        version, repo = "", ""
    try:
        metadata_text = (root / "metadata.yaml").read_text(encoding="utf-8")
    except OSError:
        metadata_text = ""
    if "vibe_llm_enabled=false" not in metadata_text:
        errors.append("metadata.yaml must declare vibe_llm_enabled=false")
    if not allow_empty_repo and (not repo or not re.match(r"https?://[^\s]+", repo)):
        errors.append("metadata.yaml repo must be a real https/http URL before release")

    try:
        errors.extend(_check_schema(root))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        errors.append(f"invalid _conf_schema.json: {type(exc).__name__}")

    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        if version and f"## {version}" not in changelog:
            errors.append(f"CHANGELOG.md is missing ## {version}")
        if re.search(r"^##\s+Unreleased\s*$", changelog, re.MULTILINE):
            errors.append("CHANGELOG.md still contains an Unreleased section")
    except OSError as exc:
        errors.append(f"cannot read CHANGELOG.md: {type(exc).__name__}")

    return errors


def iter_release_files(root: Path) -> Iterable[Path]:
    """Yield deterministic runtime files for the release archive."""
    explicit = (
        "__init__.py",
        "main.py",
        "_conf_schema.json",
        "metadata.yaml",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "requirements.txt",
        "pages",
        ".astrbot-plugin",
        "i18n",
        "core",
        "logo.png",
        "assets",
    )
    paths: list[Path] = []
    for relative in explicit:
        path = root / relative
        if path.is_file():
            paths.append(path)
        elif path.is_dir():
            paths.extend(
                item
                for item in path.rglob("*")
                if item.is_file()
                and "__pycache__" not in item.parts
                and item.suffix not in {".pyc", ".pyo"}
            )
    return sorted(set(paths), key=lambda item: item.relative_to(root).as_posix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--allow-empty-repo",
        action="store_true",
        help="allow local CI checks before the public repository URL exists",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors = validate_release(root, allow_empty_repo=args.allow_empty_repo)
    if errors:
        print("Release check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    files = [path.relative_to(root).as_posix() for path in iter_release_files(root)]
    version = plugin_version(root)
    print(f"Release check passed for {version} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
