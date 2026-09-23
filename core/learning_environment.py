"""Point-in-time, non-semantic runtime features for decision learning.

These raw values are the E input for a future environment encoder. They are
kept separate from the conversation text and never contain user or message IDs.
"""

from __future__ import annotations

from typing import Any


ENVIRONMENT_SCHEMA_VERSION = 1
ACTIVITY_WINDOW_SECONDS = 300.0


def capture_environment(runtime: Any, turn: Any, arbiter: Any, *, now: float) -> dict:
    """Capture observable features before the teacher or student is awaited."""
    bot_id = str(getattr(runtime, "bot_id", "") or "")
    session_key = str(getattr(runtime, "session_key", "") or "")
    messages = tuple(getattr(turn, "messages", ()) or ())
    current_ids = {str(getattr(message, "message_id", "") or "") for message in messages}
    dag = getattr(runtime, "dag", None)
    # Scan the retained DAG, not just a short tail: a busy room can have more
    # than 200 messages inside five minutes.
    recent = dag.get_recent_nodes(limit=0) if dag is not None else []
    prior = [node for node in recent if getattr(node, "msg_id", None) not in current_ids
             and float(getattr(node, "timestamp", 0) or 0) <= now]
    window = [node for node in recent if 0 <= now - float(getattr(node, "timestamp", 0) or 0)
              <= ACTIVITY_WINDOW_SECONDS]
    oldest = float(getattr(recent[0], "timestamp", 0) or 0) if recent else 0.0
    capacity = int(getattr(dag, "max_nodes", 0) or 0)
    window_complete = bool(dag is not None and
                           (not capacity or len(recent) < capacity or now - oldest > ACTIVITY_WINDOW_SECONDS))
    active_users = {str(node.user_id) for node in window
                    if getattr(node, "user_id", None) and str(node.user_id) != bot_id}

    reply_evidence = []
    unknown_reply = False
    getter = getattr(dag, "get_node", None)
    for message in messages:
        semantics = getattr(message, "semantics", None)
        target = str(getattr(message, "reply_to", "") or
                     getattr(semantics, "quoted_message_id", "") or "")
        if not target:
            continue
        parent = getter(target) if callable(getter) else None
        quoted_author = str(getattr(parent, "user_id", "") or
                            getattr(semantics, "quoted_author_id", "") or "")
        if quoted_author and bot_id:
            reply_evidence.append(quoted_author == bot_id)
        else:
            unknown_reply = True
    reply_to_self = (True if any(reply_evidence) else None if unknown_reply or not bot_id
                     else False)

    last_bot = getattr(runtime, "last_bot_node", None)
    if (last_bot is None or float(getattr(last_bot, "timestamp", 0) or 0) > now) and bot_id:
        last_bot = next((node for node in reversed(prior)
                         if str(getattr(node, "user_id", "") or "") == bot_id), None)
    last_bot_time = float(getattr(last_bot, "timestamp", 0) or 0) if last_bot else 0.0
    since_bot = round(max(0.0, now - last_bot_time), 1) if last_bot_time > 0 else None

    bot_streak = 0
    if bot_id:
        for node in reversed(prior):
            if str(getattr(node, "user_id", "") or "") != bot_id:
                break
            bot_streak += 1

    cooling_reader = getattr(arbiter, "cooling_remaining", None)
    cooling = (cooling_reader(session_key, current_time=now)
               if callable(cooling_reader) else None)
    return {
        "schema_version": ENVIRONMENT_SCHEMA_VERSION,
        "mentioned_self": (any(bot_id in tuple(getattr(message, "mentioned_users", ()) or ())
                               for message in messages) if bot_id else None),
        "reply_to_self": reply_to_self,
        "seconds_since_last_bot_message": since_bot,
        "consecutive_bot_messages_before_decision": bot_streak if bot_id else None,
        "bot_messages_last_5m": (sum(str(getattr(node, "user_id", "") or "") == bot_id
                                     for node in window) if bot_id and window_complete else None),
        "active_users_last_5m": len(active_users) if bot_id and window_complete else None,
        "room_messages_last_5m": len(window) if window_complete else None,
        "cooldown_remaining_seconds": round(max(0.0, float(cooling)), 1) if cooling is not None else None,
    }
