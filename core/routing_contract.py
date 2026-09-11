"""Compatibility readers and writers for independent routing uncertainty."""


ROUTING_WEIGHTS_VERSION = "recipient-v2"

# Codes that justify a pending/unformed topic. They contradict a stored
# conclusion of "this message already belongs to a resolved topic".
TOPIC_AMBIGUITY_EVIDENCE = frozenset({"topic_ambiguous", "topic_not_formed"})
UNRESOLVED_TOPIC_STATUS = frozenset({"unformed", "pending"})


def commit_topic_evidence(routing: dict, code: str | None = None) -> list[str]:
    """Evidence that explains the routing's final topic conclusion.

    Evidence is a reason, not a history: a back-fill, burst or rerank that
    resolves a pending topic must drop the codes that justified the previous
    conclusion, or downstream diagnostics, annotations and weight fitting end
    up reading a flag and a reason that disagree. Order is preserved, the
    result is de-duplicated, and the new code (when given) is appended last.
    """
    resolved = (
        not bool(routing.get("topic_ambiguous", False))
        and str(routing.get("topic_status") or "committed") not in UNRESOLVED_TOPIC_STATUS
    )
    evidence: list[str] = []
    for item in routing.get("evidence") or ():
        text = str(item)
        if not text or (resolved and text in TOPIC_AMBIGUITY_EVIDENCE):
            continue
        if text not in evidence:
            evidence.append(text)
    if code and code not in evidence:
        evidence.append(code)
    return evidence


def topic_evidence_is_consistent(routing: dict) -> bool:
    """True when the stored topic conclusion and its evidence agree."""
    evidence = {str(item) for item in routing.get("evidence") or ()}
    resolved = (
        not bool(routing.get("topic_ambiguous", False))
        and str(routing.get("topic_status") or "committed") not in UNRESOLVED_TOPIC_STATUS
    )
    return not (resolved and evidence & TOPIC_AMBIGUITY_EVIDENCE)


def addressee_is_ambiguous(routing: dict, default: bool = False) -> bool:
    """Read recipient uncertainty without letting topic uncertainty veto it.

    Older snapshots with confidence can be migrated without interpreting the
    aggregate ambiguous flag. Incomplete legacy snapshots retain their fallback.
    """
    if "addressee_ambiguous" in routing:
        return bool(routing["addressee_ambiguous"])
    if "addressee_confidence" in routing:
        return float(routing.get("addressee_confidence") or 0.0) < 0.72
    return bool(routing.get("ambiguous", default))
