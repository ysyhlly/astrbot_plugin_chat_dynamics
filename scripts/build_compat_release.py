"""Build deterministic companion sources, separate from the plugin installer."""
from pathlib import Path
import hashlib
import json
import zipfile

try:
    from .check_release import plugin_version
except ImportError:
    from check_release import plugin_version


def build_compat_archive(root: Path, output: Path) -> tuple[Path, Path, Path]:
    source = root / "compat"
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest["chat_dynamics"] != plugin_version(root):
        raise ValueError("compatibility version differs")
    for companion in manifest["partners"]:
        patch = source / companion["patch"]
        if not patch.resolve().is_relative_to(source.resolve()):
            raise ValueError("unsafe patch path")
        if hashlib.sha256(patch.read_bytes()).hexdigest() != companion["patch_sha256"]:
            raise ValueError("patch differs: " + companion["id"])
        if not (source / companion["license"]).is_file():
            raise ValueError("missing companion license")
    files = sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    for path in files:
        if path.suffix == ".py":
            compile(path.read_bytes(), path.relative_to(source).as_posix(), "exec")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo("compat/" + path.relative_to(source).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n", encoding="ascii")
    record = output.with_suffix(output.suffix + ".manifest.json")
    record.write_text(json.dumps({"version": manifest["chat_dynamics"], "archive": output.name,
                                 "files": [{"path": p.relative_to(source).as_posix(),
                                            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                                           for p in files]}, indent=2) + "\n", encoding="utf-8")
    return output, record, checksum


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    archive, manifest, checksum = build_compat_archive(root, root / "dist" / ("chat-dynamics-compat-" + plugin_version(root) + ".zip"))
    print(json.dumps({"archive": str(archive), "manifest": str(manifest), "sha256": str(checksum)}))
