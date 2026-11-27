"""Bounded exact-input read memoization and body-free timing counters."""
import copy
import time
from collections import OrderedDict, Counter, deque

from .shared_reads import SharedReads


def _default_exact_memo_max_bytes(context=None) -> int:
    try:
        from .. import series_panels as _sp
    except Exception:
        try:
            import series_panels as _sp
        except Exception:
            _sp = None
    if _sp is not None:
        try:
            limits = _sp.get_series_resource_limits(context)
            return max(64 * 1024, int(limits.get("chat_exact_memo_max_bytes", 3 * 1024 * 1024)))
        except Exception:
            pass
    return 3 * 1024 * 1024


class ExactMemo:
    def __init__(self, *, ttl=30.0, capacity=128, max_bytes=None, clock=time.monotonic, context=None):
        resolved_max_bytes = (
            _default_exact_memo_max_bytes(context)
            if max_bytes is None
            else int(max_bytes)
        )
        self.ttl, self.capacity, self.max_bytes, self.clock = ttl, capacity, resolved_max_bytes, clock
        self.entries = OrderedDict()
        self.bytes = 0
        self.shared = SharedReads()
        self.counts = Counter()

    def _prune(self):
        now = self.clock()
        for key, (expires, _, size) in list(self.entries.items()):
            if expires <= now:
                self.entries.pop(key)
                self.bytes -= size

    async def read(self, key, factory, *, cacheable=lambda value: value is not None):
        self._prune()
        if key in self.entries:
            self.counts['cache_hits'] += 1
            self.entries.move_to_end(key)
            return copy.deepcopy(self.entries[key][1])

        async def load():
            self.counts['loads'] += 1
            value = await factory()
            if cacheable(value):
                size = len(repr(value).encode('utf-8'))
                if size <= min(self.max_bytes, 512 * 1024):
                    while self.entries and (len(self.entries) >= self.capacity or self.bytes + size > self.max_bytes):
                        _, (_, _, removed) = self.entries.popitem(last=False)
                        self.bytes -= removed
                    self.entries[key] = (self.clock() + self.ttl, copy.deepcopy(value), size)
                    self.bytes += size
            return value
        return await self.shared.read(key, load)

    def snapshot(self):
        self._prune()
        return {**self.counts, 'entries': len(self.entries), 'bytes': self.bytes,
                'inflight': len(self.shared.pending), 'coalesced_waiters': self.shared.joins}


class CallTimings:
    def __init__(self):
        self.counts = Counter()
        self.samples = {}

    def observe(self, name, seconds):
        self.counts[name] += 1
        self.samples.setdefault(name, deque(maxlen=128)).append(seconds * 1000)

    def snapshot(self):
        result = {}
        for name, samples in self.samples.items():
            rows = sorted(samples)
            result[name] = {'calls': self.counts[name], 'samples': len(rows),
                            'p50_ms': round(rows[int((len(rows)-1)*.5)], 2),
                            'p90_ms': round(rows[int((len(rows)-1)*.9)], 2)}
        return result
