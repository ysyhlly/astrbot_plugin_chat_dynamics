"""The turn-facing half of the learning-policy consumer.

`core/learning_policy.py` answers "what does the published file say and may we
use it". This answers the questions a running turn asks: what are this host's
*configured* values for the parameters a policy can move, what should the
effective configuration be once a policy is folded in, and when was it last
read.

It was carved out of `main.py` in v1.7.0 as the first stage of splitting that
file. The boundary is deliberately narrow — the consumer's resolution rules stay
in `learning_policy.py`, and nothing here knows how a message is routed — so
the two other stages (the turn pipeline, the console surface) can move later
without renegotiating this one.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from .learning_policy import MODE_OFF, LearningPolicyConsumer

# The parameters a published policy may move, in the host's own field names.
# Kept as a list rather than derived from ALLOWED_PARAMS because the two answer
# different questions: ALLOWED_PARAMS is what may be *applied*, this is what is
# read out of the configuration to describe the baseline.
BASELINE_FIELDS = (
    "strong_addressivity_threshold",
    "safe_hover_threshold",
    "topic_commit_threshold",
    "topic_join_threshold",
    "topic_margin_threshold",
    "parent_accept_threshold",
)

MIN_REFRESH_SECONDS = 10


class LearningPolicyRuntime:
    """Caches the consumer and the read throttle for one plugin instance."""

    def __init__(self, *, mode: str = MODE_OFF, source_id: str = "",
                 expected_policy_id: str = "", expected_dataset_fingerprint: str = "",
                 host_version: str = "") -> None:
        # An empty source_id is the consumer's own default; passing it through
        # keeps the scope id in one place.
        self.consumer = LearningPolicyConsumer(
            mode=mode or MODE_OFF,
            source_id=source_id,
            expected_policy_id=expected_policy_id,
            expected_dataset_fingerprint=expected_dataset_fingerprint,
            host_version=host_version,
        )
        self.last_read_at = 0.0

    # ---- what the policy assumed ---------------------------------------

    @staticmethod
    def configured_values(config: Any, *, topic_join_threshold: float) -> dict[str, float]:
        """The whitelisted parameters as *configured*, for the baseline digest.

        Read from the configuration rather than from the live router attributes,
        because the live attributes already carry any applied policy: hashing
        those would make a policy invalidate its own compatibility check on the
        next refresh, and the mode would flap between active and incompatible
        every other interval.

        `topic_commit_threshold` is derived here exactly as
        `ThreadRouter.configure_topics` derives it, because the configuration
        field carries 0.0 to mean "derive me" while the value the policy was
        calibrated against is the derived one. The derivation is duplicated on
        purpose — the live value cannot be used — and the cross-repo test in the
        learning plugin holds the two rules together, so a change to either one
        fails loudly instead of leaving every published digest quietly
        unmatched.
        """
        values: dict[str, float] = {}
        for name in BASELINE_FIELDS:
            value = getattr(config, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values[name] = float(value)
        join = values.get("topic_join_threshold")
        if values.get("topic_commit_threshold") in (None, 0.0) and join is not None:
            values["topic_commit_threshold"] = (max(topic_join_threshold, join + 0.10)
                                                if join < topic_join_threshold else join)
        return values

    # ---- what the host will actually use --------------------------------

    def apply_to(self, config: Any, *, make: Callable[..., Any]) -> Any:
        """Fold an applied policy into a config, or return it unchanged.

        Injecting here rather than poking the routers afterwards means every
        consumer of the config — the addressivity router, the topic thresholds,
        the effective-config view in the panel — sees one consistent set of
        values, and a later config refresh cannot silently revert the policy.
        """
        if not self.consumer.decision.applied:
            return config
        changes = {name: float(value) for name, value in self.consumer.overrides().items()
                   if hasattr(config, name)}
        if not changes:
            return config
        adjusted = make(config, **changes)
        if adjusted.safe_hover_threshold >= adjusted.strong_addressivity_threshold:
            # The policy was validated as a set; a pair that overlaps would make
            # the admission rule undefined, so the whole set is dropped rather
            # than half-applied.
            return config
        return adjusted

    def configure(self, config: Any) -> None:
        self.consumer.configure(
            mode=getattr(config, "learning_policy_mode", MODE_OFF),
            expected_policy_id=getattr(config, "learning_policy_expected_policy_id", ""),
            expected_dataset_fingerprint=getattr(
                config, "learning_policy_expected_dataset_fingerprint", ""))

    def due(self, *, now: float, config: Any, force: bool = False) -> bool:
        if force:
            return True
        interval = max(MIN_REFRESH_SECONDS,
                       int(getattr(config, "learning_policy_refresh_seconds", 60) or 60))
        return now - self.last_read_at >= interval

    async def refresh(self, *, effective_config: Mapping[str, float]) -> Any:
        decision = await self.consumer.refresh(effective_config=effective_config)
        return decision

    def status(self) -> dict[str, Any]:
        return self.consumer.decision.as_dict()


__all__ = ["BASELINE_FIELDS", "MIN_REFRESH_SECONDS", "LearningPolicyRuntime"]
