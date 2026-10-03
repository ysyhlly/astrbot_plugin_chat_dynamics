"""Bounded, session-scoped local attachments referenced by committed history."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from .persist import atomic_write_json


class MediaArchive:
    def __init__(self, root, *, ttl=7 * 86400, max_bytes=256 * 1024 * 1024, max_entries=512, now=time.time):
        self.root = Path(root)
        self.ttl, self.max_bytes, self.max_entries, self.now = ttl, max_bytes, max_entries, now
        self.active = set()

    def _entries(self):
        if not self.root.exists():
            return []
        return [path for owner in self.root.iterdir() if owner.is_dir() and not owner.is_symlink()
                for path in owner.iterdir() if path.is_dir() and not path.is_symlink()]

    def prune(self):
        for directory in self._entries():
            if directory in self.active:
                continue
            try:
                manifest = directory / 'lease.json'
                expires = json.loads(manifest.read_text())['expires_at'] if manifest.exists() else directory.stat().st_mtime + self.ttl
                if float(expires) <= self.now():
                    self.release(directory)
            except (OSError, ValueError, KeyError, TypeError):
                # An unreadable receipt must not erase a live attachment.
                continue

    def reserve(self, size):
        self.prune()
        entries = self._entries()
        total = sum(path.stat().st_size for directory in entries for path in directory.iterdir()
                    if path.is_file() and not path.is_symlink())
        if total + size > self.max_bytes:
            raise OSError('conversation attachment storage is full')
        return len(entries)

    def create_directory(self, session):
        if self.reserve(0) >= self.max_entries:
            raise OSError('conversation attachment entry limit reached')
        owner = hashlib.sha256(str(session).encode()).hexdigest()
        directory = self.root / owner / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        self.active.add(directory)
        return directory

    def release(self, directory):
        directory = Path(directory)
        self.active.discard(directory)
        shutil.rmtree(directory, ignore_errors=True)
        try:
            directory.parent.rmdir()
        except OSError:
            pass

    def retain(self, event, session, conversation_id):
        cleanup = getattr(event, '_chat_dynamics_media_cleanup', None)
        if cleanup is None or not cleanup.alive:
            return
        directory = Path(event._chat_dynamics_media_directory)
        owner = hashlib.sha256(str(session).encode()).hexdigest()
        if directory.parent != self.root / owner or directory not in self.active:
            raise ValueError('attachment owner does not match conversation')
        atomic_write_json(directory / 'lease.json', {
            'conversation_hash': hashlib.sha256(str(conversation_id).encode()).hexdigest(),
            'expires_at': self.now() + self.ttl,
        })
        cleanup.detach()
        self.active.discard(directory)

    def touch(self, session, conversation_id, *, history=None):
        """Refresh referenced conversation leases without touching other sessions."""
        owner = self.root / hashlib.sha256(str(session).encode()).hexdigest()
        if not owner.is_dir() or owner.is_symlink():
            return
        digest = hashlib.sha256(str(conversation_id).encode()).hexdigest()
        try:
            referenced = None if history is None else json.dumps(
                json.loads(history) if isinstance(history, str) else history, ensure_ascii=False)
        except (TypeError, ValueError):
            return  # An unreadable history cannot authorize deletion.
        for directory in owner.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest = directory / 'lease.json'
            try:
                data = json.loads(manifest.read_text())
                if data.get('conversation_hash') == digest and float(data['expires_at']) > self.now():
                    # The immutable directory UUID survives JSON/URI escaping
                    # of Unicode or Windows path prefixes in tool arguments.
                    if (referenced is not None and directory not in self.active
                            and directory.name not in referenced):
                        self.release(directory)
                        continue
                    data['expires_at'] = self.now() + self.ttl
                    atomic_write_json(manifest, data)
            except (OSError, ValueError, KeyError, TypeError):
                continue
