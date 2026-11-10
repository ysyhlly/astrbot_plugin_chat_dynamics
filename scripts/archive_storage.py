"""Create/restore verified SQLite archives without deleting or changing the source.

Run outside the bot process. SQLite backup includes committed WAL pages. Restore
always targets a new file; replacing a running database is deliberately excluded.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify_database(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")


def archive_database(source, output_dir, timeout=600):
    source, output_dir = Path(source).resolve(strict=True), Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Reserve room for the snapshot and a worst-case incompressible gzip copy.
    if shutil.disk_usage(output_dir).free < source.stat().st_size * 2 + 64 * 1024 ** 2:
        raise OSError("Insufficient archive space; source was not changed")
    name = source.name + "." + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "." + uuid.uuid4().hex[:12]
    archive = output_dir / (name + ".gz")
    manifest = output_dir / (name + ".json")
    deadline = time.monotonic() + timeout

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite snapshot deadline exceeded")

    # All temporary files are created under this resolved, explicit destination.
    with tempfile.TemporaryDirectory(prefix=".sqlite-archive-", dir=output_dir) as temp:
        snapshot = Path(temp) / "snapshot.sqlite3"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst, pages=1024, progress=progress)
        verify_database(snapshot)
        expected = digest(snapshot)
        partial = Path(temp) / "archive.gz"
        with snapshot.open("rb") as src, gzip.open(partial, "wb", compresslevel=3) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        actual = hashlib.sha256()
        with gzip.open(partial, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual.update(chunk)
        if actual.hexdigest() != expected:
            raise ValueError("Archive round-trip checksum mismatch")
        info = {"schema_version": 1, "source_name": source.name,
                "archive": archive.name, "snapshot_sha256": expected,
                "archive_sha256": digest(partial), "snapshot_bytes": snapshot.stat().st_size,
                "archive_bytes": partial.stat().st_size, "sqlite_quick_check": "ok",
                "source_deleted": False}
        os.replace(partial, archive)
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(info, stream, indent=2)
        return info


def restore_archive(manifest, destination):
    manifest, destination = Path(manifest).resolve(strict=True), Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Restore requires a new destination")
    info = json.loads(manifest.read_text(encoding="utf-8"))
    archive = manifest.parent / info["archive"]
    if archive.resolve().parent != manifest.parent or digest(archive) != info["archive_sha256"]:
        raise ValueError("Invalid archive path or checksum")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sqlite-restore-", dir=destination.parent) as temp:
        restored = Path(temp) / "restored.sqlite3"
        with gzip.open(archive, "rb") as src, restored.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if digest(restored) != info["snapshot_sha256"]:
            raise ValueError("Restored database checksum mismatch")
        verify_database(restored)
        # Same-filesystem hard link publishes the verified file atomically and
        # fails if the destination appears in a race. No partial restore is exposed.
        os.link(restored, destination)
    return {"restored": str(destination), "sqlite_quick_check": "ok"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("archive")
    save.add_argument("source")
    save.add_argument("output_dir")
    restore = commands.add_parser("restore")
    restore.add_argument("manifest")
    restore.add_argument("destination")
    args = parser.parse_args()
    result = (archive_database(args.source, args.output_dir) if args.command == "archive"
              else restore_archive(args.manifest, args.destination))
    print(json.dumps(result, ensure_ascii=False))
