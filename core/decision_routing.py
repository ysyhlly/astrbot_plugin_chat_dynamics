"""Closed recipient/topic opinions; callers own the session lock and epoch guard."""
from __future__ import annotations

from copy import deepcopy
import math

from .evidence import routing_ledger
from .routing_contract import commit_topic_evidence
from .topic_candidates import parse_candidates
from .topic_resolution import TopicResolver


def _protected(node, routing):
    return bool(node.reply_to_id or node.mentioned_users or node.metadata.get("is_wake")
                or routing.get("explicit_reply") or routing.get("explicit_mention")
                or set(routing.get("evidence", ())) & {
                    "explicit_reply", "explicit_mention", "platform_wake", "direct_name_call"})


def build_routing_tasks(turn):
    """Build only session-local closed candidates, with immutable input copies.

    Must be called synchronously before awaiting. The returned mapping is local
    bookkeeping, never serialized to the model or persisted as a training label.
    """
    node, runtime, dag = turn.node, turn.runtime, turn.dag
    routing = node.metadata.get("routing", {})
    recent = [n for n in dag.get_recent_nodes(40)
              if n.msg_id != node.msg_id and 0 <= node.timestamp - n.timestamp <= 300]
    participants = list(dict.fromkeys([str(runtime.bot_id)] if runtime.bot_id else []))
    participants.extend(uid for uid in dict.fromkeys(n.user_id for n in reversed(recent))
                        if uid and uid not in participants and uid != node.user_id)
    participants = participants[:8]
    questions, recipients, topics, descriptions = {}, {}, {}, {}
    if not _protected(node, routing):
        for i, uid in enumerate(participants):
            key = f"recipient.{i}"
            recipients[key] = uid
            questions[key] = {"type": "noul", "instructions":
                              f"Is participant {uid} an intended recipient of the current message? "
                              "Several recipients are possible. A quoted author or subject is not "
                              "necessarily addressed. Unknown recipient means false.",
                              "criteria": {"true": "Intended recipient", "false": "Not established as recipient"}}
    topic_protected = _protected(node, routing) or bool(set(routing.get("evidence", ())) & {
        "topic_boundary", "topic_reply_continuation", "topic_reply_evidence"})
    if not topic_protected:
        for tid in parse_candidates(routing.get("topic_candidates")).top(4):
            topic = runtime.routing_state.topics.get(tid)
            if topic is None:
                continue
            messages = [dag.nodes[mid] for mid in topic.message_ids if mid in dag.nodes
                        and mid != node.msg_id and 0 <= node.timestamp - dag.nodes[mid].timestamp <= 300][-3:]
            if not messages:
                continue
            key = f"topic_{len(topics)}"
            topics[key] = tid
            descriptions[key] = {"label": topic.label, "messages": [
                {"author": n.user_id, "text": n.text} for n in messages]}
        if topics:
            questions["topic"] = {"type": "choice", "instructions":
                                  "Which offered topic does the current message continue? "
                                  "Choose KEEP when unclear or none match; do not create a topic.",
                                  "criteria": {"KEEP": "Preserve existing unresolved or local assignment",
                                               **{key: f"Continue topic {key}" for key in topics}}}
    state = {"current_message": node.text, "author": node.user_id,
             "bot_id": runtime.bot_id, "recent_messages": [
                 {"author": n.user_id, "text": n.text} for n in recent[-16:]],
             "topic_candidates": descriptions}
    mapping = {"routing": deepcopy(routing), "node_text": node.text, "node": node,
               "runtime": runtime, "dag": dag, "routing_state": runtime.routing_state,
               "recipients": recipients, "topics": topics}
    return state, questions, mapping


def _probability(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def apply_routing_answers(turn, answers, mapping):
    """Apply accepted typed opinions under lock, after caller's current-turn check.

    No DAG edge or platform fact is manufactured. Routing changes since the
    snapshot (including embedding/reranker enrichment) invalidate the whole batch.
    """
    node, runtime, dag = turn.node, turn.runtime, turn.dag
    routing = node.metadata.get("routing", {})
    if (not isinstance(answers, dict) or mapping.get("node") is not node
            or mapping.get("runtime") is not runtime or mapping.get("dag") is not dag
            or runtime.dag is not dag or dag.get_node(node.msg_id) is not node
            or mapping.get("routing_state") is not runtime.routing_state
            or mapping.get("node_text") != node.text or mapping.get("routing") != routing):
        return False
    changed = False
    recipients = mapping["recipients"]
    if recipients and not _protected(node, routing):
        rows = [answers.get(key, {}) for key in recipients]
        # Partial acceptance must not erase candidates that lacked an answer.
        if all(isinstance(row, dict) and row.get("type") == "noul"
               and _probability(row.get("noul")) and abs(row["noul"] - .5) >= .3 for row in rows):
            selected = [uid for (key, uid), row in zip(recipients.items(), rows) if row["noul"] >= .5]
            confidence = min(max(row["noul"], 1 - row["noul"]) for row in rows)
            routing.update(addressee_ids=selected, addressee_confidence=confidence if selected else 0.,
                           addressee_ambiguous=not bool(selected),
                           bot_is_addressee=runtime.bot_id in selected,
                           bot_addressee_confidence=confidence if runtime.bot_id in selected else 0.)
            routing["semantic_recipient_decision"] = True
            changed = True
    answer = answers.get("topic", {})
    tid = mapping["topics"].get(answer.get("choice")) if isinstance(answer, dict) else None
    if (tid and not _protected(node, routing) and answer.get("type") == "choice" and _probability(answer.get("confidence"))
            and answer["confidence"] >= .8 and tid in runtime.routing_state.topics):
        state = runtime.routing_state
        TopicResolver.remember(state, node, tid, dag=dag)
        state.pending_assignments.pop(node.msg_id, None)
        routing.update(topic_id=tid, topic_status="committed", topic_ambiguous=False,
                       topic_confidence=answer["confidence"])
        routing["evidence"] = commit_topic_evidence(routing, "topic_llm_rerank")
        routing["semantic_topic_decision"] = True
        node.metadata["topic_id"] = tid
        state.last_topic_id = tid
        changed = True
    if changed:
        routing["ambiguous"] = bool(routing.get("topic_ambiguous") or routing.get("addressee_ambiguous"))
        routing["ledger"] = routing_ledger(routing)
    return changed
