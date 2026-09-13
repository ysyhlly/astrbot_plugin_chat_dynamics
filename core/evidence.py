"""Bounded routing diagnostics. Scores are heuristic evidence, never probabilities."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, asdict, is_dataclass
import math

DOMAINS = frozenset({"topic", "parent", "recipient", "participation"})
ROUTING_CODES = frozenset({
    "explicit_reply", "explicit_mention", "platform_wake", "direct_name_call",
    "quoted_subject_active_interlocutor", "active_dialogue_answer", "inferred_reply",
    "active_interlocutor_followup", "topic_boundary", "topic_profile",
    "topic_reply_continuation", "topic_reply_evidence", "topic_ambiguous",
    "topic_not_formed", "topic_burst_confirmed", "topic_reopen", "topic_llm_rerank",
    "topic_backfill", "pending_followup", "parent_override", "parent_topic_override", "selected_score",
    "semantic", "topic_affinity", "qa_fit", "temporal", "turn_proximity",
    "participant", "centroid", "exemplar", "recent", "lineage", "recency", "lexical",
    "dialogue_time_decay", "dialogue_answer_shape", "dialogue_turn_factor",
    "dialogue_competitor_factor", "dialogue_continuity_score",
})
ROUTING_SOURCES = frozenset({"topic_resolver", "parent_retriever", "recipient_resolver", "routing",
                             "dialogue_continuity"})
# Dialogue continuity components are numeric evidence, not boolean codes: keep the
# raw value recorded when a later stage rebuilds the ledger.
DIALOGUE_FACTORS = frozenset({
    "dialogue_time_decay", "dialogue_answer_shape", "dialogue_turn_factor",
    "dialogue_competitor_factor", "dialogue_continuity_score",
})


def finite_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return value if math.isfinite(value) else None
        except (ValueError, OverflowError):
            pass
    return None


@dataclass(frozen=True)
class EvidenceEntry:
    domain: str
    code: str
    source: str
    raw_value: float
    contribution: float | None = None


@dataclass(frozen=True)
class EvidenceLedger:
    entries: tuple[EvidenceEntry, ...] = ()

    def export(self):
        return sanitize_ledger(self)


def sanitize_ledger(value):
    """Fresh allowlisted snapshot; no strings except known diagnostic identifiers."""
    from .participation_policy import EVIDENCE_CODES, EVIDENCE_SOURCES
    if is_dataclass(value):
        value = asdict(value)
    items = value.get("entries", ()) if isinstance(value, Mapping) else ()
    result = []
    for item in list(items)[:128] if isinstance(items, (list, tuple)) else ():
        if is_dataclass(item):
            item = asdict(item)
        if not isinstance(item, Mapping):
            continue
        domain, code, source = (item.get(k) for k in ("domain", "code", "source"))
        if not all(isinstance(v, str) for v in (domain, code, source)) or domain not in DOMAINS:
            continue
        codes = EVIDENCE_CODES if domain == "participation" else ROUTING_CODES
        sources = EVIDENCE_SOURCES if domain == "participation" else ROUTING_SOURCES
        raw = finite_number(item.get("raw_value"))
        contribution = finite_number(item.get("contribution"))
        if code in codes and source in sources and raw is not None:
            result.append(dict(domain=domain, code=code, source=source,
                               raw_value=raw, contribution=contribution))
    return dict(version=1, calibrated=False, entries=result)


def routing_ledger(routing):
    """Rebuild final decisions after rerank/backfill, retaining numeric factor details."""
    if is_dataclass(routing):
        routing = asdict(routing)
    routing = routing if isinstance(routing, Mapping) else {}
    previous = sanitize_ledger(routing.get("ledger", {}))["entries"]
    factors = {"semantic", "topic_affinity", "qa_fit", "temporal", "turn_proximity",
               "participant", "centroid", "exemplar", "recent", "lineage", "recency", "lexical"}
    entries = [e for e in previous if e["code"] in factors or e["code"] in DIALOGUE_FACTORS
               or e["code"] in {"parent_override", "parent_topic_override"}]
    # A later topic decision supersedes the old candidate's factor breakdown.
    codes = routing.get("evidence", ())
    codes = [c for c in codes if isinstance(c, str)] if isinstance(codes, (list, tuple)) else []
    if set(codes) & {"topic_llm_rerank", "topic_burst_confirmed", "topic_backfill", "pending_followup", "topic_reopen", "parent_topic_override"}:
        entries = [e for e in entries if not (e["domain"] == "topic" and e["code"] in factors)]
    for domain, field in (("topic", "topic_confidence"), ("parent", "parent_confidence"), ("recipient", "addressee_confidence")):
        entries.append(dict(domain=domain, code="selected_score", source="routing", raw_value=routing.get(field, 0.0)))
    for code in dict.fromkeys(codes):
        if code not in ROUTING_CODES:
            continue
        domain = "topic" if code.startswith("topic_") or code == "pending_followup" else "recipient"
        source = "topic_resolver" if domain == "topic" else "recipient_resolver"
        entries.append(dict(domain=domain, code=code, source=source, raw_value=1.0))
        if code in {"explicit_reply", "inferred_reply"}:
            entries.append(dict(domain="parent", code=code, source="routing", raw_value=1.0))
    return sanitize_ledger({"entries": entries})
