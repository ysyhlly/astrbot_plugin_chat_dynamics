"""Lossless parsing and evidence checks shared by collection and training."""

import copy
import json
import re

SNAPSHOT_VERSION = "1"


def parse_snapshot(state):
    """Read structured snapshots, including complete legacy JSON pairs.

    Plain text remains supported. A JSON-looking input must be wholly valid;
    recovering only its prefix would silently certify a truncated snapshot.
    """
    if isinstance(state, dict):
        return copy.deepcopy(state)
    if not isinstance(state, str) or not state.lstrip().startswith(("{", "[")):
        return None
    decoder = json.JSONDecoder()
    remaining = state.strip()
    result = {}

    def merge(left, right):
        for key, value in right.items():
            if key not in left:
                left[key] = value
            elif isinstance(left[key], dict) and isinstance(value, dict):
                merge(left[key], value)
            else:
                raise ValueError("ambiguous structured snapshot")

    try:
        while remaining:
            value, end = decoder.raw_decode(remaining)
            if not isinstance(value, dict):
                raise ValueError("structured snapshot must contain objects")
            merge(result, value)
            remaining = remaining[end:].strip()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid structured snapshot; complete JSON required") from exc
    return result


def validate_snapshot(state, questions=None):
    """Reject corrupt JSON and explicitly missing dynamic-candidate evidence."""
    parsed = parse_snapshot(state)
    if parsed is None:
        return
    conversation = parsed.get("conversation", {})
    if isinstance(conversation, dict) and conversation.get("truncated") is True:
        raise ValueError("incomplete current message snapshot")
    routing = parsed.get("routing_semantics", parsed)
    messages = [m for container in (conversation, routing, parsed) if isinstance(container, dict)
                for key in ("messages", "background", "recent_messages")
                for m in (container.get(key, []) if isinstance(container.get(key), list) else [])
                if isinstance(m, dict)]
    targets = parsed.get("target_candidates", {})
    recipients = routing.get("recipient_candidates", {}) if isinstance(routing, dict) else {}
    for key, question in (questions or {}).items():
        instructions = question.get("instructions", "")
        if key.startswith("target."):
            candidate = targets.get(key) if isinstance(targets, dict) else None
            if candidate is None and isinstance(instructions, str):
                match = re.search(r"address message (\S+)\?", instructions)
                if match:
                    candidate = next((m for m in messages if str(m.get("message_id")) == match[1]), None)
                    if candidate is None:
                        raise ValueError("missing target candidate evidence")
            if candidate is not None and (not isinstance(candidate, dict) or not candidate.get("text")
                                          or candidate.get("text_missing")):
                raise ValueError("missing target candidate text")
        elif key.startswith("recipient.") and isinstance(recipients, dict) and recipients:
            candidate = recipients.get(key)
            if not isinstance(candidate, dict) or not candidate.get("user_id"):
                raise ValueError("missing recipient candidate evidence")
            if not candidate.get("is_bot") and not any(m.get("text") for m in candidate.get("messages", [])
                                                       if isinstance(m, dict)):
                raise ValueError("missing recipient candidate text")
