from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from scripts.build_release import build_archive
from scripts.check_release import iter_release_files, plugin_version, validate_release


ROOT = Path(__file__).resolve().parents[1]


def test_register_decorator_version_matches_metadata():
    version = plugin_version(ROOT)
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    match = re.search(
        r'@register\(\s*"astrbot_plugin_chat_dynamics"\s*,\s*"[^"]+"\s*,\s*"[^"]+"\s*,\s*"(v[^"]+)"\s*,\s*"([^"]*)"',
        text,
    )
    assert match is not None
    assert match.group(1) == version
    repo = match.group(2)
    assert repo == "" or repo.startswith("http")


def test_plugin_version_follows_metadata_yaml(tmp_path):
    copied = tmp_path / "plugin"
    shutil.copytree(
        ROOT,
        copied,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "dist", "artifacts", ".git", ".venv"),
    )
    metadata = copied / "metadata.yaml"
    changelog = copied / "CHANGELOG.md"
    original = plugin_version(copied)
    metadata.write_text(
        metadata.read_text(encoding="utf-8")
        .replace(f"version: {original}", "version: v9.9.9")
        .replace('repo: ""', 'repo: "https://example.invalid/chat-dynamics"'),
        encoding="utf-8",
    )
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(f"## {original}", "## v9.9.9", 1),
        encoding="utf-8",
    )
    assert plugin_version(copied) == "v9.9.9"
    assert validate_release(copied, allow_empty_repo=False) == []


def test_release_check_accepts_local_checkout_but_requires_repo_for_public_release(tmp_path):
    assert validate_release(ROOT, allow_empty_repo=True) == []
    # The checked-in tree now carries the real repository URL, so the public
    # gate passes as-is. The empty-repo rejection is exercised on a copy.
    assert validate_release(ROOT, allow_empty_repo=False) == []
    copied = tmp_path / "plugin"
    shutil.copytree(
        ROOT,
        copied,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "dist", "artifacts", ".git", ".venv"),
    )
    metadata = copied / "metadata.yaml"
    metadata.write_text(
        re.sub(
            r'^\s*repo\s*:\s*.*$',
            'repo: ""',
            metadata.read_text(encoding="utf-8"),
            count=1,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    assert validate_release(copied, allow_empty_repo=True) == []
    errors = validate_release(copied, allow_empty_repo=False)
    assert any("repo must be a real" in error for error in errors)


def test_release_file_allowlist_excludes_tests_caches_and_development_scripts():
    names = {path.relative_to(ROOT).as_posix() for path in iter_release_files(ROOT)}
    assert names
    assert all(not name.startswith("tests/") for name in names)
    assert all(not name.startswith("integration/") for name in names)
    assert all(not name.startswith("scripts/") for name in names)
    assert all("__pycache__" not in name and not name.endswith(".pyc") for name in names)
    assert ".astrbot-plugin/i18n/zh-CN.json" in names


def test_release_archive_is_deterministic_when_metadata_has_a_real_repo(tmp_path):
    # Work on a temporary copy so this test never changes the checked-in
    # metadata or leaves a dist artifact in the workspace.
    copied = tmp_path / "plugin"
    shutil.copytree(ROOT, copied, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "dist", "artifacts", ".git", ".venv"))
    metadata = copied / "metadata.yaml"
    metadata.write_text(
        metadata.read_text(encoding="utf-8").replace('repo: ""', 'repo: "https://example.invalid/chat-dynamics"'),
        encoding="utf-8",
    )

    first = build_archive(copied, tmp_path / "one.zip")
    second = build_archive(copied, tmp_path / "two.zip")
    first_hash = hashlib.sha256(first[0].read_bytes()).hexdigest()
    second_hash = hashlib.sha256(second[0].read_bytes()).hexdigest()
    assert first_hash == second_hash
    manifest = json.loads(first[1].read_text(encoding="utf-8"))
    assert manifest["version"] == plugin_version(copied)
    assert all(not item["path"].startswith(("tests/", "scripts/")) for item in manifest["files"])
