"""The reasoning store: free-text decision reasoning, kept apart from the trace.

Why a second store
------------------
`decision_trace` is a bounded-facts record and must stay one -- see
`core/persona_trace` and `tests/test_trace_free_text_boundary.py`. The reasoning
chain is wanted anyway, because a learner can only pick up *judgement* from prose
that explains a judgement; enumerables alone teach classification.

So the prose lives here instead, under its own key prefix and its own contract
(`docs/reasoning-store-contract.md`), which says plainly that this data contains
text and where it goes. Keeping it out of `decision_trace` is not tidiness: it is
what keeps the bounded record honest for every consumer that relies on "no
message text" being true of it.

Contents and rules
------------------
* only what the model wrote about its own reasoning -- `rationale`,
  `why_rejected`, `response_goal`. Never conversation text, never persona text.
* every record is scrubbed on the way in by `core.scrub`. Scrubbing is
  best-effort and must not be described as anonymisation.
* the pseudonym salt is per installation and rotatable; rotating it severs
  linkability across records.
* the store is opt-in. Off by default means "reasoning is not collected", not
  "reasoning is collected and hidden", and turning it on is a recorded act.

Writing is best-effort too: if the store is unavailable the decision is
unaffected. A record of reasoning must never cost the reasoning it records.
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from . import scrub

STORE_SCHEMA_VERSION = 1
SALT_KEY = "chat_dynamics_reasoning_salt_v1"
RECORD_KEY_PREFIX = "chat_dynamics_reasoning_v1_"


class KeyValueBackend(Protocol):
    async def get_kv_data(self, key: str, default: Any) -> Any: ...
    async def put_kv_data(self, key: str, value: Any) -> None: ...


async def load_salt(backend: KeyValueBackend) -> bytes:
    """The installation's pseudonym salt, created on first use.

    Hex-encoded at rest because the KV backends in play store JSON documents and
    raw bytes have no faithful representation there.
    """
    raw = await backend.get_kv_data(SALT_KEY, "")
    if isinstance(raw, str) and len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    salt = scrub.new_salt()
    await backend.put_kv_data(SALT_KEY, salt.hex())
    return salt


def build_record(turn, decision, *, session_hash: str, message_ids: Sequence[str],
                 salt: bytes, now: float) -> dict[str, Any] | None:
    """One turn's reasoning, scrubbed. None when there is no reasoning to keep.

    Built synchronously and separately from the write so it can be tested without
    a backend and so a failure here cannot reach the decision path.
    """
    identifiers = scrub.collect_identifiers(turn)
    substitutions = scrub.build_substitutions(identifiers, salt)

    rationale = scrub.scrub(getattr(decision, "rationale", "") or "", substitutions)
    goal = scrub.scrub(getattr(decision, "response_goal", "") or "", substitutions)
    alternatives = [
        {"action": action, "why_rejected": scrub.scrub(why, substitutions)}
        for action, why in getattr(decision, "alternatives", ())
        if why
    ]
    if not rationale and not goal and not alternatives:
        return None
    return {
        "store_schema_version": STORE_SCHEMA_VERSION,
        "session_hash": session_hash,
        "msg_ids": [str(m) for m in message_ids],
        "ts": float(now),
        # The bounded half is referenced, never duplicated, so the two records
        # cannot drift apart into two different accounts of one decision.
        "decision": {"action": getattr(decision, "action", ""),
                     "state": getattr(decision, "state", ""),
                     "length": getattr(decision, "length", ""),
                     "reason_code": getattr(decision, "reason_code", ""),
                     "reason_category": getattr(decision, "reason_category", "")},
        "rationale": rationale,
        "response_goal": goal,
        "alternatives": alternatives,
        "scrubbed": True,
    }


async def save(backend: KeyValueBackend, session_hash: str, record: Mapping[str, Any]) -> None:
    """Append one record to its session's list.

    Appends are read-modify-write by construction; the KV backends here have no
    atomic append, and reasoning records are low-volume enough that the lost-update
    window is acceptable. Losing a record is a gap in training data, never a
    correctness problem, which is the same asymmetry that governs the whole store.
    """
    key = RECORD_KEY_PREFIX + session_hash
    rows = await backend.get_kv_data(key, [])
    if not isinstance(rows, list):
        rows = []
    rows.append(dict(record))
    await backend.put_kv_data(key, rows)
