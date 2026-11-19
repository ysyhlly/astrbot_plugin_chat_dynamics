"""The hosted decision-model-preview counts and enforces its own 65,536-token window."""

from dataclasses import replace
import json

from .. import member_boundary
from ..context_retrieval import clip_text

MODEL = "decision-model-preview"
CONTEXT_TOKENS = 65_536
# Transport sanity limit, independent of tokens. In particular a valid English
# 64K-token request can be substantially larger than the old 256 KiB body cap.
WIRE_BYTES = 4 * 1024 * 1024


def enabled(client):
    return getattr(client, "context_window_tokens", None) == CONTEXT_TOKENS


def remember_input(host, event, parsed):
    from ..native_decision import selected_decision_client
    if enabled(selected_decision_client(host)) and parsed.message_id and parsed.text:
        event._chat_dynamics_wide_input = (
            str(parsed.message_id), str(parsed.sender_id), clip_text(parsed.text, WIRE_BYTES)
        )


def _expanded_background(turn, host, event):
    """Reuse longer bodies of already selected, attributed history; never widen the source set."""
    frames = []
    for message in turn.background:
        source = message.source_text
        quoted = getattr(message.semantics, "quoted_author_id", "")
        if (not source or not message.text or len(source) <= len(message.text)
                or not message.author or message.semantics is None or (message.reply_to and not quoted)):
            continue
        # A curated or redacted excerpt cannot be replaced with its original.
        if clip_text(source, len(message.text)) != message.text:
            continue
        sources = (message.author, quoted, *message.mentioned_users)
        frames.append({"frame_id": message.message_id, "author_uid": message.author,
                       "source_uids": list(dict.fromkeys(uid for uid in sources if uid)),
                       "provenance_complete": True, "kind": "event", "text": clip_text(source, WIRE_BYTES)})
    if not frames:
        return turn
    filtered, boundary = member_boundary.filter_frames(host, event, frames, purpose="role_input")
    if not boundary.available and (member_boundary.fail_closed(boundary) or boundary.service is not None):
        return turn
    restored = {r["frame_id"]: r["text"] for r in filtered if isinstance(r, dict) and
                r.get("frame_id") and isinstance(r.get("text"), str)}
    tried = {f["frame_id"] for f in frames}
    kept = tuple(replace(m, text=restored[m.message_id], source_text=None) if m.message_id in restored else m
                 for m in turn.background if m.message_id not in tried or m.message_id in restored)
    return replace(turn, background=kept)


def decision_turn(turn, events, host):
    """Expand only this turn's accepted sources, after rechecking their attribution."""
    if any(getattr(event, "_chat_dynamics_strip_quote_media", False) for event in events):
        return turn
    turn = _expanded_background(turn, host, events[-1] if events else None)
    saved = {v[0]: v for event in events if (v := getattr(event, "_chat_dynamics_wide_input", None))}
    if not saved and not turn.truncated:
        return turn
    frames = []
    for message in turn.messages:
        original = saved.get(message.message_id)
        quoted = getattr(message.semantics, "quoted_author_id", "")
        if message.reply_to and not quoted:
            return turn
        text = original[2] if original and original[1] == message.author else message.source_text
        if not isinstance(text, str) or not text:
            return turn
        frames.append({"frame_id": message.message_id, "author_uid": message.author,
                       "source_uids": [uid for uid in (message.author, quoted) if uid],
                       "provenance_complete": bool(message.author), "kind": "event", "text": text})
    allowed, boundary = member_boundary.filter_frames(host, events[-1] if events else None, frames,
                                                     purpose="role_input")
    if not boundary.available and (member_boundary.fail_closed(boundary) or boundary.service is not None):
        return turn
    texts = {r["frame_id"]: r["text"].strip() for r in allowed
             if isinstance(r, dict) and isinstance(r.get("text"), str) and r.get("frame_id")}
    if not turn.messages or any(m.message_id not in texts for m in turn.messages):
        return turn
    messages = tuple(replace(m, source_text=texts[m.message_id]) for m in turn.messages)
    original = "\n".join(texts[m.message_id] for m in messages)
    visible = clip_text(original, WIRE_BYTES)
    return replace(turn, messages=messages, text=visible, source_text=visible, truncated=visible != original)


def reduce_after_overflow(state, questions, retry=0):
    """Retain targets, source IDs, reserved quotes and both ends of long fragments."""
    from copy import deepcopy
    from ..turn_decision import clip_conversation_text
    from .context_budget import shrink_prose
    from ..jev_decision import bound_request_state

    reduced = deepcopy(state)
    if retry:
        # A second confirmed overflow can have arbitrary token density.
        # Repack using the conservative legacy envelope, retaining attribution.
        return bound_request_state(reduced, questions)
    conversation = reduced.get("conversation", {})
    conversation.pop("nearby_context", None)
    conversation.pop("nearby_context_policy", None)
    current = conversation.get("text", "")
    byte_count = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    ratio = min(0.75, CONTEXT_TOKENS * 4 / max(1, byte_count))
    if len(current) > 512:
        clip_conversation_text(conversation, max(512, int(len(current) * ratio)))
    for msg in conversation.get("background", ()):
        if len(msg.get("text", "")) > 200:
            msg["text"] = clip_text(msg["text"], max(200, len(msg["text"]) // 2))
    if reduced == state:
        counts = dict.fromkeys(("state", "branch", "packed"), 2)
        reduced = shrink_prose(state, counts, {"max_" + k: 1 for k in counts})
    return reduced
