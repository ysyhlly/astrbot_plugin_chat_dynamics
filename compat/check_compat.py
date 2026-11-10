"""Read-only compatibility check. Never applies patches or reloads a plugin."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(path: Path) -> bytes | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8")


def safe_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
        raise ValueError("unsafe manifest path")
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("target escapes plugin directory")
    return path


def check_bundle(bundle: Path, plugin: str, target: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
    row = next((row for row in manifest["partners"] if row["id"] == plugin), None)
    if row is None:
        raise ValueError("unknown companion")
    if not target.is_dir():
        raise ValueError("plugin directory does not exist")
    patch = safe_path(bundle, row["patch"])
    if digest(patch.read_bytes()) != row["patch_sha256"]:
        raise ValueError("patch checksum differs")
    before, after, different = [], [], []
    for file in row["files"]:
        data = normalized(safe_path(target, file["path"]))
        actual = None if data is None else digest(data)
        if actual == file["before_sha256"]:
            before.append(file["path"])
        elif actual == file["after_sha256"]:
            after.append(file["path"])
        else:
            different.append(file["path"])
    status = "base_matches" if not different and not after else "already_applied" if not different and not before else "mismatch"
    return {"plugin": plugin, "status": status, "base_version": row["base_version"],
            "target_version": row["target_version"], "base_commit": row.get("base_commit"),
            "checked_files": len(row["files"]), "different_files": different,
            "mixed_state": bool(before and after)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", required=True, help="companion id from manifest.json")
    parser.add_argument("--target", required=True, type=Path, help="explicit plugin SOURCE directory")
    args = parser.parse_args()
    try:
        result = check_bundle(Path(__file__).resolve().parent, args.plugin, args.target.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"base_matches", "already_applied"} else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
