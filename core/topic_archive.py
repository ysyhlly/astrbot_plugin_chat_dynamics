"""Bounded in-memory topic summaries, owned by a single routing session."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .semantics import lexical_tokens


@dataclass(frozen=True)
class ArchivedTopic:
    topic_id: str
    summary: str
    exemplars: tuple[str, ...]
    participants: frozenset[str]
    updated_at: float
    archived_at: float


@dataclass(frozen=True)
class ArchiveMatch:
    topic_id: str
    score: float
    margin: float
    topic: ArchivedTopic


class TopicArchive:
    """Extractive retrieval without network calls or newer host SDK features.

    Call archive before active message IDs are pruned. Retrieval is deliberately
    conservative and should run only after active candidates have scored low.
    A match identifies a topic, never a reply parent or a bot addressee.
    """

    def __init__(self, max_topics: int = 32, ttl_seconds: float = 86400.0,
                 threshold: float = 0.72, margin: float = 0.08):
        self.max_topics = max(0, min(256, int(max_topics)))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.threshold = float(threshold)
        self.margin = float(margin)
        self.entries: dict[str, ArchivedTopic] = {}

    def clear(self) -> None:
        self.entries.clear()

    def pop(self, topic_id: str) -> ArchivedTopic | None:
        return self.entries.pop(topic_id, None)

    def prune(self, now: float) -> None:
        for key, topic in list(self.entries.items()):
            if not 0 <= now - topic.updated_at <= self.ttl_seconds:
                del self.entries[key]
        while len(self.entries) > self.max_topics:
            oldest = min(self.entries, key=lambda key: self.entries[key].updated_at)
            del self.entries[oldest]

    @staticmethod
    def _substantive(text: str) -> bool:
        return len(re.sub(r"\W", "", text)) >= 8 and len(lexical_tokens(text)) >= 3

    def archive(self, topic: Any, dag: Any, now: float) -> ArchivedTopic | None:
        self.prune(now)
        texts = [dag.nodes[mid].text for mid in topic.message_ids if mid in dag.nodes]
        # These cached exemplars survive eviction of the original DAG nodes.
        texts.extend(getattr(topic, "summary_excerpts", ()))
        texts = list(dict.fromkeys(text.strip()[:320] for text in texts
                                   if self._substantive(text.strip()))) [-4:]
        updated_at = float(topic.updated_at)
        if not texts or not topic.topic_id or not 0 <= now - updated_at <= self.ttl_seconds:
            return None
        entry = ArchivedTopic(topic.topic_id, "\n".join(texts), tuple(texts),
                              frozenset(sorted(topic.participants)[:32]), updated_at, now)
        self.entries[entry.topic_id] = entry
        self.prune(now)
        return self.entries.get(entry.topic_id)

    def retrieve(self, node: Any, dag: Any) -> ArchiveMatch | None:
        self.prune(node.timestamp)
        if not self._substantive(node.text):
            return None
        query_tokens = lexical_tokens(node.text)
        ranked: list[tuple[float, str]] = []
        for topic in self.entries.values():
            best = 0.0
            for text in topic.exemplars:
                # Shared concrete surface evidence is required: broad concept
                # similarity alone must not merge unrelated technical topics.
                exemplar_tokens = lexical_tokens(text)
                overlap = query_tokens & exemplar_tokens
                if len(overlap) < 2:
                    continue
                try:
                    score = float(dag.semantic_match_fn(node.text, text).score)
                except (TypeError, ValueError, AttributeError):
                    continue
                if math.isfinite(score):
                    # The fallback matcher caps concept-free identical text at
                    # 0.7; substantial lexical agreement is independent evidence.
                    score = max(score, 0.90 * len(overlap) / len(query_tokens | exemplar_tokens))
                    best = max(best, min(1.0, max(0.0, score)))
            if best:
                ranked.append((best, topic.topic_id))
        ranked.sort(reverse=True)
        if not ranked:
            return None
        score, topic_id = ranked[0]
        margin = score - (ranked[1][0] if len(ranked) > 1 else 0.0)
        if score < self.threshold or margin < self.margin:
            return None
        return ArchiveMatch(topic_id, score, margin, self.entries[topic_id])
