"""Unified parameterized release, packaging, and health-gate CLI for the 8 Dynamics plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SERIES_PLUGINS: tuple[str, ...] = (
    "astrbot_plugin_chat_dynamics",
    "astrbot_plugin_dynamics_world",
    "astrbot_plugin_dynamics_learning",
    "astrbot_plugin_dynamics_memory",
    "astrbot_plugin_dynamics_horizons",
    "astrbot_plugin_dynamics_games",
    "astrbot_plugin_visual_media",
    "astrbot_plugin_group_chronicle",
)
_VERSION_RE = re.compile(r"^\s*version\s*:\s*['\"]?([^'\"\s]+)", re.MULTILINE)
_MAX_MARKETPLACE_ZIP_BYTES = 16 * 1024 * 1024
_SUSPICIOUS_RE = re.compile(
    r"(\.db$|\.sqlite|\.pem$|\.key$|\.env$|\.env\.|__pycache__|\.pyc$|\.ruff_cache|\.pytest_cache|\.mypy_cache)",
    re.I,
)
_SOURCE_SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules", "dist", "artifacts"}
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_version(repo: Path) -> str:
    meta = (repo / "metadata.yaml").read_text(encoding="utf-8-sig")
    m = _VERSION_RE.search(meta)
    if not m:
        raise ValueError(f"Missing version in {repo.name}/metadata.yaml")
    return m.group(1).strip()


def _build_source_zip(repo: Path, out_path: Path) -> Path:
    """Build a deterministic source zip directly from git-tracked files (no external template dependency)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    if (repo / ".git").exists():
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "ls-files"],
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).decode("utf-8", errors="ignore")
        for rel in sorted(line.strip() for line in raw.splitlines() if line.strip()):
            p = repo / rel
            if p.is_file() and not (set(p.relative_to(repo).parts) & _SOURCE_SKIP_DIRS):
                files.append(p)
    else:
        for p in sorted(repo.rglob("*")):
            if p.is_file() and not (set(p.relative_to(repo).parts) & _SOURCE_SKIP_DIRS):
                files.append(p)

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            rel = f"{repo.name}/{p.relative_to(repo).as_posix()}"
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, p.read_bytes())
    return out_path


def _build_install_zip(repo: Path, version: str, out_dir: Path) -> Path:
    """Invoke each plugin's native deterministic builder into a unified install ZIP path."""
    name = repo.name
    out_dir.mkdir(parents=True, exist_ok=True)
    install_zip = out_dir / f"{name}-{version}-install.zip"

    if name == "astrbot_plugin_chat_dynamics":
        cmd = [sys.executable, str(repo / "scripts/build_release.py"), "--root", str(repo), "--output", str(install_zip)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"build_release failed for {name}: {res.stderr or res.stdout}")
    elif name == "astrbot_plugin_dynamics_world":
        cmd = [sys.executable, str(repo / "build_release.py"), str(install_zip)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"build_release failed for {name}: {res.stderr or res.stdout}")
    elif name == "astrbot_plugin_visual_media":
        cmd = [sys.executable, str(repo / "tools/build_release.py"), "--root", str(repo), "--output", str(out_dir)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"build_release failed for {name}: {res.stderr or res.stdout}")
    else:
        packager = repo / "tools/package_release.py"
        if not packager.is_file():
            raise FileNotFoundError(f"No packager found for {name}")
        runpy.run_path(str(packager))["build"](repo, out_dir)

    if not install_zip.is_file():
        candidates = sorted(out_dir.glob(f"{name}*-install.zip"))
        if candidates:
            install_zip = candidates[0]
        else:
            raise FileNotFoundError(f"Expected install archive not produced for {name} in {out_dir}")
    return install_zip


def package_plugin(repo: Path, out_dir: Path, *, check_determinism: bool = True) -> dict:
    """Build and audit install + source archives for one series plugin."""
    version = _read_version(repo)
    install_zip = _build_install_zip(repo, version, out_dir)
    source_zip = out_dir / f"{repo.name}-{version}-source.zip"
    if not source_zip.is_file():
        _build_source_zip(repo, source_zip)

    size_bytes = install_zip.stat().st_size
    if size_bytes > _MAX_MARKETPLACE_ZIP_BYTES:
        raise RuntimeError(
            f"{repo.name} install zip exceeds 16 MB marketplace limit ({size_bytes / (1024 * 1024):.2f} MB)"
        )

    with zipfile.ZipFile(install_zip) as zf:
        if zf.testzip() is not None:
            raise RuntimeError(f"Corrupt zip: {install_zip}")
        names = zf.namelist()
        bad = [n for n in names if _SUSPICIOUS_RE.search(n)]
        if bad:
            raise RuntimeError(f"Suspicious files in {install_zip.name}: {bad[:5]}")

    if check_determinism:
        with tempfile.TemporaryDirectory() as td:
            install_zip_2 = _build_install_zip(repo, version, Path(td))
            if _sha256_file(install_zip) != _sha256_file(install_zip_2):
                raise RuntimeError(f"Non-deterministic install zip detected for {repo.name}")

    return {
        "plugin": repo.name,
        "version": version,
        "install_zip": str(install_zip),
        "install_size_bytes": size_bytes,
        "install_sha256": _sha256_file(install_zip),
        "source_zip": str(source_zip),
        "source_sha256": _sha256_file(source_zip),
    }


def _import_verify_module():
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    try:
        from . import verify_series_integration

        return verify_series_integration
    except ImportError:
        import verify_series_integration

        return verify_series_integration


def cmd_audit(args: argparse.Namespace) -> int:
    verify_main = _import_verify_module().main

    verify_argv = ["--workspace", str(args.workspace)]
    if args.skip_asset_sync:
        verify_argv.append("--skip-asset-sync")
    if args.boot_log:
        verify_argv.extend(["--boot-log", str(args.boot_log)])
    return verify_main(verify_argv)


def cmd_package(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    out_dir = args.output.resolve()
    targets = tuple(args.plugins) if args.plugins else SERIES_PLUGINS
    results = []
    for name in targets:
        repo = workspace / name
        if not repo.is_dir():
            print(f"[SKIP] {name} not found under {workspace}", file=sys.stderr)
            continue
        info = package_plugin(repo, out_dir, check_determinism=not args.no_determinism)
        results.append(info)
        print(
            f"[OK] {name} {info['version']} -> {info['install_size_bytes'] / 1024:.1f} KB "
            f"sha256:{info['install_sha256'][:12]}"
        )
    summary_path = out_dir / "series-package-report.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def cmd_verify_boot(args: argparse.Namespace) -> int:
    check_boot_log_health = _import_verify_module().check_boot_log_health

    errs = check_boot_log_health(args.boot_log.resolve())
    if errs:
        for e in errs:
            print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    print(f"[OK] Boot log health gate passed: {args.boot_log}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="Run cross-8-plugin integration and hygiene checks")
    p_audit.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    p_audit.add_argument("--skip-asset-sync", action="store_true")
    p_audit.add_argument("--boot-log", type=Path, default=None)
    p_audit.set_defaults(func=cmd_audit)

    p_pkg = sub.add_parser("package", help="Build and audit deterministic ZIPs for selected/all plugins")
    p_pkg.add_argument("--workspace", type=Path, default=WORKSPACE_ROOT)
    p_pkg.add_argument("--output", type=Path, required=True)
    p_pkg.add_argument("--plugins", nargs="*", default=[])
    p_pkg.add_argument("--no-determinism", action="store_true")
    p_pkg.set_defaults(func=cmd_package)

    p_boot = sub.add_parser("verify-boot", help="Run deployment health gate on an AstrBot boot log")
    p_boot.add_argument("--boot-log", type=Path, required=True)
    p_boot.set_defaults(func=cmd_verify_boot)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
