"""Turn an existing human annotation into labellable samples.

The replay annotations already store the decision trace, including the ledger,
so no new runtime collection path is needed and no message text is copied. Two
labelled tasks come out of one record: topic assignment, and whether the bot
should have been addressed. A third appears when the annotator also recorded
whether a reply was wanted.
"""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

from ..message_features import analyze_text
from .sample import FEATURE_CODES, LearningSample

NONE = "none"
BOT = "bot"
OTHER = "other"
REPLY = "reply"
SILENT = "silent"


def facts_from_text(text: str) -> dict[str, float]:
    """Shape facts a labelled message carries, without keeping the text itself."""
    features = analyze_text(str(text or ""))
    return {
        "fact.is_short": float(features.is_short),
        "fact.is_elliptical": float(features.is_elliptical),
        "fact.is_ack": float(features.is_ack),
        "fact.is_question": float(features.is_question),
        "fact.is_answer_like": float(features.is_answer_like),
        "fact.is_topic_boundary": float(features.is_topic_boundary),
        "fact.is_filler": float(features.is_filler),
        "fact.can_start_topic": float(features.can_start_topic),
        "fact.question_ending": float(features.question_ending),
        "fact.answer_boundary": float(features.answer_boundary),
        "fact.reaction_like": float(features.is_filler or features.is_ack),
        "fact.information_density": float(features.information_density),
        "fact.lexical_terms": float(len(features.lexical_keywords)),
    }


def facts_from_trace(trace: Mapping | None, routing: Mapping | None = None) -> dict[str, float]:
    """Identity facts the trace already recorded; none of them need the text."""
    identity = (trace or {}).get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    codes = (routing or {}).get("evidence", ())
    codes = codes if isinstance(codes, (list, tuple)) else ()
    return {
        "fact.bot_mentioned": float(bool(identity.get("mention"))),
        "fact.bot_vocative": float(bool(identity.get("vocative"))),
        "fact.bot_subject": float(bool(identity.get("subject"))),
        "fact.has_reply": float("explicit_reply" in codes),
    }


def features_from_trace(trace: Mapping | None, routing: Mapping | None = None, *,
                        text: str = "") -> tuple[tuple[str, float], ...]:
    """Raw factor values from the ledger; applied codes fall back to 1.0.

    Message facts join only when the annotation kept the text, which depends on
    the console content switch. Absent facts stay absent rather than being
    guessed, so a factor table can always say how many rows carried each value.
    """
    collected: dict[str, float] = {}
    entries = (trace or {}).get("ledger", {})
    entries = entries.get("entries", ()) if isinstance(entries, Mapping) else ()
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        if not isinstance(entry, Mapping):
            continue
        code, raw = entry.get("code"), entry.get("raw_value")
        if (code in FEATURE_CODES and isinstance(raw, (int, float))
                and not isinstance(raw, bool) and isfinite(raw)):
            collected[code] = float(raw)
    codes = (routing or {}).get("evidence", ())
    for code in codes if isinstance(codes, (list, tuple)) else ():
        if code in FEATURE_CODES:
            collected.setdefault(code, 1.0)
    collected.update(facts_from_trace(trace, routing))
    if isinstance(text, str) and text.strip():
        collected.update(facts_from_text(text))
    return tuple(sorted(collected.items()))


def _confidence(trace: Mapping, domain: str) -> float:
    value = (trace.get(domain) or {}).get("confidence")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _recipient_label(record: Mapping) -> str | None:
    value = record.get("bot_targeted")
    if value is True:
        return BOT
    if value is False:
        return OTHER
    return None


def _predicted_recipient(trace: Mapping, bot_id: str) -> str:
    ids = (trace.get("recipient") or {}).get("ids") or []
    if bot_id and any(str(item) == bot_id for item in ids):
        return BOT
    return OTHER


def samples_from_annotation(record: Mapping, *, session_id: str = "", bot_id: str = "",
                            source: str = "manual_replay") -> list[LearningSample]:
    """One annotation record in, up to three labelled samples out."""
    if not isinstance(record, Mapping):
        return []
    msg_id = str(record.get("msg_id") or "")
    if not msg_id:
        return []
    trace = record.get("decision_trace")
    trace = trace if isinstance(trace, Mapping) else {}
    routing = record.get("routing")
    routing = routing if isinstance(routing, Mapping) else {}
    features = features_from_trace(trace, routing, text=record.get("text") or "")
    stamp = record.get("annotated_at")
    stamp = float(stamp) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else 0.0
    session = str(session_id or record.get("session_key") or "unknown")
    samples: list[LearningSample] = []

    predicted_topic = str(record.get("predicted_topic") or "UNKNOWN")
    expected_topic = str(record.get("expected_topic") or "UNKNOWN")
    samples.append(LearningSample(
        session, msg_id, stamp, "topic", predicted_topic, expected_topic,
        _confidence(trace, "topic"), source, features, str(record.get("error_type") or "")))

    expected_recipient = _recipient_label(record)
    if expected_recipient is not None:
        samples.append(LearningSample(
            session, msg_id, stamp, "recipient", _predicted_recipient(trace, bot_id),
            expected_recipient, _confidence(trace, "recipient"), source, features,
            str(record.get("recipient_error_type") or "")))

    expected_reply = record.get("expected_reply")
    if isinstance(expected_reply, bool):
        should_reply = (trace.get("participation") or {}).get("should_reply")
        samples.append(LearningSample(
            session, msg_id, stamp, "participation",
            REPLY if should_reply is True else SILENT, REPLY if expected_reply else SILENT,
            _confidence(trace, "participation"), source, features,
            "correct" if should_reply is expected_reply else "wrong_reply_decision"))

    return samples


def samples_from_annotations(records, *, session_id: str = "", bot_id: str = "",
                             source: str = "manual_replay") -> list[LearningSample]:
    result: list[LearningSample] = []
    for record in records if isinstance(records, (list, tuple)) else ():
        result.extend(samples_from_annotation(record, session_id=session_id, bot_id=bot_id,
                                              source=source))
    return result
