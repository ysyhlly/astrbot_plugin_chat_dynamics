from pathlib import Path
import hashlib
import importlib.util
import json
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_compat", ROOT / "compat/check_compat.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def fixture(tmp_path):
    bundle, target = tmp_path / "bundle", tmp_path / "plugin"
    bundle.mkdir()
    target.mkdir()
    (bundle / "change.patch").write_bytes(b"example patch\n")
    (target / "main.py").write_bytes(b"before\n")
    row = {"id": "example", "base_version": "1", "target_version": "2", "base_commit": None,
           "patch": "change.patch", "patch_sha256": hashlib.sha256(b"example patch\n").hexdigest(),
           "files": [{"path": "main.py", "before_sha256": hashlib.sha256(b"before\n").hexdigest(),
                      "after_sha256": hashlib.sha256(b"after\n").hexdigest()}]}
    (bundle / "manifest.json").write_text(json.dumps({"partners": [row]}), encoding="utf-8")
    return bundle, target


def test_checker_rejects_operator_edits_without_changing_any_files(tmp_path):
    bundle, target = fixture(tmp_path)
    assert checker.check_bundle(bundle, "example", target)["status"] == "base_matches"
    (target / "main.py").write_bytes(b"operator edit\n")
    assert checker.check_bundle(bundle, "example", target)["status"] == "mismatch"
    assert (target / "main.py").read_bytes() == b"operator edit\n"


def test_checker_accepts_completed_update_and_rejects_tampered_patch(tmp_path):
    bundle, target = fixture(tmp_path)
    (target / "main.py").write_bytes(b"after\n")
    assert checker.check_bundle(bundle, "example", target)["status"] == "already_applied"
    (bundle / "change.patch").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="checksum"):
        checker.check_bundle(bundle, "example", target)


def test_checker_rejects_symlink_escape(tmp_path):
    target = tmp_path / "plugin"
    target.mkdir()
    with pytest.raises(ValueError):
        checker.safe_path(target, "../private.json")
    (target / "external").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        checker.safe_path(target, "external/private.json")


def test_published_patch_hashes_and_licenses_are_complete():
    bundle = ROOT / "compat"
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["partners"]) == 6
    for partner in manifest["partners"]:
        assert checker.digest((bundle / partner["patch"]).read_bytes()) == partner["patch_sha256"]
        assert (bundle / partner["license"]).is_file()
        for file in partner["files"]:
            assert not {"data", "node_modules", "__pycache__"}.intersection(Path(file["path"]).parts)
