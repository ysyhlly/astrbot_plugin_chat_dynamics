"""Native AstrBot rerank provider, used only for discussion membership."""
from __future__ import annotations

import asyncio
import json
import math
import hashlib
import time
from collections import Counter
from dataclasses import dataclass
from .exact_calls import ExactMemo, CallTimings


def provider_stamp(provider):
    return hashlib.sha256(json.dumps([
        getattr(provider, 'provider_config', None), getattr(provider, 'provider_settings', None),
        getattr(provider, 'model_name', None)], sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class DiscussionRanking:
    scores: tuple[tuple[float, str], ...] = ()
    reason: str = 'unavailable'


class DiscussionCalls:
    """Owned by the host context; no cross-account/global text cache."""
    def __init__(self):
        self.memo = ExactMemo(ttl=45, capacity=256)
        self.bindings = {}
        self.failures = {}
        self.probes = set()
        self.counts = Counter()
        self.timings = CallTimings()
        self.slots = {}

    def snapshot(self):
        now = time.monotonic()
        return {**self.counts, 'memo': self.memo.snapshot(), 'timings': self.timings.snapshot(),
                'providers_in_backoff': sum(until > now for _, until in self.failures.values()),
                'probes_in_flight': len(self.probes)}


class DiscussionReranker:
    DEFAULT_TIMEOUT = 6.0
    MAX_BACKOFF_SECONDS = 120.0
    HIGH_RULE_CONFIDENCE = 0.85

    def __init__(
        self,
        context,
        provider_id,
        *,
        timeout=DEFAULT_TIMEOUT,
        threshold=.65,
        margin=.10,
        min_candidates=2,
        high_rule_confidence=HIGH_RULE_CONFIDENCE,
    ):
        self.context, self.provider_id = context, provider_id
        self.timeout, self.threshold, self.margin = timeout, threshold, margin
        self.min_candidates = max(1, int(min_candidates))
        self.high_rule_confidence = float(high_rule_confidence)
        calls = getattr(context, '_cd_discussion_calls', None)
        if not isinstance(calls, DiscussionCalls):
            calls = DiscussionCalls()
            try:
                setattr(context, '_cd_discussion_calls', calls)
            except (AttributeError, TypeError):
                pass
        self.calls = calls

    def count(self, name, amount=1):
        self.calls.counts[name] += amount

    async def rank_scoped(self, text, candidates, scope, *, rule_confidence=None):
        return await self.rank(text, candidates, scope=scope, rule_confidence=rule_confidence)

    async def rank(self, text, candidates, *, scope=None, rule_confidence=None):
        if not self.provider_id or not text.strip() or not candidates:
            return DiscussionRanking(reason='not_configured' if not self.provider_id else 'insufficient_context')
        if rule_confidence is not None:
            try:
                if float(rule_confidence) >= self.high_rule_confidence:
                    self.calls.counts['high_confidence_skips'] += 1
                    return DiscussionRanking(reason='high_rule_confidence')
            except (TypeError, ValueError):
                pass
        if len(candidates) < self.min_candidates:
            self.calls.counts['single_candidate_skips'] += 1
            return DiscussionRanking(reason='single_candidate')
        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            fingerprint = provider_stamp(provider)
            binding = self.calls.bindings.get(self.provider_id)
            if binding is None or binding[0] is not provider or binding[1] != fingerprint:
                self.calls.bindings[self.provider_id] = (provider, fingerprint)
                self.calls.failures.pop(self.provider_id, None)
                self.calls.probes.discard(self.provider_id)
            signature = (id(provider), fingerprint)
            documents = [json.dumps({'discussion': c.summary, 'recent_messages': c.exemplars}, ensure_ascii=False)
                         for c in candidates]
            query = '判断当前消息是否延续候选中的同一具体事项；相同参与者或宽泛领域不代表同一讨论。当前消息：' + text[:800]
            key = (self.provider_id, signature, self.timeout,
                   hashlib.sha256(json.dumps([scope, query, documents, [c.topic_id for c in candidates]],
                                             sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest())
            async def invoke():
                queued = time.monotonic()
                async with self.calls.slots.setdefault(self.provider_id, asyncio.Semaphore(2)):
                    self.calls.timings.observe('rerank_queue', time.monotonic() - queued)
                    if (self.calls.bindings.get(self.provider_id) != (provider, fingerprint)
                            or self.context.get_provider_by_id(self.provider_id) is not provider
                            or provider_stamp(provider) != fingerprint):
                        return DiscussionRanking(reason='provider_changed')
                    now_mono = time.monotonic()
                    fail_count, backoff_until = self.calls.failures.get(self.provider_id, (0, 0.0))
                    if backoff_until > now_mono:
                        self.calls.counts['backoff_skips'] += 1
                        return DiscussionRanking(reason='backoff')
                    owns_probe = False
                    if fail_count > 0:
                        if self.provider_id in self.calls.probes:
                            self.calls.counts['backoff_skips'] += 1
                            return DiscussionRanking(reason='backoff')
                        self.calls.probes.add(self.provider_id)
                        owns_probe = True
                    start = time.monotonic()
                    queue_elapsed = max(0.0, start - queued)
                    if callable(getattr(provider, 'rerank', None)):
                        self.calls.counts['provider_calls'] += 1
                    result = None
                    try:
                        try:
                            result = await self._request(provider, query, documents, candidates)
                        finally:
                            gen_elapsed = max(0.0, time.monotonic() - start)
                            self.calls.timings.observe('rerank', gen_elapsed)
                            try:
                                from .. import series_panels

                                ok_flag = result is not None and result.reason == 'ranked'
                                err_label = '' if ok_flag else (result.reason if result is not None else 'rerank_error')
                                series_panels.record_series_llm_usage(
                                    self.context,
                                    plugin_id='astrbot_plugin_chat_dynamics',
                                    provider_id=str(self.provider_id),
                                    purpose='rerank',
                                    queue_seconds=queue_elapsed,
                                    generation_seconds=gen_elapsed,
                                    ok=ok_flag,
                                    error_type=err_label,
                                    group_id=str(scope or ''),
                                )
                            except Exception:
                                pass
                        if (self.calls.bindings.get(self.provider_id) != (provider, fingerprint)
                                or self.context.get_provider_by_id(self.provider_id) is not provider
                                or provider_stamp(provider) != fingerprint):
                            return DiscussionRanking(reason='provider_changed')
                        if result.reason == 'ranked':
                            self.calls.failures.pop(self.provider_id, None)
                        else:
                            count = min(self.calls.failures.get(self.provider_id, (0, 0.0))[0] + 1, 6)
                            delay = min(self.MAX_BACKOFF_SECONDS, 4.0 * (2 ** (count - 1)))
                            self.calls.failures[self.provider_id] = (count, time.monotonic() + delay)
                        self.calls.counts[result.reason] += 1
                        return result
                    finally:
                        if owns_probe:
                            self.calls.probes.discard(self.provider_id)
            # Unscoped diagnostic callers get no cross-request memoization.
            if scope is None:
                return await invoke()
            return await self.calls.memo.read(key, invoke, cacheable=lambda r: r.reason == 'ranked')
        except asyncio.CancelledError:
            raise
        except Exception:
            return DiscussionRanking(reason='unavailable')

    async def _request(self, provider, query, documents, candidates):
        try:
            if not callable(getattr(provider, 'rerank', None)):
                return DiscussionRanking(reason='wrong_provider_type')
            results = await asyncio.wait_for(provider.rerank(query=query, documents=documents, top_n=len(documents)),
                                             timeout=self.timeout)
            scores, seen = [], set()
            for row in results:
                index, score = row.index, row.relevance_score
                if (type(index) is not int or index in seen or not 0 <= index < len(candidates)
                        or type(score) not in (float, int) or not math.isfinite(score) or not 0 <= score <= 1):
                    return DiscussionRanking(reason='invalid_scores')
                seen.add(index)
                scores.append((float(score), candidates[index].topic_id))
            if len(seen) != len(candidates):
                return DiscussionRanking(reason='incomplete_scores')
            return DiscussionRanking(tuple(sorted(scores, reverse=True)), 'ranked')
        except asyncio.TimeoutError:
            return DiscussionRanking(reason='timeout')
        except Exception:
            return DiscussionRanking(reason='unavailable')
