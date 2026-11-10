"""Prepare a pinned, patched LivingMemory SOURCE fixture in this checkout.

Never installs or loads a plugin. Existing nonmatching directories are rejected.
"""
from pathlib import Path, PurePosixPath
import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def prepare(destination: Path, archive: Path | None = None) -> Path:
    destination = destination.resolve()
    if destination != ROOT / "data/plugins/astrbot_plugin_livingmemory":
        raise ValueError("fixture must stay in this checkout's ignored data/plugins directory")
    bundle = ROOT / "compat"
    spec = importlib.util.spec_from_file_location("fixture_compat_check", bundle / "check_compat.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8-sig"))
    partner = next(row for row in manifest["partners"] if row["id"] == "livingmemory")
    if destination.exists():
        if checker.check_bundle(bundle, "livingmemory", destination)["status"] == "already_applied":
            return destination
        raise ValueError("existing fixture differs; no files were replaced")
    repository, commit = partner["repository"], partner["base_commit"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid pinned source")
    with tempfile.TemporaryDirectory(prefix="cd-companion-source-") as temporary:
        temporary = Path(temporary)
        if archive is None:
            archive = temporary / "source.zip"
            request = urllib.request.Request("https://codeload.github.com/" + repository + "/zip/" + commit,
                                             headers={"User-Agent": "ChatDynamics-companion-fixture"})
            with urllib.request.urlopen(request, timeout=60) as response:
                archive.write_bytes(response.read())
        stage = temporary / "source"
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            # git archive caches have no enclosing directory; codeload does.
            prefix = "" if "metadata.yaml" in names else names[0].split("/")[0] + "/"
            for info in source.infolist():
                if info.is_dir():
                    continue
                relative = PurePosixPath(info.filename[len(prefix):])
                if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative) or ":" in str(relative):
                    raise ValueError("unsafe source archive")
                path = stage / str(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.read(info))
        assert checker.check_bundle(bundle, "livingmemory", stage)["status"] == "base_matches"
        for row in partner["files"]:
            if row["before_sha256"] is not None:
                path = stage / row["path"]
                path.write_bytes(checker.normalized(path))
        patch = str((bundle / partner["patch"]).resolve())
        subprocess.run(["git", "apply", "--check", patch], cwd=stage, check=True)
        subprocess.run(["git", "apply", patch], cwd=stage, check=True)
        assert checker.check_bundle(bundle, "livingmemory", stage)["status"] == "already_applied"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, destination)
    for package in (destination.parent, destination.parent.parent):
        marker = package / "__init__.py"
        if not marker.exists():
            marker.write_bytes(b"")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="optional local copy of the pinned source archive")
    args = parser.parse_args()
    print(json.dumps({"fixture": str(prepare(args.destination, args.archive)), "loaded": False}))
