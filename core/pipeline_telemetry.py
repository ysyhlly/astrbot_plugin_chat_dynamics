"""Bounded per-turn timings, separate from persisted lifetime counters."""
import asyncio
import hashlib
import time
from collections import deque


class PipelineTelemetry:
    def __init__(self, maximum=512):
        self.started_at = time.time()
        self.samples = deque(maxlen=maximum)
        self.hook_samples = deque(maxlen=512)
        self.deliveries = deque(maxlen=256)

    def record_hook(self, stage, turn, seconds, outcome='success'):
        self.hook_samples.append({'turn_id': self.turn_id(turn), 'stage': stage,
            'outcome': outcome, 'elapsed_ms': round(max(0.0, seconds)*1000, 1), 'at': time.time()})

    def record_delivery(self, turn, seconds):
        self.deliveries.append({'turn_id': self.turn_id(turn), 'at': time.time(),
            'stages': {name: round(max(0.0, value)*1000, 1) for name, value in seconds.items()}})

    @staticmethod
    def turn_id(turn):
        return hashlib.sha256((turn.session_key + "|" + "|".join(m.message_id for m in turn.messages)).encode()).hexdigest()[:32]

    def record(self, stage, turn, seconds, outcome='success'):
        self.samples.append({'turn_id': self.turn_id(turn), 'stage': stage, 'outcome': outcome,
                             'elapsed_ms': round(max(0.0, seconds) * 1000, 1), 'at': time.time()})

    async def run(self, stage, turn, operation):
        key = self.turn_id(turn)
        started, outcome = time.monotonic(), "success"
        try:
            return await operation
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            self.samples.append({"turn_id": key, "stage": stage, "outcome": outcome,
                                 "elapsed_ms": round((time.monotonic() - started) * 1000, 1), "at": time.time()})

    def snapshot(self):
        grouped = {}
        for row in self.samples:
            grouped.setdefault(row["stage"], []).append(row)
        stages = {}
        for stage, rows in grouped.items():
            values = sorted(row["elapsed_ms"] for row in rows)
            stages[stage] = {"count": len(rows), "p50_ms": values[(len(values) - 1) // 2],
                             "p90_ms": values[min(len(values) - 1, int(len(values) * .9))],
                             "failed": sum(r["outcome"] == "failed" for r in rows),
                             "cancelled": sum(r["outcome"] == "cancelled" for r in rows)}
        hooks = {}
        for row in self.hook_samples:
            hooks.setdefault(row['stage'], []).append(row['elapsed_ms'])
        hook_stats = {name: {'count': len(values), 'p50_ms': sorted(values)[(len(values)-1)//2],
                            'p90_ms': sorted(values)[min(len(values)-1, int(len(values)*.9))]}
                      for name, values in hooks.items()}
        delivered = {}
        for row in self.deliveries:
            for name, value in row['stages'].items():
                delivered.setdefault(name, []).append(value)
        delivery_stats = {name: {'count': len(values), 'p50_ms': sorted(values)[(len(values)-1)//2],
                                'p90_ms': sorted(values)[min(len(values)-1, int(len(values)*.9))],
                                'failed': 0, 'cancelled': 0} for name, values in delivered.items()}
        stages.update(delivery_stats)
        return {"window": "bounded_samples_since_reload", "started_at": self.started_at,
                "sample_limit": self.samples.maxlen, "stages": stages, "recent": list(self.samples)[-16:],
                'hooks': hook_stats, 'hook_sample_limit': self.hook_samples.maxlen,
                'delivered': {'count': len(self.deliveries), 'sample_limit': self.deliveries.maxlen,
                              'stages': delivery_stats, 'recent': list(self.deliveries)[-16:]}}
