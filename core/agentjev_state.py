"""One bounded AgentJev state representation for training and inference."""

from __future__ import annotations

import json
import hashlib


MAX_PATH_BYTES = 1950
SEMANTIC_KEYS = ("recipient_ids", "basis", "certainty", "quoted_message_id",
                 "quoted_author_id", "parent_message_id", "mentioned_user_ids",
                 "bot_is_addressee", "subject_is_bot", "routing_ambiguous")


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore")


def _message(item: dict, *, text_bytes: int) -> dict:
    result = {key: item[key] for key in
              ("message_id", "author", "reply_to", "mentioned_users", "timestamp")
              if item.get(key) not in (None, "", [], ())}
    if isinstance(item.get("text"), str):
        result["text"] = _text(item["text"], text_bytes)
    semantics = item.get("semantics")
    if isinstance(semantics, dict):
        routing = {key: semantics[key] for key in SEMANTIC_KEYS
                   if semantics.get(key) not in (None, "", [], ())}
        if routing:
            result["routing"] = routing
    return result


def pack_state(state: dict) -> tuple[str, str]:
    """Preserve addressivity evidence while bounding untrusted message bodies."""
    conversation = state["conversation"]
    current = {"text": _text(conversation.get("text"), 1024)}
    current.update({key: conversation[key] for key in ("author", "explicit", "truncated")
                    if key in conversation})
    current["messages"] = [_message(item, text_bytes=180)
                           for item in (conversation.get("messages") or [])[-4:]
                           if isinstance(item, dict)]
    packed = {"current": current}
    background = [_message(item, text_bytes=140)
                  for item in (conversation.get("background") or [])[-3:]
                  if isinstance(item, dict)]
    if background:
        packed["background"] = background
    for key in ("routing_semantics", "observations", "participation_policy", "previous_state"):
        if key in state:
            packed[key] = state[key]
    if "persona" in state:
        packed["persona"] = _text(state["persona"], 180)
    candidates = state.get("target_candidates")
    if isinstance(candidates, dict):
        # The complete candidate body is already supplied in the target
        # question's option. Repeating it here displaces message context.
        packed["target_candidates"] = {
            key: {field: value for field, value in item.items()
                  if field in ("message_id", "author", "text_missing")}
            for key, item in candidates.items() if isinstance(item, dict)
        }
    encoded = json.dumps(packed, ensure_ascii=False, separators=(",", ":"))
    prefix = json.dumps({"current": current}, ensure_ascii=False,
                        separators=(",", ":"))[:-1]
    return encoded, prefix


def path_fits(state: str, question: str, options: list[str]) -> bool:
    """UTF-8 bytes upper-bound the number of tokenizer input tokens."""
    return all(len(("[STATE] " + state + "\n[QUESTION] " + question
                    + "\n[CANDIDATE] " + option).encode("utf-8")) <= MAX_PATH_BYTES
               for option in options)


def target_option(message_id: str, description: str) -> str:
    """Keep identity at the tail; upstream candidate truncation keeps the tail."""
    identity = _text(str(message_id), 80)
    body = _text(description, 180)
    fingerprint = hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:16]
    return f"{identity}: {body} [message_id:{fingerprint}]"
