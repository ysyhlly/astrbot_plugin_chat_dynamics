"""Cross-8-plugin CI & packaging hard gate for the Chat Dynamics series.

Pure Python standard library so it can run in any clean checkout or CI runner
before development dependencies or AstrBot are installed.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import sys
import zipfile
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SERIES_PLUGINS = (
    "astrbot_plugin_chat_dynamics",
    "astrbot_plugin_dynamics_world",
    "astrbot_plugin_dynamics_learning",
    "astrbot_plugin_dynamics_memory",
    "astrbot_plugin_dynamics_horizons",
    "astrbot_plugin_dynamics_games",
    "astrbot_plugin_visual_media",
    "astrbot_plugin_group_chronicle",
)

_SKIP_SCAN_DIRS = frozenset(
    {
        ".git",
        ".github",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        ".venv",
        "venv",
        "vendor",
        "dist",
        "build",
        "tests",
        "test",
        "integration",
        "integration_tests",
        "scripts",
        "tools",
    }
)

_FORBIDDEN_ARCHIVE_SEGMENTS = frozenset(
    {
        ".git",
        ".github",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "tests",
        "test",
        "integration",
        "integration_tests",
    }
)
_FORBIDDEN_ARCHIVE_FILENAMES = frozenset(
    {
        ".gitignore",
        ".gitattributes",
        ".env",
        "package-lock.json",
        "build_release.py",
        "pytest.ini",
        "pyproject.toml",
    }
)
_FORBIDDEN_ARCHIVE_GLOBS = (
    "*.pyc",
    "*.pyo",
    "*.bak",
    "*.bak-*",
    "*.orig",
    "*.rej",
    "*.swp",
    "*~",
)

_SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_META_FIELD_RE = re.compile(r"^\s*([a-zA-Z0-9_]+)\s*:\s*(.*?)\s*$", re.MULTILINE)
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[([^\]]+)\])?")

# Host/runtime packages provided by AstrBot itself or non-PyPI runtime shims.
_HOST_PROVIDED_MODULES = frozenset(
    {
        "astrbot",
        "aiohttp",
        "quart",
        "yaml",
        "PIL",
        "pydantic",
        "httpx",
        "openai",
        "anthropic",
        "google",
        "fastapi",
        "starlette",
        "uvicorn",
        "requests",
        "websockets",
        "psutil",
        "sqlalchemy",
        "aiosqlite",
        "jinja2",
        "werkzeug",
        "click",
        "dotenv",
        "typing_extensions",
        "faiss",
        "apscheduler",
        "mcp",
        "sqlmodel",
    }
)

# Map import top-level module name -> normalized PyPI distribution name(s).
_IMPORT_TO_DIST: dict[str, tuple[str, ...]] = {
    "rank_bm25": ("rank-bm25", "rank_bm25"),
    "PIL": ("pillow",),
    "yaml": ("pyyaml",),
    "cv2": ("opencv-python", "opencv-python-headless"),
    "bs4": ("beautifulsoup4",),
    "sklearn": ("scikit-learn",),
    "skimage": ("scikit-image",),
    "bilibili_api": ("bilibili-api-python", "bilibili-api"),
    "jwt": ("pyjwt",),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "Crypto": ("pycryptodome", "pycryptodomex"),
    "faiss": ("faiss-cpu", "faiss-gpu"),
    "jieba": ("jieba",),
    "numpy": ("numpy",),
    "networkx": ("networkx",),
    "ebooklib": ("ebooklib",),
    "lxml": ("lxml",),
    "playwright": ("playwright",),
    "cryptography": ("cryptography", "pyjwt[crypto]"),
    "scipy": ("scipy",),
    "aiofiles": ("aiofiles",),
    "feedparser": ("feedparser",),
    "trafilatura": ("trafilatura",),
    "httpx": ("httpx",),
}


def _norm_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def parse_requirements(req_path: Path) -> set[str]:
    """Parse normalized package distribution names (and pkg[extra] tokens) from requirements.txt."""
    if not req_path.is_file():
        return set()
    dists: set[str] = set()
    for raw in req_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        match = _REQ_NAME_RE.match(line)
        if match:
            base = _norm_dist(match.group(1))
            dists.add(base)
            raw_extras = match.group(2)
            if raw_extras:
                for extra in raw_extras.split(","):
                    clean_extra = _norm_dist(extra)
                    if clean_extra:
                        dists.add(f"{base}[{clean_extra}]")
    return dists


def _local_module_names(repo_dir: Path) -> set[str]:
    names = {repo_dir.name}
    for entry in repo_dir.iterdir():
        if entry.name.startswith(".") or entry.name in _SKIP_SCAN_DIRS:
            continue
        if entry.is_dir():
            names.add(entry.name)
        elif entry.is_file() and entry.suffix == ".py":
            names.add(entry.stem)
    return names


def _collect_unconditional_imports(tree: ast.AST) -> list[tuple[str, int]]:
    """Collect module top-level imports not guarded by try/except, TYPE_CHECKING, or lazy functions."""
    results: list[tuple[str, int]] = []

    def _visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, ast.Try):
            for child in node.body:
                _visit(child, True)
            for handler in node.handlers:
                for child in handler.body:
                    _visit(child, True)
            for child in node.orelse:
                _visit(child, guarded)
            for child in node.finalbody:
                _visit(child, guarded)
            return
        if isinstance(node, ast.If):
            test = node.test
            is_type_checking = (
                isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
            ) or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
            for child in node.body:
                _visit(child, guarded or is_type_checking)
            for child in node.orelse:
                _visit(child, guarded)
            return
        if not guarded:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top:
                        results.append((top, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top = node.module.split(".", 1)[0]
                    if top:
                        results.append((top, node.lineno))
        for child in ast.iter_child_nodes(node):
            _visit(child, guarded)

    _visit(tree, False)
    return results


def check_requirements_coverage(repo_dir: Path) -> list[str]:
    """Verify unconditional third-party imports are declared in requirements.txt."""
    errors: list[str] = []
    declared = parse_requirements(repo_dir / "requirements.txt")
    local_names = _local_module_names(repo_dir)
    stdlib = set(sys.stdlib_module_names) | {"__future__", "pkg_resources"}

    seen: set[tuple[str, str]] = set()
    for py in sorted(repo_dir.rglob("*.py")):
        rel_parts = py.relative_to(repo_dir).parts
        if set(rel_parts) & _SKIP_SCAN_DIRS:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8-sig"), filename=str(py))
        except (OSError, SyntaxError) as exc:
            errors.append(
                f"{repo_dir.name}: syntax error in {py.relative_to(repo_dir).as_posix()}: {exc}"
            )
            continue
        for top, lineno in _collect_unconditional_imports(tree):
            if (
                top in stdlib
                or top in local_names
                or top in _HOST_PROVIDED_MODULES
                or top.startswith("astrbot_plugin_")
            ):
                continue
            candidates = _IMPORT_TO_DIST.get(top, (top,))
            if any(_norm_dist(cand) in declared for cand in candidates):
                continue
            key = (py.relative_to(repo_dir).as_posix(), top)
            if key not in seen:
                seen.add(key)
                errors.append(
                    f"{repo_dir.name}: undeclared third-party import '{top}' at {key[0]}:{lineno} (not in requirements.txt)"
                )
    return errors


def check_metadata_and_version(repo_dir: Path) -> list[str]:
    """Validate metadata.yaml presence, required fields, and semver version."""
    errors: list[str] = []
    meta_path = repo_dir / "metadata.yaml"
    if not meta_path.is_file():
        return [f"{repo_dir.name}: missing metadata.yaml"]
    text = meta_path.read_text(encoding="utf-8-sig", errors="ignore")
    fields: dict[str, str] = {}
    for match in _META_FIELD_RE.finditer(text):
        key = match.group(1).strip()
        val = match.group(2).strip().strip("'\"")
        if key not in fields:
            fields[key] = val
    for req in ("name", "desc", "version", "author"):
        if not fields.get(req):
            errors.append(f"{repo_dir.name}: metadata.yaml missing required field '{req}'")
    version = fields.get("version", "")
    if version and not _SEMVER_RE.match(version):
        errors.append(f"{repo_dir.name}: invalid metadata.yaml version '{version}'")
    return errors


def check_ci_workflow(repo_dir: Path, *, require_ci_yml: bool = True) -> list[str]:
    """Ensure the repository defines a CI workflow under .github/workflows/."""
    wf_dir = repo_dir / ".github" / "workflows"
    if not wf_dir.is_dir():
        return [f"{repo_dir.name}: missing .github/workflows/ directory"]
    if require_ci_yml:
        if not (wf_dir / "ci.yml").is_file():
            return [f"{repo_dir.name}: missing .github/workflows/ci.yml"]
        return []
    ymls = list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))
    if not ymls:
        return [f"{repo_dir.name}: no workflow YAML files under .github/workflows/"]
    return []


def check_archive_hygiene(
    zip_path: Path,
    plugin_name: str,
    *,
    require_single_top_dir: bool = True,
) -> list[str]:
    """Validate zip archive structure and reject leaked development/backup files."""
    errors: list[str] = []
    if not zip_path.is_file():
        return [f"archive not found: {zip_path}"]
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"invalid zip archive {zip_path.name}: {exc}"]
    if not names:
        return [f"archive is empty: {zip_path.name}"]

    top_dirs: set[str] = set()
    has_root_files = False
    for raw_name in names:
        if "\\" in raw_name or raw_name.startswith("/"):
            errors.append(f"{zip_path.name}: invalid archive path '{raw_name}'")
            continue
        parts = [p for p in raw_name.split("/") if p]
        if not parts:
            continue
        if ".." in parts:
            errors.append(f"{zip_path.name}: path traversal entry '{raw_name}'")
            continue
        if len(parts) == 1 and not raw_name.endswith("/"):
            has_root_files = True
        else:
            top_dirs.add(parts[0])

        seg_set = set(parts)
        bad_segs = sorted(seg_set & _FORBIDDEN_ARCHIVE_SEGMENTS)
        if bad_segs:
            errors.append(
                f"{zip_path.name}: forbidden dev directory {bad_segs[0]} in '{raw_name}'"
            )
            continue
        fname = parts[-1]
        if fname in _FORBIDDEN_ARCHIVE_FILENAMES or any(
            fnmatch.fnmatch(fname, pat) for pat in _FORBIDDEN_ARCHIVE_GLOBS
        ):
            errors.append(f"{zip_path.name}: forbidden dev/backup file '{raw_name}'")

    if require_single_top_dir:
        if has_root_files or top_dirs != {plugin_name}:
            errors.append(
                f"{zip_path.name}: expected single top-level directory '{plugin_name}/', "
                f"found top_dirs={sorted(top_dirs)} has_root_files={has_root_files}"
            )
    return errors


def check_lm_bridge_hygiene(repo_dir: Path) -> list[str]:
    """Verify any lm_bridge.py avoids stdlib logging and exposes detect_lm."""
    errors: list[str] = []
    for bridge in sorted(repo_dir.rglob("lm_bridge.py")):
        if set(bridge.relative_to(repo_dir).parts) & _SKIP_SCAN_DIRS:
            continue
        src = bridge.read_text(encoding="utf-8-sig", errors="ignore")
        rel = bridge.relative_to(repo_dir).as_posix()
        try:
            tree = ast.parse(src, filename=str(bridge))
        except SyntaxError as exc:
            errors.append(f"{repo_dir.name}/{rel}: syntax error: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                a.name == "logging" or a.name.startswith("logging.") for a in node.names
            ):
                errors.append(f"{repo_dir.name}/{rel}:{node.lineno}: stdlib logging import")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "logging":
                errors.append(f"{repo_dir.name}/{rel}:{node.lineno}: stdlib logging import")
        if "def detect_lm(" not in src:
            errors.append(f"{repo_dir.name}/{rel}: missing detect_lm()")
    return errors


def check_plugin_repo(
    repo_dir: Path,
    *,
    zip_path: Path | None = None,
    allow_flat_zip: bool = False,
) -> list[str]:
    """Run all single-repository CI gates on one plugin checkout."""
    errors: list[str] = []
    if not repo_dir.is_dir():
        return [f"plugin directory not found: {repo_dir}"]
    errors.extend(check_metadata_and_version(repo_dir))
    errors.extend(check_ci_workflow(repo_dir, require_ci_yml=True))
    errors.extend(check_requirements_coverage(repo_dir))
    errors.extend(check_lm_bridge_hygiene(repo_dir))
    if zip_path is not None:
        errors.extend(
            check_archive_hygiene(
                zip_path,
                repo_dir.name,
                require_single_top_dir=not allow_flat_zip,
            )
        )
    return errors


def check_series_workspace(
    workspace: Path,
    *,
    plugin: str | None = None,
    skip_sync_check: bool = False,
    zip_path: Path | None = None,
    allow_flat_zip: bool = False,
) -> list[str]:
    """Run cross-series or single-plugin CI checks."""
    errors: list[str] = []
    if not skip_sync_check and plugin is None:
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        try:
            from .sync_series_assets import check_workspace
        except ImportError:
            from sync_series_assets import check_workspace

        errors.extend(check_workspace(workspace))

    targets = (plugin,) if plugin else SERIES_PLUGINS
    for name in targets:
        repo_dir = workspace / name
        if not repo_dir.is_dir():
            if plugin:
                errors.append(f"plugin repo not found: {repo_dir}")
            continue
        errors.extend(
            check_plugin_repo(
                repo_dir,
                zip_path=zip_path if plugin else None,
                allow_flat_zip=allow_flat_zip,
            )
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--plugin", type=str, default=None)
    parser.add_argument("--skip-sync-check", action="store_true")
    parser.add_argument("--zip", dest="zip_path", type=Path, default=None)
    parser.add_argument("--allow-flat-zip", action="store_true")
    args = parser.parse_args(argv)

    errors = check_series_workspace(
        args.workspace.resolve(),
        plugin=args.plugin,
        skip_sync_check=args.skip_sync_check,
        zip_path=args.zip_path.resolve() if args.zip_path else None,
        allow_flat_zip=args.allow_flat_zip,
    )
    if errors:
        print(f"Series CI check FAILED ({len(errors)} issues):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    scope = args.plugin or f"{len(SERIES_PLUGINS)} series plugins"
    print(f"Series CI check PASSED ({scope}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
