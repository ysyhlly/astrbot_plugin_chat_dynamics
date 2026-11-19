"""Short attributed evidence shared by this turn's tone estimate and reply."""

import hashlib
import json

from .context_retrieval import clip_text
from .nearby_context import filter_nearby


def _quoted_id(message):
    return message.reply_to or getattr(message.semantics, "quoted_message_id", "")


def tone_context(turn, host, event):
    """Prefer real quotes and a delivered exchange over mere chronological proximity."""
    current = {m.message_id for m in turn.messages}
    available = {
        m.message_id: m for m in (*turn.nearby_context, *turn.background)
        if m.message_id and m.message_id not in current and m.author and m.text
        and (turn.snapshot_at is None or m.timestamp is None or m.timestamp <= turn.snapshot_at)
    }
    if not available:
        return []
    # Both inputs are frozen by the caller. Recheck their authors (including
    # quoted authors) and use the text returned by the boundary service.
    allowed = filter_nearby(tuple(available.values()), host, event, include_mentions=True)
    available = {m.message_id: m for m in allowed if m.text.strip()}
    ordered = sorted(allowed, key=lambda m: m.timestamp if m.timestamp is not None else float("-inf"))
    direct = [mid for m in reversed(turn.messages) if (mid := _quoted_id(m)) in available]
    chain = [_quoted_id(available[mid]) for mid in direct]
    dialogue = turn.dialogue
    previous = [dialogue.last_bot_message_id, dialogue.last_user_message_id] if dialogue else []
    # A report from this exchange can arrive between the quoted action and the
    # evaluation. A newer line from bystanders should not evict that report.
    related = [m.message_id for m in reversed(ordered)
               if m.author == turn.author or turn.author in m.mentioned_users or _quoted_id(m) in direct]
    priority = dict.fromkeys([*direct, *chain, *related[:1], *previous, *related,
                              *(m.message_id for m in reversed(ordered))])
    selected = [mid for mid in priority if mid in available][:3]
    rows = []
    for message in ordered:
        mid = message.message_id
        if mid not in selected:
            continue
        basis = "context_only"
        if mid in direct:
            basis = "quoted"
        elif mid in chain:
            basis = "quote_chain"
        elif mid in previous:
            basis = "dialogue"
        row = {"message_id": mid, "author": message.author, "text": message.text, "basis": basis}
        if message.author == turn.bot_id:
            row["author_is_bot"] = True
        if message.author_name:
            row["author_name"] = message.author_name[:64]
        if reply_to := _quoted_id(message):
            row["reply_to"] = reply_to
        if quoted := getattr(message.semantics, "quoted_author_id", ""):
            row["quoted_author_id"] = quoted
        if message.mentioned_users:
            row["mentioned_user_ids"] = list(message.mentioned_users[:4])
            row["mentions_bot"] = turn.bot_id in message.mentioned_users
        rows.append(row)
    return rows


def exchange(conversation, rows):
    current = (conversation.get("messages") or [{}])[-1]
    semantics = current.get("semantics") or {}
    text = conversation.get("text", "")
    return {
        "bot_id": (conversation.get("bot_identity") or {}).get("user_id", ""),
        "preceding": list(rows),
        "current": {"speaker_id": conversation.get("author", ""), "message_id": current.get("message_id", ""),
                    "quotes": current.get("reply_to") or semantics.get("quoted_message_id", ""),
                    "quoted_author_id": semantics.get("quoted_author_id", ""),
                    "mentions": semantics.get("mentioned_user_ids", ()), "words": clip_text(text, 240),
                    "excerpt": len(text) > 240},
    }


def source_signatures(rows):
    return {
        row["message_id"]: hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for row in rows
    }


def for_reply(turn, frame, host, event):
    """Revalidate the exact cited rows. A changed source invalidates its estimate."""
    if not isinstance(frame, dict) or not turn.messages or (
        frame.get("session_key"), frame.get("speaker_id"), frame.get("trigger_message_id")
    ) != (turn.session_key, turn.author, turn.messages[-1].message_id):
        return None, []
    expected = frame.get("tone_sources")
    if not expected:
        return frame, []
    rows = [r for r in tone_context(turn, host, event) if r["message_id"] in expected]
    if source_signatures(rows) != expected:
        return None, []
    return frame, rows
