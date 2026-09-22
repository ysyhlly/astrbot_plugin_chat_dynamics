"""Where a turn finally ended up: the fact schema 2 could not record.

Schema 2 could say "the router admitted a reply". It could not say whether
anything was ever sent, which made three unrelated failures read identically to
anyone looking at the annotation record:

    admitted, 作息/降温/媒体 把发送压掉   -> the gate said no, not a model error
    admitted, 生成超时/空回复            -> generation failed
    admitted, 生成成功但平台发送失败      -> delivery failed

Collapsed into one "missed_reply", all three say "the router should have
replied" — and moving `strong_addressivity_threshold` cannot fix any of them.

This module is the only place that writes the outcome. Every checkpoint in the
turn — never attempted, gate suppression, generation failure, delivery failure,
delivered — calls one of the `mark_*` helpers, so the vocabulary lives in one
file instead of being spelled out at five call sites in `main.py`.

Two rules that decide whether the recording is usable:

* **the outcome is a property of the message, not of the attempt.** It is stored
  on the node's metadata (and mirrored into the frozen decision trace, so a
  reader that only kept the trace still sees it), keyed by nothing else;
* **delivery is terminal.** Once any fragment of a reply has gone out, the turn
  is `delivered`; a later fragment failing does not retract that, and a
  partially delivered reply is a delivered one.

Deliberately independent of the learning plugin: this is the host describing
what it did, and it has no knowledge of who reads it.
"""
from __future__ import annotations

import time
from copy import deepcopy
from collections.abc import Mapping

OUTCOME_KEY = "outcome"

VALUE_DELIVERED = "delivered"
VALUE_SUPPRESSED = "suppressed"
VALUE_GENERATION_FAILED = "generation_failed"
VALUE_DELIVERY_FAILED = "delivery_failed"
VALUE_NOT_ATTEMPTED = "not_attempted"
# Host-internal, and deliberately outside the learning vocabulary: it marks the
# window between "the gate said speak" and "we know how it ended". A reader that
# meets it (because the process died mid-generation) reads it as an unrecognised
# value, which is the honest reading — nobody ever found out how that turn ended.
VALUE_IN_FLIGHT = "in_flight"

STAGE_ADMISSION = "admission"
STAGE_GATE = "gate"
STAGE_GENERATION = "generation"
STAGE_DELIVERY = "delivery"

MAX_REASON = 96


def _metadata(node: object) -> dict | None:
    """The node's metadata mapping, or None when it has none.

    None and `{}` are deliberately different answers: an empty metadata dict is
    a node that can be written to, and treating it as "nowhere to write" would
    silently drop the outcome of exactly the turns that had nothing else
    recorded about them.
    """
    metadata = getattr(node, "metadata", None)
    return metadata if isinstance(metadata, dict) else None


def read_outcome(source: object) -> dict:
    """The outcome block recorded on a node (or a metadata mapping), or {}.

    Accepts a node, a metadata dict, or a decision-trace dict: after the trace
    is frozen the fact is mirrored into it, and a caller holding either should
    not have to know which.
    """
    if isinstance(source, Mapping):
        block = source.get(OUTCOME_KEY)
        return dict(block) if isinstance(block, Mapping) else {}
    return read_outcome(_metadata(source) or {})


def _should_write(existing: object, value: str) -> bool:
    if not isinstance(existing, Mapping):
        return True
    return existing.get("final_outcome") != VALUE_DELIVERED


def _write(node: object, value: str, *, delivered: bool, reason: str = "",
           stage: str, now: float | None = None) -> dict:
    block = {
        "final_outcome": value,
        "delivered": bool(delivered),
        "suppression_reason": str(reason or "")[:MAX_REASON],
        "stage": stage,
        "recorded_at": time.time() if now is None else float(now),
    }
    metadata = _metadata(node)
    if metadata is None:
        return block
    if not _should_write(metadata.get(OUTCOME_KEY), value):
        return dict(metadata[OUTCOME_KEY])
    if value == VALUE_DELIVERED:
        latency = metadata.get("turn_latency")
        if isinstance(latency, dict) and "first_send_seconds" not in latency:
            started = latency.get("started")
            if isinstance(started, (int, float)) and not isinstance(started, bool):
                elapsed = time.perf_counter() - started
                if 0 <= elapsed < float("inf"):
                    latency["first_send_seconds"] = elapsed
    metadata[OUTCOME_KEY] = dict(block)
    trace = metadata.get("decision_trace")
    if isinstance(trace, dict):
        # The trace was frozen before any of this was known, so the fact is
        # written into it afterwards. A reader that kept only the trace — the
        # learning plugin does exactly that — would otherwise see an admitted
        # turn with no ending, which is schema 2 all over again.
        updated = deepcopy(trace)
        updated[OUTCOME_KEY] = dict(block)
        metadata["decision_trace"] = updated
    observer = getattr(node, "_decision_learning_outcome", None)
    if callable(observer):
        try:
            observer(dict(block))
        except Exception:
            # Optional training telemetry never changes delivery semantics.
            pass
    return block


def mark_not_attempted(node: object, *, now: float | None = None) -> dict:
    """The reply flow was never entered. The default for every processed turn.

    Written at trace-build time so that "we did not try" is a recorded fact
    rather than a missing one: an absent outcome and a negative one are
    different claims, and only the second can be counted.
    """
    return _write(node, VALUE_NOT_ATTEMPTED, delivered=False, stage=STAGE_ADMISSION, now=now)


def mark_suppressed(node: object, reason: str, *, stage: str = STAGE_GATE,
                    now: float | None = None) -> dict:
    """The gate (or the arbiter) decided not to speak. `reason` is its code."""
    return _write(node, VALUE_SUPPRESSED, delivered=False, reason=reason,
                  stage=stage, now=now)


def mark_generation_failed(node: object, reason: str = "",
                           *, now: float | None = None) -> dict:
    return _write(node, VALUE_GENERATION_FAILED, delivered=False, reason=reason,
                  stage=STAGE_GENERATION, now=now)


def mark_delivery_failed(node: object, reason: str = "send_failed",
                         *, now: float | None = None) -> dict:
    return _write(node, VALUE_DELIVERY_FAILED, delivered=False, reason=reason,
                  stage=STAGE_DELIVERY, now=now)


def mark_in_flight(node: object, *, now: float | None = None) -> dict:
    """The reply flow was entered; how it ends is not known yet."""
    return _write(node, VALUE_IN_FLIGHT, delivered=False, stage=STAGE_GENERATION, now=now)


def mark_delivered(node: object, *, now: float | None = None) -> dict:
    """At least one fragment reached the platform. Terminal."""
    return _write(node, VALUE_DELIVERED, delivered=True, stage=STAGE_DELIVERY, now=now)


__all__ = [
    "MAX_REASON", "OUTCOME_KEY", "STAGE_ADMISSION", "STAGE_DELIVERY", "STAGE_GATE",
    "STAGE_GENERATION", "VALUE_DELIVERED", "VALUE_DELIVERY_FAILED",
    "VALUE_GENERATION_FAILED", "VALUE_IN_FLIGHT", "VALUE_NOT_ATTEMPTED", "VALUE_SUPPRESSED",
    "mark_delivered", "mark_delivery_failed", "mark_generation_failed", "mark_in_flight",
    "mark_not_attempted", "mark_suppressed", "read_outcome",
]
