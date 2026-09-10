"""Bounded topic profiles and conservative topic membership evidence."""
from __future__ import annotations

import math
import re
from collections import Counter
from copy import copy
from typing import Any

from .graph import ConversationNode
from .session_runtime import RoutingState, TopicState
from .semantics import cosine, hashed_embedding, lexical_tokens

_BOUNDARY = re.compile(r"^(?:对了|话说|顺便问一下|另外|换个话题|说到这个|by the way)", re.I)
_ELLIPTICAL = re.compile(r"^(?:那|这个|这样|然后|继续|怎么办|为什么|怎么设|默认|我默认(?:的)?|好的|行|对|确实|好使|不行|成|那这个|这个呢)[呢？?。!！\s]*$")
_FILLER = re.compile(r"^(?:哈+|嗯+|哦+|好的|好|ok|233+|[？?!！]+)$", re.I)


def is_elliptical(text: str) -> bool:
    return bool(_ELLIPTICAL.fullmatch(text.strip()))


def build_contextual_query(node: ConversationNode, dag: Any, candidate_topic: TopicState | None = None, max_len: int = 12) -> str:
    """Build candidate-local context for a reranker, never a pairwise similarity query.

    Without a candidate, return the original utterance. Borrowing a speaker's
    previous unrelated utterance before candidate retrieval biases all scores.
    """
    text = node.text.strip()
    if candidate_topic is None or dag is None or not (is_elliptical(text) or len(text) <= max_len):
        return text
    recent = [dag.nodes[mid] for mid in candidate_topic.message_ids if mid in dag.nodes
              and mid != node.msg_id and 0 < node.timestamp - dag.nodes[mid].timestamp <= 90][-2:]
    context = "\n".join(f"{n.user_id}: {n.text[:240]}" for n in recent)
    return f"Context:\n{context}\nCurrent ({node.user_id}): {text}" if context else text


class TopicResolver:
    def __init__(self, join_threshold=0.58, ambiguity_threshold=0.48,
                 margin_threshold=0.06, window_seconds=300.0, max_exemplars=5):
        self.join_threshold = join_threshold
        self.ambiguity_threshold = ambiguity_threshold
        self.margin_threshold = margin_threshold
        self.window_seconds = window_seconds
        self.max_exemplars = max(1, min(5, max_exemplars))

    @staticmethod
    def remember(state: RoutingState, node: ConversationNode, topic_id: str, dag: Any = None) -> None:
        for topic in state.topics.values():
            if node.msg_id in topic.message_ids:
                topic.message_ids.remove(node.msg_id)
        topic = state.topics.setdefault(topic_id, TopicState(topic_id))
        topic.message_ids.append(node.msg_id)
        if topic.generated_title:
            node.metadata["topic_title"] = topic.generated_title
        topic.participants.add(node.user_id)
        topic.updated_at = max(topic.updated_at, node.timestamp)
        if not topic.created_at:
            topic.created_at = node.timestamp
        if dag is not None:
            for existing in state.topics.values():
                TopicResolver.rebuild_profile(existing, dag)

    @staticmethod
    def rebuild_profile(topic: TopicState, dag: Any, exclude_id: str = "", as_of: float | None = None, window_seconds: float = 300.0, query_text: str | None = None) -> list[ConversationNode]:
        nodes = [dag.nodes[mid] for mid in topic.message_ids if mid in dag.nodes and mid != exclude_id]
        if as_of is not None:
            nodes = [n for n in nodes if 0 < as_of - n.timestamp <= window_seconds]
        nodes.sort(key=lambda n: (n.timestamp, n.msg_id))
        substantive = [n for n in nodes if not is_elliptical(n.text) and not _FILLER.fullmatch(n.text.strip())]
        vectors = [(n, hashed_embedding(n.text)) for n in substantive]
        space = "hashed"
        adapter = getattr(getattr(dag, "semantic_match_fn", None), "__self__", None)
        cached = getattr(adapter, "cached", None)
        if getattr(adapter, "enabled", False) and callable(cached) and substantive:
            neural = [(n, cached(n.text)) for n in substantive]
            query_vector = cached(query_text) if query_text is not None else neural[0][1]
            dimension = len(query_vector) if query_vector else 0
            # One adapter generation, complete coverage, and one dimension are
            # required. Never average hashed fallbacks into a neural centroid.
            if dimension and all(v and len(v) == dimension and all(math.isfinite(x) for x in v) for _, v in neural):
                vectors = neural
                space = f"neural:{getattr(adapter, 'provider_id', '')}:{getattr(adapter, '_generation', 0)}:{dimension}"
        centroid = [sum(v[i] for _, v in vectors) / len(vectors) for i in range(len(vectors[0][1]))] if vectors else []
        norm = math.sqrt(sum(v * v for v in centroid))
        topic.centroid_vector = [v / norm for v in centroid] if norm else None
        topic.centroid_space = space
        # Central representatives, rather than the last arbitrary short turns.
        central = sorted(((cosine(v, topic.centroid_vector or []), n) for n, v in vectors), key=lambda x: (-x[0], x[1].msg_id))
        topic.exemplar_messages = [(n.msg_id, round(s, 4)) for s, n in central[:5]]
        if central:
            topic.summary_excerpts = [n.text.strip()[:320] for _, n in central[:4]]
        topic.recent_message_ids = [n.msg_id for n in nodes[-2:]]
        topic.participants = {n.user_id for n in nodes}
        counts = Counter(token for n in substantive for token in lexical_tokens(n.text))
        topic.keywords = set(token for token, _ in counts.most_common(24))
        topic.interlocutor_affinity = {}
        for current in nodes:
            parent = dag.nodes.get(current.reply_to_id)
            routing = current.metadata.get("routing", {})
            targets = list(routing.get("addressee_ids", []))
            if parent is not None:
                targets.append(parent.user_id)
            for target in set(targets):
                if target != current.user_id:
                    key = (current.user_id, target)
                    topic.interlocutor_affinity[key] = topic.interlocutor_affinity.get(key, 0.0) + 1.0
        if nodes:
            topic.updated_at = max(n.timestamp for n in nodes)
        if substantive:
            topic.label = topic.generated_title or substantive[0].text.strip()[:80]
        return nodes

    def score_topic(self, node, dag, topic, matches) -> float:
        # Keep the public profile complete. A delayed turn scores against a
        # private historical view and must not rewind activity used by pruning.
        self.rebuild_profile(topic, dag)
        topic = copy(topic)
        nodes = self.rebuild_profile(topic, dag, exclude_id=node.msg_id, as_of=node.timestamp, window_seconds=self.window_seconds, query_text=node.text)
        nodes = [n for n in nodes if 0 < node.timestamp - n.timestamp <= self.window_seconds]
        if not nodes:
            return 0.0
        # All semantic terms use the original message. Context copied from a
        # candidate must never become semantic evidence for that same candidate.
        query_vector = hashed_embedding(node.text)
        if topic.centroid_space.startswith("neural:"):
            adapter = dag.semantic_match_fn.__self__
            query_vector = adapter.cached(node.text) or ()
        centroid = cosine(query_vector, topic.centroid_vector or [])
        allowed = {n.msg_id for n in nodes}
        exemplar = max((matches.get(mid, 0.0) for mid, _ in topic.exemplar_messages[:self.max_exemplars] if mid in allowed), default=0.0)
        recent = sum(matches.get(n.msg_id, 0.0) for n in nodes[-2:]) / len(nodes[-2:])
        lineage = float(bool(node.reply_to_id and node.reply_to_id in allowed))
        targets = set(node.mentioned_users)
        parent = dag.nodes.get(node.reply_to_id)
        if parent is not None:
            targets.add(parent.user_id)
        affinity = max((min(1.0, (topic.interlocutor_affinity.get((node.user_id, target), 0.0) + topic.interlocutor_affinity.get((target, node.user_id), 0.0)) / 2) for target in targets), default=0.0)
        # Short responses may continue a known exchange, without assuming that
        # every active group participant is addressing everyone else.
        if is_elliptical(node.text) and nodes[-1].user_id != node.user_id:
            affinity = max(affinity, min(1.0, topic.interlocutor_affinity.get((nodes[-1].user_id, node.user_id), 0.0)))
        participant = 0.25 * float(node.user_id in topic.participants) + 0.75 * affinity
        recency = max(0.0, 1 - (node.timestamp - nodes[-1].timestamp) / self.window_seconds)
        tokens = lexical_tokens(node.text)
        lexical = len(tokens & topic.keywords) / max(1, len(tokens))
        return round(0.30 * centroid + 0.20 * exemplar + 0.15 * recent + 0.15 * lineage + 0.10 * participant + 0.05 * recency + 0.05 * lexical, 4)

    def boundary_score(self, node, ranked, explicit_parent=None) -> float:
        """Evidence strength, not a calibrated probability. Markers need novelty."""
        if is_elliptical(node.text):
            return 0.0
        best = ranked[0][0] if ranked else 0.0
        marker = bool(_BOUNDARY.search(node.text.strip()))
        if marker and best < 0.55:
            return 0.85
        return 0.4 if best < 0.2 else 0.0

    def resolve(self, node, dag, state, matches, explicit_parent=None):
        ranked = sorted(((self.score_topic(node, dag, topic, matches), topic.topic_id) for topic in state.topics.values()), reverse=True)
        ranked = [(score, tid) for score, tid in ranked if score > 0]
        parent_topic = None
        if explicit_parent is not None:
            routing = explicit_parent.metadata.get("routing", {})
            if routing.get("topic_status") not in {"pending", "unknown"}:
                parent_topic = routing.get("topic_id") or explicit_parent.thread_id
        boundary = self.boundary_score(node, ranked, explicit_parent)
        if boundary >= 0.75:
            return node.msg_id, boundary, False, ["topic_boundary"], None, ranked
        if parent_topic and is_elliptical(node.text):
            return parent_topic, 0.95, False, ["topic_reply_continuation"], None, ranked
        if parent_topic:
            scores = dict((tid, score) for score, tid in ranked)
            scores[parent_topic] = min(0.95, scores.get(parent_topic, 0.0) + 0.45)
            ranked = sorted(((score, tid) for tid, score in scores.items()), reverse=True)
        if not ranked:
            return node.msg_id, 1.0, False, [], None, []
        best, tid = ranked[0]
        second_score, second = ranked[1] if len(ranked) > 1 else (0.0, None)
        evidence = ["topic_profile"]
        if tid == parent_topic:
            evidence.append("topic_reply_evidence")
        if best >= self.join_threshold and best - second_score >= self.margin_threshold:
            return tid, best, False, evidence, second, ranked
        if best >= self.ambiguity_threshold or is_elliptical(node.text):
            return tid, best, True, evidence + ["topic_ambiguous"], second, ranked
        return node.msg_id, 1.0, False, [], None, ranked
