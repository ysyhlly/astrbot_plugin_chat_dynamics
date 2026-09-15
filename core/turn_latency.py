"""Bounded stage timing for a turn, using elapsed time rather than wall time."""
from __future__ import annotations

import math
import time


def start_turn(node, started=None):
    node.metadata['turn_latency'] = {'started': time.perf_counter() if started is None else started,
                                     'stages': {}, 'degraded': []}


def record_stage(node, name, started):
    timing = node.metadata.get('turn_latency')
    if isinstance(timing, dict):
        timing.setdefault('stages', {})[name] = max(0.0, time.perf_counter() - started)


def degrade(node, reason):
    timing = node.metadata.get('turn_latency')
    if isinstance(timing, dict) and reason not in timing.setdefault('degraded', []):
        timing['degraded'].append(reason)


def summarize_turn_latencies(dags):
    """Recent retained turns only; no message content or persistent collection."""
    stages = {}
    degraded = {}
    sampled = 0
    for dag in sorted(dags.values(), key=lambda value: value.last_timestamp(), reverse=True):
        if sampled >= 2048:
            break
        for node in dag.get_recent_nodes(min(64, 2048 - sampled)):
            sampled += 1
            timing = node.metadata.get('turn_latency', {})
            for stage, value in timing.get('stages', {}).items():
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    stages.setdefault(stage, []).append(value)
            first = timing.get('first_send_seconds')
            if isinstance(first, (int, float)) and math.isfinite(first) and first >= 0:
                stages.setdefault('first_send', []).append(first)
            for reason in timing.get('degraded', []):
                degraded[reason] = degraded.get(reason, 0) + 1
    def percentiles(values):
        values.sort()
        return {'count': len(values), 'p50_seconds': values[math.ceil(len(values) * .5) - 1],
                'p95_seconds': values[math.ceil(len(values) * .95) - 1]}
    return {'scope': 'recent_retained_turn_sample', 'sampled_nodes': sampled, 'sample_limit': 2048,
            'stages': {k: percentiles(v) for k, v in stages.items()},
            'degraded': degraded}
