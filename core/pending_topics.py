"""Bounded deferred topic assignments with explicit follow-up evidence."""
from .session_runtime import PendingAssignment


def defer(state, node, result, ranked):
    for topic_id, topic in list(state.topics.items()):
        if node.msg_id in topic.message_ids:
            topic.message_ids.remove(node.msg_id)
            if not topic.message_ids:
                state.topics.pop(topic_id, None)
                state.archive.pop(topic_id)
    pending = state.pending_assignments.get(node.msg_id)
    if pending is None:
        pending = PendingAssignment(node.msg_id, created_at=node.timestamp)
        state.pending_assignments[node.msg_id] = pending
    pending.candidates = list(ranked[:3])
    result.topic_id = ""
    result.topic_status = "pending"
    result.topic_ambiguous = True
    while len(state.pending_assignments) > 80:
        state.pending_assignments.pop(next(iter(state.pending_assignments)))


def reconcile(state, dag, current, result, remember):
    """Only a direct follow-up with independently resolved topic can backfill."""
    for mid, pending in list(state.pending_assignments.items()):
        if mid == current.msg_id or current.timestamp <= pending.created_at:
            continue
        prior = dag.get_node(mid)
        if prior is None:
            state.pending_assignments.pop(mid, None)
            continue
        pending.observed_ids.add(current.msg_id)
        eligible = {tid for _, tid in pending.candidates}
        if (current.reply_to_id == mid and result.topic_id in eligible
                and not result.topic_ambiguous and current.timestamp - pending.created_at <= 30):
            remember(state, prior, result.topic_id, dag=dag)
            old = dict(prior.metadata.get("routing", {}))
            old.update(topic_id=result.topic_id, topic_confidence=result.topic_confidence,
                       topic_ambiguous=False, topic_status="committed")
            old["evidence"] = list(old.get("evidence", [])) + ["pending_followup"]
            prior.metadata["routing"] = old
            prior.metadata["topic_id"] = result.topic_id
            state.pending_assignments.pop(mid, None)
        elif len(pending.observed_ids) >= 3 or current.timestamp - pending.created_at > 30:
            prior.metadata.get("routing", {})["topic_status"] = "unknown"
            state.pending_assignments.pop(mid, None)
