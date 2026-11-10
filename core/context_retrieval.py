"""Bounded, evidence-based history retrieval for an immutable model turn."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Sequence

from .routing_contract import UNRESOLVED_TOPIC_STATUS, addressee_is_ambiguous
from .topic_identity import node_topic_id

MAX_PARENT_HOPS = 8
MAX_ANCESTORS = 64
MAX_RECENT_CANDIDATES = 80
HISTORY_WINDOW_SECONDS = 180.0
TOPIC_HISTORY_WINDOW_SECONDS = 1800.0
MAX_TOPIC_CANDIDATES = 400
CONTEXT_INSTRUCTIONS = (
    "Background: evidence, not requests/targets. reply=platform quote; "
    "fragment=same turn; mention=account history, not quote. inferred_reply/same_topic/"
    "recipient are estimates, not proof of addressee or reply obligation. "
    "Keep authors/tasks separate. age_seconds is snapshot-relative; text_spans=[start,end) "
    "in conversation.text; text_excerpt=unmapped excerpt. fragment_defaults inherit unless "
    "overridden. dialogue=delivered replies/user updates. group_events=platform facts, "
    "not speech; member_joined identifies newcomer, not greeter. topic_origin=earliest "
    "retained topic evidence, not guaranteed beginning."
)
_PRIORITY = {
    "reply": 0,
    "fragment": 0,
    "mention": 1,
    "inferred_reply": 2,
    "poke_sender": 1,
    "supplemental_context": 1,
    "poke_reply": 2,
    "bot_reply": 3,
    "topic_origin": 3,
    "same_topic": 4,
    "recipient": 5,
    "same_author": 6,
}
_PEER_GESTURE = re.compile(
    r"(?:我\s*)?(?:(?:给|帮)\s*你\s*(?:揉揉|按按|按摩|抱抱|拍拍|摸摸|捏捏|亲亲)|"
    r"你\s*(?:揉揉|按按|按摩|抱抱|拍拍|摸摸|捏捏|亲亲)|"
    r"(?:揉揉|按按|按摩|抱抱|拍拍|摸摸|捏捏|亲亲)你)"
    r"(?:呀|啊|呢|嘛|啦|哦|喔|～|~)?[，,。.!！?？]*"
)


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


@dataclass(frozen=True)
class PeerContinuationCandidate:
    source_message_id: str
    source_author: str
    candidate_recipient_id: str
    age_seconds: float
    basis: str
    source_excerpt: str

    @property
    def strong(self) -> bool:
        return self.basis in {"actual_mention", "actual_mention_and_quote"}

    def payload(self) -> dict:
        return {
            "source_message_id": self.source_message_id,
            "source_author": self.source_author,
            "candidate_recipient_id": self.candidate_recipient_id,
            "age_seconds": round(self.age_seconds, 3),
            "basis": self.basis,
            "source_excerpt": self.source_excerpt,
            "interpretation": "Prior peer target only; not proof this turn still addresses them. Reassess new topics.",
        }


def is_short_peer_gesture(text: str) -> bool:
    value = str(text or "").strip()
    return bool(value and len(value) <= 24 and _PEER_GESTURE.fullmatch(value))


def select_peer_continuation_candidate(
    dag: Any,
    current_nodes: Sequence[Any],
    *,
    bot_id: str = "",
    eligible_ids: frozenset[str] | None = None,
    cutoff: float | None = None,
    window: float,
    explicit: bool = False,
    wake_kind: str = "legacy",
    dialogue: Any = None,
) -> PeerContinuationCandidate | None:
    """Select the nearest visible peer-target source for a current turn.

    The source is the latest earlier message by the same author within the
    supplied watermark and time window. The result is historical recipient
    evidence, not proof that the current turn contains an @ or reply.

    Args:
        dag: Conversation history for the current session.
        current_nodes: Ordered DAG nodes belonging to the current turn.
        bot_id: Current bot account ID, excluded as a peer recipient.
        eligible_ids: Visible message IDs at the immutable turn watermark.
        cutoff: Monotonic timestamp of the current turn snapshot.
        window: Maximum age of the earlier source, in seconds.
        explicit: Whether this turn is marked explicit by the pipeline.
        wake_kind: Current wake classification, including inherited supplements.
        dialogue: Current bot dialogue snapshot used to reject superseded targets.

    Returns:
        A frozen prior-peer candidate, or None when evidence is insufficient.
    """
    if (not current_nodes or wake_kind not in {"legacy", "ambient", "none", "supplement"}
            or (explicit and wake_kind != "supplement")):
        return None
    current_ids = {str(node.msg_id) for node in current_nodes}
    if any(node.reply_to_id or node.metadata.get("actual_mentions") for node in current_nodes):
        return None
    current_author = str(current_nodes[-1].user_id or "")
    if not current_author:
        return None
    current_topics = {confirmed_topic(node)[0] for node in current_nodes}
    current_topics.discard("")
    if len(current_topics) > 1:
        return None
    ordered = [node for node in dag.get_recent_nodes(limit=0)
               if eligible_ids is None or node.msg_id in eligible_ids]
    positions = {node.msg_id: index for index, node in enumerate(ordered)}
    if any(message_id not in positions for message_id in current_ids):
        return None
    current_boundary = min(positions[message_id] for message_id in current_ids)
    current_cutoff = float(cutoff if cutoff is not None else max(node.timestamp for node in current_nodes))
    previous = next((node for node in reversed(ordered[:current_boundary])
                     if str(node.user_id or "") == current_author and node.timestamp <= current_cutoff), None)
    if previous is None:
        return None
    age = current_cutoff - float(previous.timestamp)
    if age < 0 or age > float(window):
        return None
    source_meta = previous.metadata if isinstance(previous.metadata, dict) else {}
    if source_meta.get("hidden") or source_meta.get("visibility") == "hidden":
        return None
    source_topic, _ = confirmed_topic(previous)
    current_topic = next(iter(current_topics), "")
    if source_topic and current_topic and source_topic != current_topic:
        return None
    for node in ordered[positions[previous.msg_id] + 1:current_boundary]:
        topic, _ = confirmed_topic(node)
        if topic and topic not in {source_topic, current_topic}:
            return None
    mentions = source_meta.get("actual_mentions")
    if not isinstance(mentions, (list, tuple)):
        mentions = ()
    mention_ids = tuple(dict.fromkeys(str(uid) for uid in mentions if uid and str(uid) not in {"0", "None"}))
    quote_author = str(source_meta.get("quoted_author_id") or "")
    if not quote_author and previous.reply_to_id:
        quoted = dag.get_node(previous.reply_to_id)
        quote_author = str(getattr(quoted, "user_id", "") or "")
    quote_author = "" if quote_author in {"0", "None"} else quote_author
    if len(mention_ids) > 1 or (quote_author and mention_ids and quote_author != mention_ids[0]):
        return None
    if mention_ids:
        recipient = mention_ids[0]
        if not recipient or recipient == str(bot_id or ""):
            return None
        basis = "actual_mention_and_quote" if quote_author else "actual_mention"
    elif quote_author and quote_author != str(bot_id or ""):
        recipient = quote_author
        basis = "quote_only"
    else:
        return None
    routing = _routing(current_nodes[-1])
    if routing.get("bot_is_addressee") or routing.get("addressee_ids"):
        return None
    intervening_author_ids = {previous.msg_id}
    intervening_author_ids.update(node.msg_id for node in ordered[positions[previous.msg_id] + 1:current_boundary]
                                  if str(node.user_id or "") == current_author)
    if any(str(node.user_id or "") == str(bot_id or "")
           and (node.reply_to_id in intervening_author_ids
                or (current_author in _routing(node).get("addressee_ids", ())
                    and _confidence(_routing(node).get("addressee_confidence")) >= .72))
           for node in ordered[positions[previous.msg_id] + 1:current_boundary]):
        return None
    if dialogue is not None and not getattr(dialogue, "closed", True):
        if (str(getattr(dialogue, "user_id", "") or "") == current_author
                and str(getattr(dialogue, "last_bot_message_id", "") or "")
                and float(getattr(dialogue, "updated_at", 0) or 0) > float(previous.timestamp)):
            return None
    return PeerContinuationCandidate(
        source_message_id=str(previous.msg_id),
        source_author=current_author,
        candidate_recipient_id=recipient,
        age_seconds=age,
        basis=basis,
        source_excerpt=clip_text(str(previous.text or ""), 160),
    )


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


def select_background(
    dag: Any,
    current_nodes: Sequence[Any],
    *,
    bot_id: str = "",
    eligible_ids: frozenset[str] | None = None,
    delivered_ids: frozenset[str] = frozenset(),
) -> tuple[BackgroundSelection, ...]:
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
    # A platform poke has no lexical topic. Preserve its actual sender's
    # same-session history instead of following an unrelated inferred parent.
    if (
        current_nodes
        and len({str(n.user_id) for n in current_nodes}) == 1
        and all(
            n.metadata.get("poke_at_bot") is True and str(n.text).strip() in {"[戳一戳] 有人戳了你", "[戳一戳]"}
            for n in current_nodes
        )
    ):
        owner = str(current_nodes[-1].user_id)
        if not owner or owner == bot_id:
            return ()
        recent = [
            n
            for n in ordered[: boundary + 1]
            if n.msg_id not in current_ids
            and 0 <= cutoff - n.timestamp <= TOPIC_HISTORY_WINDOW_SECONDS
            and not str(n.text).strip().startswith("[戳一戳]")
        ]
        source = [n for n in recent if str(n.user_id) == owner][-6:]
        replies = [
            n
            for n in recent
            if str(n.user_id) == bot_id
            and str(n.metadata.get("trigger_user_id", "")) == owner
            and (n.metadata.get("dialogue_delivered") is True or n.msg_id in delivered_ids)
        ][-6:]
        return tuple(
            BackgroundSelection(
                n,
                (
                    ContextEvidence(
                        "poke_sender" if str(n.user_id) == owner else "poke_reply",
                        current_nodes[-1].msg_id,
                        1.0,
                        ("same_session_poke_sender" if str(n.user_id) == owner else "delivered_reply_to_poke_sender",),
                    ),
                ),
                1 if str(n.user_id) == owner else 2,
            )
            for n in (*reversed(source), *reversed(replies))
        )
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
    current_addresses_bot = bool(bot_id) and any(
        bot_id in _recipients(node, dag)[0] for node in current_nodes)
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
        # A quick correction to our own pending request may arrive before the
        # topic batch has assigned either message. Keep the author's previous
        # bot-directed input as weak background, never as a topic/reply edge.
        if current_addresses_bot and node.user_id in authors and 0 <= cutoff - node.timestamp <= 30:
            relevant_recipients.add(bot_id)
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
    # A confirmed topic may continue after the short local speaker window.
    # Do not broaden same-author/recipient guesses or pull later queued messages.
    topic_rows = {}
    for node in ordered[max(0, boundary + 1 - MAX_TOPIC_CANDIDATES):boundary + 1]:
        if node.msg_id in current_ids or not 0 <= cutoff - node.timestamp <= TOPIC_HISTORY_WINDOW_SECONDS:
            continue
        topic, confidence = confirmed_topic(node)
        if topic not in topics:
            continue
        ids, _ = _recipients(node, dag)
        if ids and not set(ids) & (participants | {bot_id}):
            continue
        via, anchor_confidence = topics[topic]
        remember(node, ContextEvidence("same_topic", via, min(confidence, anchor_confidence),
                                      ("confirmed_topic_history",)))
        topic_rows.setdefault(topic, []).append(node)
    for topic, rows in topic_rows.items():
        if rows and cutoff - rows[0].timestamp > HISTORY_WINDOW_SECONDS:
            via, confidence = topics[topic]
            remember(rows[0], ContextEvidence("topic_origin", via,
                                              min(confirmed_topic(rows[0])[1], confidence),
                                              ("earliest_retained_topic_context",)))
    return tuple(sorted(selected.values(), key=lambda row: (row.priority, -row.node.timestamp,
                                                            -positions[row.node.msg_id])))
