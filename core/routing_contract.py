"""Compatibility readers for independent topic and recipient uncertainty."""


ROUTING_WEIGHTS_VERSION = "recipient-v2"

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
