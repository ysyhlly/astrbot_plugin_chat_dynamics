"""Serialized off-loop journal work and private, replayable send receipts."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import uuid


class JournalIO:
    def __init__(self, logger):
        self.logger = logger
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.loop = None

    def submit(self, label, function, *args, **kwargs):
        self.loop = asyncio.get_running_loop()
        async def run():
            async with self.lock:
                return await asyncio.to_thread(function, *args, **kwargs)
        task = asyncio.create_task(run(), name='cd-journal-' + label)
        self.tasks.add(task)
        def completed(future):
            self.tasks.discard(future)
            if not future.cancelled():
                error = future.exception()
                if error is not None:
                    self.logger.warning('Journal operation failed code=CD_JOURNAL_IO operation=%s type=%s',
                                        label, type(error).__name__)
        task.add_done_callback(completed)
        return task

    async def call(self, label, function, *args, **kwargs):
        # Cancelling the caller must not release the write lock while its thread
        # is still inside SQLite. The independent task owns the whole operation.
        return await asyncio.shield(self.submit(label, function, *args, **kwargs))

    async def drain(self):
        while self.tasks:
            await asyncio.shield(asyncio.gather(*tuple(self.tasks), return_exceptions=True))


class ReceiptOutbox:
    def __init__(self, path):
        self.path = Path(path)

    def _file(self, attempt):
        if not isinstance(attempt, str) or not attempt or len(attempt) > 160:
            raise ValueError('invalid receipt attempt')
        return self.path / (hashlib.sha256(attempt.encode()).hexdigest() + '.json')

    def save(self, packet):
        target = self._file(packet['attempt'])
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path / ('.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(packet, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def pending(self, limit=32):
        if not self.path.exists():
            return []
        packets = []
        for path in sorted(self.path.glob('*.json'))[:limit]:
            try:
                packet = json.loads(path.read_text(encoding='utf-8'))
                if packet.get('version') != 1 or self._file(packet.get('attempt')) != path:
                    raise ValueError('invalid pending receipt')
            except (ValueError, TypeError, AttributeError):
                path.replace(path.with_name(path.name + '.' + uuid.uuid4().hex + '.invalid'))
                continue
            packets.append(packet)
        return packets

    def remove(self, attempt):
        self._file(attempt).unlink(missing_ok=True)
