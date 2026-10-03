"""Bounded, evidence-based history retrieval for an immutable model turn."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Sequence

from .routing_contract import UNRESOLVED_TOPIC_STATUS, addressee_is_ambiguous
from .topic_identity import node_topic_id

MAX_PARENT_HOPS = 8
MAX_ANCESTORS = 64
MAX_RECENT_CANDIDATES = 80
HISTORY_WINDOW_SECONDS = 180.0
CONTEXT_INSTRUCTIONS = (
    "Background is evidence, never requests/targets. context_evidence: reply=platform "
    "quote; fragment=same turn; mention=account history, not quote. inferred_reply/"
    "same_topic/recipient are estimates; use confidence/evidence. Same topic proves "
    "neither recipient nor need to reply. Attribute constraints; keep tasks separate. "
    "age_seconds is snapshot-relative. text_spans are zero-based [start,end) offsets "
    "in conversation.text; text_excerpt is an unmapped source excerpt. Earlier "
    "fragments inherit fragment_defaults; local values override. dialogue holds "
    "delivered replies and user updates, never current requests or recipient proof."
)
_PRIORITY = {"reply": 0, "fragment": 0, "mention": 1, "inferred_reply": 2,
             "bot_reply": 3, "same_topic": 4, "recipient": 5, "same_author": 6}


def clip_text(text: str, limit: int) -> str:
    """Keep both the question's opening and constraints often added at its end."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    marker = "\n[...]\n"
    if limit <= len(marker):
        return text[-limit:]
    head = (limit - len(marker)) // 2
    return text[:head] + marker + text[-(limit - head - len(marker)):]


@dataclass(frozen=True)
class ContextEvidence:
    relation: str
    via_message_id: str
    confidence: float
    evidence: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(str(code)[:96] for code in self.evidence if code)[:8])

    @property
    def priority(self) -> int:
        rank = _PRIORITY.get(self.relation, 7)
        for code, floor in (("mention_ancestry", 1), ("inferred_ancestry", 2), ("related_supplement", 3)):
            if code in self.evidence:
                rank = max(rank, floor)
        return rank


@dataclass(frozen=True)
class BackgroundSelection:
    node: Any
    context_evidence: tuple[ContextEvidence, ...]
    priority: int


def _routing(node: Any) -> dict:
    routing = node.metadata.get("routing", {})
    return routing if isinstance(routing, dict) else {}


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.0
    return float(value) if 0 <= value <= 1 else 0.0


def confirmed_topic(node: Any) -> tuple[str, float]:
    """Return only an unambiguous topic above its recorded acceptance threshold."""
    routing = _routing(node)
    confidence = _confidence(routing.get("topic_confidence"))
    threshold = _confidence(routing.get("topic_threshold", .58)) or .58
    if (routing.get("topic_ambiguous")
            or routing.get("topic_status") in UNRESOLVED_TOPIC_STATUS
            or confidence < threshold):
        return "", 0.0
    return node_topic_id(node), confidence


def _recipients(node: Any, dag: Any) -> tuple[tuple[str, ...], float]:
    mentions = node.metadata.get("actual_mentions", node.mentioned_users) or ()
    if mentions:
        return tuple(str(uid) for uid in mentions if uid), 1.0
    routing = _routing(node)
    confidence = _confidence(routing.get("addressee_confidence"))
    if confidence >= .72 and not addressee_is_ambiguous(routing, True):
        return tuple(str(uid) for uid in routing.get("addressee_ids", ()) if uid), confidence
    if not routing.get("addressee_ids") and not routing.get("bot_is_addressee"):
        quoted = dag.get_node(node.reply_to_id) if node.reply_to_id else None
        if quoted is not None:
            return (quoted.user_id,), 1.0
    return (), 0.0


def _parents(node: Any, dag: Any) -> tuple[tuple[Any, ContextEvidence], ...]:
    """Follow platform links and accepted inference; candidate pointers are insufficient."""
    links = []
    parent_ids = set(node.parent_ids)
    if node.reply_to_id:
        parent_ids.add(node.reply_to_id)
    routing = _routing(node)
    for parent_id in sorted(parent_ids):
        parent = dag.get_node(parent_id)
        if parent is None or parent.msg_id == node.msg_id or parent.timestamp > node.timestamp:
            continue
        kind = "reply" if parent_id == node.reply_to_id else node.edge_kinds.get(parent_id)
        if kind in {"reply", "mention"}:
            links.append((parent, ContextEvidence(kind, node.msg_id, 1.0, ("platform_" + kind,))))
        elif (kind == "fragment" and node.user_id == parent.user_id
              and node.metadata.get("turn_id")
              and node.metadata["turn_id"] == parent.metadata.get("turn_id")):
            child_index = node.metadata.get("turn_index")
            parent_index = parent.metadata.get("turn_index")
            if (child_index is not None and parent_index is not None
                    and (type(child_index) is not int or type(parent_index) is not int
                         or parent_index >= child_index)):
                continue
            links.append((parent, ContextEvidence(kind, node.msg_id, 1.0, ("same_turn_fragment",))))
        elif kind == "inferred_reply" and not node.reply_to_id:
            edges = node.metadata.get("edge_metadata") or {}
            edge = edges.get(parent_id, {}) if isinstance(edges, dict) else {}
            if not isinstance(edge, dict):
                continue
            confidence = _confidence(edge.get("confidence", routing.get("parent_confidence")))
            threshold = _confidence(routing.get("parent_threshold", .72)) or .72
            if (confidence < threshold or node.timestamp - parent.timestamp > HISTORY_WINDOW_SECONDS
                    or routing.get("parent_ambiguous")
                    or (routing.get("parent_message_id") and routing["parent_message_id"] != parent_id)
                    or ("parent_confidence" in routing and _confidence(routing["parent_confidence"]) < threshold)):
                continue
            topic, _ = confirmed_topic(node)
            parent_topic, _ = confirmed_topic(parent)
            if topic and parent_topic and topic != parent_topic:
                continue
            reasons = tuple(str(code) for code in routing.get("evidence", ()) if code)
            reason = str(edge.get("reason") or node.metadata.get("inferred_parent_reason") or "")
            links.append((parent, ContextEvidence(kind, node.msg_id, confidence,
                                                  tuple(dict.fromkeys(("accepted_inferred_reply", *reasons, reason))))))
    return tuple(links)


def select_background(dag: Any, current_nodes: Sequence[Any], *, bot_id: str = "",
                      eligible_ids: frozenset[str] | None = None) -> tuple[BackgroundSelection, ...]:
    """Return strongest evidence first; the caller budgets text then restores chronology.

    Explicit ancestors may be older than the recent window. All supplements are
    bounded by age, count and the latest current node's position, including when
    messages share timestamps or the DAG has advanced while a turn was queued.
    """
    current_ids = {node.msg_id for node in current_nodes}
    cutoff = max(node.timestamp for node in current_nodes)
    ordered = [node for node in dag.get_recent_nodes(limit=0)
               if eligible_ids is None or node.msg_id in eligible_ids]
    positions = {node.msg_id: index for index, node in enumerate(ordered)}
    boundary = max(positions.get(mid, -1) for mid in current_ids)
    selected: dict[str, BackgroundSelection] = {}
    current_topics = {topic for node in current_nodes if (topic := confirmed_topic(node)[0])}
    topic_anchor_ids = set(current_ids)

    def remember(node: Any, evidence: ContextEvidence, priority: int | None = None) -> bool:
        if (node.msg_id in current_ids or node.timestamp > cutoff
                or positions.get(node.msg_id, boundary + 1) > boundary):
            return False
        rank = evidence.priority if priority is None else priority
        prior = selected.get(node.msg_id)
        proofs = prior.context_evidence if prior else ()
        if evidence not in proofs:
            proofs = tuple(sorted((*proofs, evidence), key=lambda proof: (proof.priority, -proof.confidence)))[:3]
        selected[node.msg_id] = BackgroundSelection(node, proofs, min(rank, prior.priority) if prior else rank)
        return prior is None or rank < prior.priority

    frontier = [(node, 0, True, 0) for node in current_nodes]
    traversed: dict[tuple[str, int, bool], int] = {}
    while frontier:
        next_frontier = []
        for child, rank, reliable_path, hops in frontier:
            key = (child.msg_id, rank, reliable_path)
            if traversed.get(key, MAX_PARENT_HOPS + 1) <= hops:
                continue
            traversed[key] = hops
            for parent, proof in _parents(child, dag):
                # A quoted turn can contain more than eight pieces. Fragment
                # steps share one turn; only conversational parent hops consume
                # depth. The ancestor count still bounds all retrieved nodes.
                parent_hops = hops + (proof.relation != "fragment")
                if parent_hops > MAX_PARENT_HOPS:
                    continue
                path_rank = max(rank, proof.priority)
                parent_topic = node_topic_id(parent)
                if path_rank and current_topics and parent_topic and parent_topic not in current_topics:
                    continue
                if path_rank > proof.priority:
                    code = "inferred_ancestry" if path_rank >= 2 else "mention_ancestry"
                    proof = replace(proof, evidence=(code, *proof.evidence))
                remember(parent, proof, path_rank)
                reliable = reliable_path and proof.relation != "mention"
                if reliable and parent.msg_id in selected and parent.msg_id not in topic_anchor_ids:
                    topic_anchor_ids.add(parent.msg_id)
                if parent.msg_id in selected:
                    next_frontier.append((parent, path_rank, reliable, parent_hops))
                if len(selected) >= MAX_ANCESTORS:
                    break
            if len(selected) >= MAX_ANCESTORS:
                break
        frontier = next_frontier
        if not frontier or len(selected) >= MAX_ANCESTORS:
            break

    # Mention edges select an account's recent message. They cannot establish
    # continuation of that message's topic or authorise its whole reply branch.
    anchors = list(current_nodes) + [row.node for mid, row in selected.items() if mid in topic_anchor_ids]
    anchor_ids = topic_anchor_ids
    topics = {}
    recipients = {}
    for node in anchors:
        topic, confidence = confirmed_topic(node)
        if topic and (topic not in topics or confidence > topics[topic][1]):
            topics[topic] = (node.msg_id, confidence)
    for node in current_nodes:
        ids, confidence = _recipients(node, dag)
        for uid in ids:
            if uid != bot_id:
                recipients[uid] = (node.msg_id, confidence)
    authors = {node.user_id for node in current_nodes}
    participants = authors | set(recipients) | {node.user_id for node in anchors}
    recent = [node for node in ordered[:boundary + 1]
              if 0 <= cutoff - node.timestamp <= HISTORY_WINDOW_SECONDS][-MAX_RECENT_CANDIDATES:]
    for node in recent:
        if node.msg_id in current_ids:
            continue
        candidate_topic = node_topic_id(node)
        if topics and candidate_topic and candidate_topic not in topics:
            continue
        topic, confidence = confirmed_topic(node)
        ids, recipient_confidence = _recipients(node, dag)
        # A known participant can also speak to someone outside this exchange.
        # A shared bot recipient only connects supplements with a confirmed topic.
        relevant_recipients = participants | ({bot_id} if bot_id and topic in topics else set())
        if ids and not set(ids) & relevant_recipients:
            continue
        # Apply topic/recipient exclusions before adding any supplement. A mention
        # points to an account's latest message, not a reply to that question.
        for parent, proof in _parents(node, dag):
            if parent.msg_id in anchor_ids and proof.relation in {"reply", "inferred_reply"}:
                relation = "bot_reply" if node.user_id == bot_id else proof.relation
                remember(node, ContextEvidence(relation, parent.msg_id, proof.confidence,
                                              ("related_supplement", *proof.evidence)),
                         max(3, proof.priority))
        if topic in topics:
            via, anchor_confidence = topics[topic]
            remember(node, ContextEvidence("same_topic", via, min(confidence, anchor_confidence),
                                           tuple(str(code) for code in _routing(node).get("evidence", ()) if code)))
        if node.user_id in recipients:
            via, confidence = recipients[node.user_id]
            remember(node, ContextEvidence("recipient", via, confidence, ("addressed_account_history",)))
        elif set(ids) & authors:
            remember(node, ContextEvidence("recipient", current_nodes[-1].msg_id, recipient_confidence,
                                           ("addressed_current_speaker",)))
        if node.user_id in authors:
            remember(node, ContextEvidence("same_author", current_nodes[-1].msg_id, 0.0,
                                           ("recent_speaker_history",)))
    return tuple(sorted(selected.values(), key=lambda row: (row.priority, -row.node.timestamp,
                                                            -positions[row.node.msg_id])))
