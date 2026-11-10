"""Body-free timings around the native SDK; no tool/prompt contents retained."""
import inspect
import asyncio
import time
from collections import deque
import math


def mark_received(event):
    """Anchor the host event's creation time to this process' monotonic clock."""
    existing = getattr(event, '_cd_received_monotonic_v1', None)
    if type(existing) in (float, int) and math.isfinite(existing) and existing > 0:
        return
    now = time.monotonic()
    created = getattr(event, 'created_at', None)
    age = time.time()-created if type(created) in (float, int) and math.isfinite(created) else 0
    event._cd_received_monotonic_v1 = now-age if 0 <= age <= 3600 else now


class TimedHookTrace:
    """Observe the native dispatcher on one owned event without replacing it."""
    def __init__(self, delegate, callback, clock=time.monotonic):
        self.delegate, self.callback, self.clock = delegate, callback, clock
        self.starts = {}
        self.pending = deque(maxlen=256)

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def record(self, action, **fields):
        # Native tracing/dispatch retains its exact order and stop behavior.
        result = self.delegate.record(action, **fields)
        hook = str(fields.get('hook', ''))
        if hook not in {'OnLLMRequestEvent', 'OnLLMResponseEvent', 'OnAgentDoneEvent'}:
            return result
        key = (hook, str(fields.get('plugin', ''))[:160],
               str(fields.get('handler_full_name') or fields.get('handler', ''))[:200])
        if action == 'handler_start':
            if len(self.starts) < 128:
                self.starts.setdefault(key, []).append(self.clock())
        elif action == 'handler_end':
            starts = self.starts.get(key, [])
            if starts:
                started = starts.pop()
                if not starts:
                    self.starts.pop(key, None)
                self.pending.append(('hook/' + '/'.join(key), max(0.0, self.clock()-started),
                                     'failed' if fields.get('status') == 'error' else 'success'))
        return result

    async def flush(self):
        while self.pending:
            name, seconds, outcome = self.pending.popleft()
            value = self.callback(name, seconds, outcome)
            if inspect.isawaitable(value):
                await value


async def emit(callback, name, started, outcome='success'):
    if callback is None:
        return
    value = callback(name, time.monotonic() - started, outcome)
    if inspect.isawaitable(value):
        await value


class TimedContext:
    def __init__(self, delegate, callback):
        self.delegate, self.callback = delegate, callback

    def __getattr__(self, key):
        return getattr(self.delegate, key)

    async def process(self, *args, **kwargs):
        started, outcome = time.monotonic(), 'success'
        try:
            return await self.delegate.process(*args, **kwargs)
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except BaseException:
            outcome = 'failed'
            raise
        finally:
            await emit(self.callback, 'context_compaction', started, outcome)
