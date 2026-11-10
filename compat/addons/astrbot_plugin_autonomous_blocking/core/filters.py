"""Pure, provenance-aware filtering helpers for downstream context providers."""

from __future__ import annotations

import copy
from typing import Any


def _shielded(runtime: Any, context: dict[str, Any]) -> set[str]:
    snapshot = runtime.policy_snapshot(context)
    return {
        str(rule.get("target_uid"))
        for rule in snapshot.get("rules", [])
        if rule.get("mode") == "shield"
    }


def _sources(frame: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("author_uid", "subject_id"):
        value = frame.get(key)
        if value is not None and str(value).strip():
            values.add(str(value).strip())
    for key in ("source_uids", "authors"):
        value = frame.get(key)
        if isinstance(value, (list, tuple, set)):
            values.update(str(item).strip() for item in value if item is not None and str(item).strip())
    return values


def filter_frames(runtime: Any, context: dict[str, Any], frames: list[dict[str, Any]], purpose: str = "") -> list[dict[str, Any]]:
    """Return copied frames, removing shielded or unverifiable material conservatively."""
    shielded = _shielded(runtime, context)
    result = []
    for frame in frames or []:
        if not isinstance(frame, dict):
            continue
        sources = _sources(frame)
        if sources.intersection(shielded):
            continue
        if shielded and not bool(frame.get("provenance_complete")):
            continue
        result.append(copy.deepcopy(frame))
    return result


def filter_material(runtime: Any, context: dict[str, Any], bundle: dict[str, Any], purpose: str = "") -> dict[str, Any]:
    """Filter known frame collections without altering source text or static persona data."""
    result = copy.deepcopy(bundle) if isinstance(bundle, dict) else {}
    for key in ("frames", "history", "retrieval", "quotes", "references", "materials", "context_items"):
        value = result.get(key)
        if isinstance(value, list):
            result[key] = filter_frames(runtime, context, value, purpose)
    snapshot = result.get("snapshot")
    if isinstance(snapshot, list):
        result["snapshot"] = filter_frames(runtime, context, snapshot, purpose)
    elif isinstance(snapshot, dict):
        shielded = _shielded(runtime, context)
        for key in ("frames", "history", "context_items", "messages", "retrieval", "materials"):
            value = snapshot.get(key)
            if isinstance(value, list):
                snapshot[key] = filter_frames(runtime, context, value, purpose)
        # Presentation summaries are dynamic projections, not static persona or
        # worldbook material. Unless they carry a complete, explicit UID source
        # set, their relation to a shielded user cannot be established safely.
        for key in ("global_presentation", "scoped_presentation") if shielded else ():
            projection = snapshot.get(key)
            if projection is None:
                continue
            if not isinstance(projection, dict):
                snapshot.pop(key, None)
                continue
            sources = _sources(projection)
            if not projection.get("provenance_complete") or not sources or sources.intersection(shielded):
                snapshot.pop(key, None)
    return result
