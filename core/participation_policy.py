"""Pure participation decisions over immutable recipient and observation snapshots.

Identity resolution and DAG access belong to AddressivityRouter. This policy only
applies the existing participation rules; evidence families are accounting, not
new caps or independent probability estimates.
"""
from __future__ import annotations

from dataclasses import dataclass


EVIDENCE_CODES = frozenset({
    "canonical_recipient", "bot_mention", "other_mention", "vocative", "bot_reply", "routed_bot", "routed_other",
    "bot_subject", "ambient_baseline", "human_quote", "platform_wake", "temporal_gap", "intervening_messages",
    "continuation_cue", "lexical_overlap", "embedding_without_tokens", "recipient_confidence",
    "active_interlocutor", "explicit_thread", "pending_hover",
})
EVIDENCE_FAMILIES = frozenset({"recipient", "baseline", "platform", "temporal", "dialogue", "topic"})
EVIDENCE_SOURCES = frozenset({"policy", "routing", "message.mentions", "identity_matcher", "message.reply",
    "message.is_wake", "message.timestamp", "dag.recent", "continuation_matcher", "lexical_tokens",
    "semantic_match", "dag.dialogue", "dag.thread", "pending_hover"})


@dataclass(frozen=True)
class Evidence:
    """One participation observation.

    "strength" is the contribution the policy applied; "raw_value" is the fact it
    was derived from. They are different quantities and must stay different: a
    7.3 second gap and the +0.25 the policy gives it are not the same number.
    Keeping only the contribution would leave a later learner fitting the current
    weights back out of the current weights.
    """

    code: str
    family: str
    strength: float
    source: str
    raw_value: float | None = None

    def ledger_entry(self):
        from .evidence import EvidenceEntry
        # A caller that never set a raw value keeps the previous behaviour rather
        # than recording a zero that would read as a real observation.
        raw = self.strength if self.raw_value is None else self.raw_value
        return EvidenceEntry("participation", self.code, self.source, raw, self.strength)


@dataclass(frozen=True)
class RecipientSnapshot:
    ids: tuple[str, ...] = ()
    canonical: bool = False
    ambiguous: bool = True
    bot_targeted: bool = False
    confidence: float = 0.0
    bot_confidence: float = 0.0
    topic_confidence: float = 0.0
    subject_is_bot: bool = False


@dataclass(frozen=True)
class ParticipationSnapshot:
    bot_id: str
    recipient: RecipientSnapshot
    observations: tuple[Evidence, ...] = ()
    reason_details: tuple[tuple[str, str], ...] = ()
    parent_user_id: str | None = None
    has_prior_bot: bool = False
    semantic_score: float = 0.0
    lexical_overlap: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParticipationDecision:
    score: float
    level: str
    is_bot_targeted: bool
    target_user_id: str | None
    reasons: tuple[str, ...]
    topic_relevance: float
    evidence: tuple[Evidence, ...]
    family_contributions: tuple[tuple[str, float], ...]
    contribution_total: float


def _result(score, level, targeted, target, reasons, relevance, evidence):
    families = {}
    for item in evidence:
        families[item.family] = families.get(item.family, 0.0) + item.strength
    return ParticipationDecision(score, level, targeted, target, tuple(reasons), relevance,
        tuple(evidence), tuple((key, round(value, 4)) for key, value in families.items()),
        round(sum(item.strength for item in evidence), 4))


@dataclass(frozen=True)
class ParticipationPolicy:
    strong_threshold: float = 0.70
    hover_threshold: float = 0.40

    def explicit(self, snapshot: ParticipationSnapshot) -> ParticipationDecision | None:
        """Resolve definitive supplied recipient evidence without reading context."""
        recipient = snapshot.recipient
        facts = {item.code: item for item in snapshot.observations}
        details = dict(snapshot.reason_details)

        def fixed(code, score, targeted, target, reason, relevance=0.0, raw=None):
            source = facts[code].source if code in facts else "routing"
            # The structural codes are boolean facts — the observation is "this
            # happened" — so their raw value is 1.0 unless a confidence carries
            # the actual measurement.
            observed = raw if raw is not None else 1.0
            # A caller-supplied fact may omit its human-readable detail; the
            # code stays the reason rather than raising on a missing key.
            return _result(score, "strong" if targeted else "weak", targeted, target,
                           [details.get(code, reason)], relevance,
                           [Evidence(code, "recipient", score, source, float(observed))])

        if recipient.canonical and recipient.ids and not recipient.ambiguous:
            targeted = snapshot.bot_id in recipient.ids
            confidence = recipient.bot_confidence if targeted else recipient.confidence
            return fixed("canonical_recipient", max(self.strong_threshold, confidence) if targeted else .15,
                         targeted, snapshot.bot_id if targeted else recipient.ids[0],
                         "Canonical recipient inference", recipient.topic_confidence)
        for code, score in (("bot_mention", 1.0), ("other_mention", .15), ("vocative", .95), ("bot_reply", .98)):
            if code in facts:
                targeted = code != "other_mention"
                if not targeted and "other_target" not in details:
                    continue
                target = snapshot.bot_id if targeted else details["other_target"]
                return fixed(code, score, targeted, target, details.get(code, code),
                             1.0 if targeted else 0.0, raw=1.0)
        if recipient.bot_targeted and recipient.bot_confidence >= .72 and not recipient.ambiguous:
            return fixed("routed_bot", max(self.strong_threshold, min(.94, recipient.bot_confidence)),
                         True, snapshot.bot_id, "Conversation routing identifies bot as addressee",
                         recipient.topic_confidence, raw=recipient.bot_confidence)
        if recipient.ids and snapshot.bot_id not in recipient.ids and recipient.confidence >= .72 and not recipient.ambiguous:
            return fixed("routed_other", .15, False, recipient.ids[0],
                         "Conversation routing identifies another addressee",
                         raw=recipient.confidence)
        if recipient.subject_is_bot and not recipient.bot_targeted:
            return fixed("bot_subject", .20, False, None,
                         "Bot is the subject, not the identified addressee", raw=1.0)
        return None

    def evaluate(self, snapshot: ParticipationSnapshot) -> ParticipationDecision:
        """Apply legacy weights in their original order, then classify once."""
        explicit = self.explicit(snapshot)
        if explicit is not None:
            return explicit
        facts = {item.code: item for item in snapshot.observations}
        details = dict(snapshot.reason_details)
        # The baseline is a constant floor, so 1.0 ("the floor applied") is its
        # honest observation rather than the 0.20 it contributes.
        evidence = [Evidence("ambient_baseline", "baseline", .20, "policy", 1.0)]
        reasons = []
        score, relevance = .20, snapshot.semantic_score

        def add(code, family, contribution, reason, source=None, raw=None):
            """Record a contribution together with the fact that produced it.

            "raw" is the measurement — seconds, a count, a ratio, a confidence —
            never "contribution". A learner that receives the contribution can only
            recover the weights that are already in force.
            """
            nonlocal score
            score += contribution
            observed = raw
            if observed is None and code in facts:
                observed = facts[code].strength
            if observed is None:
                observed = 1.0
            evidence.append(Evidence(code, family, contribution,
                                     source or (facts[code].source if code in facts else "routing"),
                                     float(observed)))
            reasons.append(reason)

        if not snapshot.has_prior_bot:
            reasons.append("No prior bot message in session context")
            if "human_quote" in facts:
                add("human_quote", "recipient", -.15, "Human quote penalty (-0.15)", raw=1.0)
                score = max(.05, round(score, 4))
            return _result(score, "weak", False, snapshot.parent_user_id, reasons, 0.0, evidence)
        if "human_quote" in facts:
            add("human_quote", "recipient", -.15, "Human quote penalty (-0.15)", raw=1.0)
        if "platform_wake" in facts:
            add("platform_wake", "platform", .15, "Platform wake flag without explicit @mention",
                raw=1.0)
        if "temporal_gap" in facts and "human_quote" not in facts:
            gap = facts["temporal_gap"].strength
            if gap < 15.0:
                delta, reason = .25, f"Immediate temporal proximity ({gap:.1f}s <= 15s)"
            elif gap < 45.0:
                delta, reason = .15, f"Close temporal proximity ({gap:.1f}s <= 45s)"
            elif gap < 120.0:
                delta, reason = .05, f"Moderate temporal proximity ({gap:.1f}s <= 120s)"
            else:
                delta, reason = -.10, f"Stale conversation gap ({gap:.1f}s > 120s)"
            # The observation is the gap in seconds; the delta is what the policy
            # decided to do about it.
            add("temporal_gap", "temporal", delta, reason, raw=gap)
        if "intervening_messages" in facts:
            count = int(facts["intervening_messages"].strength)
            delta = .05 if count == 0 else 0.0 if count <= 2 else -.15
            reason = "Zero intervening messages since bot output" if count == 0 else f"{count} intervening messages" + (" (thread divergence)" if count > 2 else "")
            add("intervening_messages", "dialogue", delta, reason, raw=count)
        if "continuation_cue" in facts:
            add("continuation_cue", "dialogue", .15, details["continuation_cue"],
                raw=facts["continuation_cue"].strength)
        if "lexical_overlap" in facts:
            overlap = facts["lexical_overlap"].strength
            if overlap >= .25 or len(snapshot.lexical_overlap) >= 2:
                add("lexical_overlap", "topic", .15, f"Lexical keyword overlap: {list(snapshot.lexical_overlap)[:3]}", raw=overlap)
                relevance = max(relevance, min(1.0, overlap))
            elif len(snapshot.lexical_overlap) == 1:
                add("lexical_overlap", "topic", .05, f"Minor keyword overlap: {list(snapshot.lexical_overlap)}", raw=overlap)
                relevance = max(relevance, min(.4, overlap))
        elif "embedding_without_tokens" in facts and facts["embedding_without_tokens"].strength >= .62:
            add("embedding_without_tokens", "topic", .05, "High embedding cosine without surface overlap",
                raw=facts["embedding_without_tokens"].strength)
        recipient = snapshot.recipient
        if recipient.bot_confidence > 0.0:
            bonus = round(recipient.bot_confidence * .20, 4)
            add("recipient_confidence", "recipient", bonus, f"Router bot addressee confidence bonus (+{bonus:.2f})",
                raw=recipient.bot_confidence)
            relevance = max(relevance, recipient.topic_confidence)
        if "active_interlocutor" in facts:
            add("active_interlocutor", "dialogue", .12,
                "Active interlocutor dialogue continuation bonus (+0.12)", raw=1.0)
            relevance = max(relevance, .45)
        if "explicit_thread" in facts:
            add("explicit_thread", "dialogue", .08,
                "Shares explicit thread with last bot turn", raw=1.0)
            relevance = max(relevance, .35)
        hover_bonus = 0.0
        hover_fact = 0.0
        for observation in snapshot.observations:
            if observation.code == "pending_hover":
                hover_bonus += .12 if hover_bonus else .20
                hover_fact = observation.strength
                relevance = max(relevance, .65 if observation.strength else .45)
        if hover_bonus:
            add("pending_hover", "dialogue", min(.28, hover_bonus),
                "Follow-up resolves a pending safe-hover turn", raw=hover_fact)
        score = max(0.0, min(1.0, score))
        level = "strong" if score >= self.strong_threshold else "hover" if score >= self.hover_threshold else "weak"
        targeted = level == "strong"
        target = snapshot.bot_id if targeted else (snapshot.parent_user_id or (recipient.ids[0] if recipient.ids else None))
        return _result(score, level, targeted, target, reasons, relevance, evidence)
