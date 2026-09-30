"""One versioned decision snapshot for Kev serving, export, and replay.

The complete Command Code builder already validates message identities, text,
timestamps, Persona, and Environment. Reuse it so a second backend cannot
silently invent a different view of the same turn.
"""

from __future__ import annotations

import json

from .integrations.agentjev import build_cmdcode_request

SCHEMA_VERSION = "chat_decision_v1"
MAX_SNAPSHOT_BYTES = 24_000


def build_decision_snapshot(state: dict, questions: dict) -> dict | None:
    """Return a lossless, bounded snapshot or refuse incomplete evidence.

    No text is clipped here. Token admission must be checked with the pinned Kev
    tokenizer before training or active deployment.
    """
    built = build_cmdcode_request(state, questions)
    if built is None:
        return None
    try:
        snapshot = json.loads(built[0]["state"])
        candidates = state.get("target_candidates", {})
        targets = {}
        for qid in questions:
            if not qid.startswith("target."):
                continue
            candidate = candidates.get(qid) if isinstance(candidates, dict) else None
            if (not isinstance(candidate, dict) or candidate.get("text_missing") is True
                    or not isinstance(candidate.get("message_id"), str)
                    or not isinstance(candidate.get("text"), str) or not candidate["text"].strip()):
                return None
            targets[qid] = {"message_id": candidate["message_id"],
                            "author": str(candidate.get("author") or ""),
                            "text": candidate["text"]}
        if targets:
            snapshot["target_candidates"] = targets
        snapshot = {"schema_version": SCHEMA_VERSION, **snapshot}
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (KeyError, TypeError, ValueError, RecursionError):
        return None
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        return None
    return snapshot


def snapshot_json(snapshot: dict) -> str:
    """Stable bytes for comparing online and offline state construction."""
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
