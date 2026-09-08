"""Build a deterministic, runtime-only Chat Dynamics release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

try:
    from .check_release import iter_release_files, plugin_version, validate_release
except ImportError:  # direct ``python scripts/build_release.py`` execution
    from check_release import iter_release_files, plugin_version, validate_release


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive(
    root: Path, output: Path, *, allow_empty_repo: bool = False
) -> tuple[Path, Path, Path]:
    errors = validate_release(root, allow_empty_repo=allow_empty_repo)
    if errors:
        raise RuntimeError("; ".join(errors))
    files = list(iter_release_files(root))
    version = plugin_version(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())

    manifest = output.with_suffix(output.suffix + ".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "version": version,
                "archive": output.name,
                "files": [
                    {
                        "path": path.relative_to(root).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                    for path in files
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{_sha256(output)}  {output.name}\n", encoding="ascii")
    return output, manifest, checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-empty-repo",
        action="store_true",
        help="build a local install zip when metadata.yaml repo is still empty",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    version = plugin_version(root)
    output = (args.output or (root / f"dist/astrbot_plugin_chat_dynamics-{version}.zip")).resolve()
    try:
        archive, manifest, checksum = build_archive(
            root, output, allow_empty_repo=args.allow_empty_repo
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Release build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"archive": str(archive), "manifest": str(manifest), "sha256": str(checksum)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
