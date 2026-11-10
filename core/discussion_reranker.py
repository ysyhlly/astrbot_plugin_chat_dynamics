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
        self.counts = Counter()
        self.timings = CallTimings()
        self.slots = {}

    def snapshot(self):
        now = time.monotonic()
        return {**self.counts, 'memo': self.memo.snapshot(), 'timings': self.timings.snapshot(),
                'providers_in_backoff': sum(until > now for _, until in self.failures.values())}


class DiscussionReranker:
    def __init__(self, context, provider_id, *, timeout=3.0, threshold=.65, margin=.10):
        self.context, self.provider_id = context, provider_id
        self.timeout, self.threshold, self.margin = timeout, threshold, margin
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

    async def rank_scoped(self, text, candidates, scope):
        return await self.rank(text, candidates, scope=scope)

    async def rank(self, text, candidates, *, scope=None):
        if not self.provider_id or not text.strip() or not candidates:
            return DiscussionRanking(reason='not_configured' if not self.provider_id else 'insufficient_context')
        try:
            provider = self.context.get_provider_by_id(self.provider_id)
            fingerprint = provider_stamp(provider)
            binding = self.calls.bindings.get(self.provider_id)
            if binding is None or binding[0] is not provider or binding[1] != fingerprint:
                self.calls.bindings[self.provider_id] = (provider, fingerprint)
                self.calls.failures.pop(self.provider_id, None)
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
                    if self.calls.failures.get(self.provider_id, (0, 0))[1] > time.monotonic():
                        self.calls.counts['backoff_skips'] += 1
                        return DiscussionRanking(reason='backoff')
                    start = time.monotonic()
                    if callable(getattr(provider, 'rerank', None)):
                        self.calls.counts['provider_calls'] += 1
                    try:
                        result = await self._request(provider, query, documents, candidates)
                    finally:
                        self.calls.timings.observe('rerank', time.monotonic() - start)
                    if (self.calls.bindings.get(self.provider_id) != (provider, fingerprint)
                            or self.context.get_provider_by_id(self.provider_id) is not provider
                            or provider_stamp(provider) != fingerprint):
                        return DiscussionRanking(reason='provider_changed')
                    if result.reason == 'ranked':
                        self.calls.failures.pop(self.provider_id, None)
                    else:
                        count = min(self.calls.failures.get(self.provider_id, (0, 0))[0] + 1, 5)
                        self.calls.failures[self.provider_id] = (count, time.monotonic() + min(30, 2 ** count))
                    self.calls.counts[result.reason] += 1
                    return result
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
