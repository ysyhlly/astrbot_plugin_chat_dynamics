"""Read-only consumer for a policy published by Dynamics Learning.

Dynamics Learning studies this plugin's own recorded decisions and publishes a
parameter offer. This module decides whether to *use* that offer, and it is
deliberately the only place in this plugin that knows the learning plugin
exists at all.

Three modes, and the default is the one that changes nothing:

    off     不读取发布文件
    shadow  读取、解析、算出「会改成什么」，但不应用
    active  应用，且必须通过全部兼容性检查

## What compatibility means here

Three independent checks, each catching a different way of being stale:

1. **policy_contract_version** — the file's own protocol. A policy written
   under a newer protocol is refused outright rather than half-read.
2. **validated_host_versions** — the host versions Learning has actually
   replayed this policy against. Membership, not a SemVer comparison:
   `1.7.0 -> 1.7.1` can move a scoring order or a gate sequence, which changes
   the distribution every threshold in the file was calibrated against. An
   empty list means Learning never learned which host produced the data, and
   "cannot verify" is treated exactly like "does not match".
3. **baseline_config_hash** — the digest of the configuration the policy
   assumed. If the operator has changed one of these parameters since, the
   policy's numbers were calibrated against a baseline that no longer exists.

`active` requires all three. `shadow` requires the first and the optional
pins, and marks the rest as `version_mismatch`/`baseline_mismatch` — never
applying them. That is the whole point of the mode: the disagreement data is
worth collecting precisely when the policy is not yet trusted.

## What this module will not do

It applies **only** parameters on `ALLOWED_PARAMS`, and it never writes
anything back to the learning plugin's scope. A consumer that applies whatever
it is handed is not a consumer, it is a remote control.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ACTIVE = "active"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ACTIVE)

# Where the learning plugin publishes. Its own plugin scope, one key: the
# publish contract is materialised on the producer's side so this side reads an
# agreed artefact instead of re-deriving the shape and drifting from it.
LEARNING_SCOPE_ID = "ysyhlly/astrbot_plugin_dynamics_learning"
PUBLISHED_KEY = "learning_published_v1"
CANDIDATE_KEY = "learning_candidate_v1"

# The publish protocol this consumer implements (Learning -> ChatDynamics).
SUPPORTED_POLICY_CONTRACT_VERSIONS = (1,)

# The intersection of "what Dynamics Learning can propose" and "what this
# plugin can apply at runtime".
#
# Anything outside it — an unknown name, or a known name outside its declared
# range — makes the policy *incompatible* rather than partially applied: a
# policy is a validated set, and the offline result describes the whole set, so
# applying the subset this host happens to understand would apply a
# configuration nobody measured. Shadow still resolves the subset, because
# observing a partial resolution is useful once it is marked as partial.
ALLOWED_PARAMS = (
    "strong_addressivity_threshold",
    "safe_hover_threshold",
    "topic_commit_threshold",
    "topic_join_threshold",
    "topic_margin_threshold",
    "parent_accept_threshold",
)

STATUS_OFF = "off"
STATUS_NO_POLICY = "no_policy"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_SHADOW = "shadow"
STATUS_ACTIVE = "active"

# A `params` value outside its parameter's declared range is refused rather
# than clamped: a published 0.67 and a clamped 0.70 are different policies, and
# quietly applying the second would make the recorded result describe neither.
PARAM_RANGES = {
    "strong_addressivity_threshold": (0.50, 0.90),
    "safe_hover_threshold": (0.20, 0.60),
    "topic_commit_threshold": (0.30, 0.95),
    "topic_join_threshold": (0.30, 0.85),
    "topic_margin_threshold": (0.0, 0.50),
    "parent_accept_threshold": (0.50, 0.95),
}


def baseline_config_hash(values: Mapping[str, Any]) -> str:
    """The canonical digest of a resolved policy, over the whitelisted keys.

    This has to be byte-identical to `core/policy.py:baseline_config_hash` on
    the learning side — every name, sorted, four decimals, no whitespace. The
    canonical form is part of the contract, not an implementation detail: a
    different rounding on one side produces a different digest for the same
    configuration, which reads as "the config moved" when it had not.
    """
    canonical = {}
    for name in ALLOWED_PARAMS:
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        canonical[name] = f"{float(value):.4f}"
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _text(value: Any, limit: int = 64) -> str:
    return value[:limit] if isinstance(value, str) else ""


@dataclass(frozen=True)
class PolicyView:
    """One published policy, parsed. Nothing here decides anything yet."""

    policy_id: str = ""
    state: str = ""
    params: Mapping[str, float] = field(default_factory=dict)
    rejected_params: tuple[str, ...] = ()
    contract_version: int | None = None
    trace_schema_version: int | None = None
    learning_version: str = ""
    dataset_fingerprint: str = ""
    chat_dynamics_version: str = ""
    validated_host_versions: tuple[str, ...] = ()
    baseline_config_hash: str = ""
    shadow_observed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "state": self.state,
            "params": {key: round(value, 4) for key, value in self.params.items()},
            "rejected_params": list(self.rejected_params),
            "policy_contract_version": self.contract_version,
            "trace_schema_version": self.trace_schema_version,
            "learning_version": self.learning_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "chat_dynamics_version": self.chat_dynamics_version,
            "validated_host_versions": list(self.validated_host_versions),
            "baseline_config_hash": self.baseline_config_hash,
            "shadow_observed": self.shadow_observed,
        }


@dataclass(frozen=True)
class Decision:
    """What this host will do with the published policy, and why."""

    mode: str = MODE_OFF
    status: str = STATUS_OFF
    policy_id: str = ""
    applied: bool = False
    overrides: Mapping[str, float] = field(default_factory=dict)
    version_mismatch: bool = False
    baseline_mismatch: bool = False
    unverifiable_version: bool = False
    reasons: tuple[str, ...] = ()
    view: PolicyView | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "status": self.status,
            "policy_id": self.policy_id,
            "applied": self.applied,
            "overrides": {key: round(value, 4) for key, value in self.overrides.items()},
            "version_mismatch": self.version_mismatch,
            "baseline_mismatch": self.baseline_mismatch,
            "unverifiable_version": self.unverifiable_version,
            "reasons": list(self.reasons),
            "policy": self.view.as_dict() if self.view is not None else None,
        }


def parse_published(payload: Any) -> list[PolicyView]:
    """Read the publish payload into views, newest first.

    A malformed entry is skipped rather than aborting the list: the published
    file is written by another plugin, and one bad row must not take out the
    policies beside it.
    """
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("policies")
    if not isinstance(rows, list):
        return []
    views: list[PolicyView] = []
    for row in rows[:32]:
        if not isinstance(row, Mapping):
            continue
        policy_id = _text(row.get("policy_id"), 64)
        if not policy_id:
            continue
        source = row.get("source") if isinstance(row.get("source"), Mapping) else {}
        target = row.get("target") if isinstance(row.get("target"), Mapping) else {}
        raw_params = row.get("params") if isinstance(row.get("params"), Mapping) else {}
        params: dict[str, float] = {}
        rejected: list[str] = []
        for name, value in raw_params.items():
            if name not in ALLOWED_PARAMS:
                rejected.append(str(name)[:64])
                continue
            number = _number(value)
            if number is None:
                rejected.append(str(name)[:64])
                continue
            low, high = PARAM_RANGES[name]
            if not low <= number <= high:
                rejected.append(f"{name}={number:g}(超出 {low:g}~{high:g})"[:64])
                continue
            params[name] = number
        versions = target.get("validated_host_versions")
        contract = row.get("policy_contract_version")
        views.append(PolicyView(
            policy_id=policy_id,
            state=_text(row.get("state"), 32),
            params=params,
            rejected_params=tuple(rejected),
            contract_version=(contract if isinstance(contract, int)
                              and not isinstance(contract, bool) else None),
            trace_schema_version=(
                source.get("trace_schema_version")
                if isinstance(source.get("trace_schema_version"), int)
                and not isinstance(source.get("trace_schema_version"), bool) else None),
            learning_version=_text(source.get("learning_version"), 32),
            dataset_fingerprint=_text(source.get("dataset_fingerprint"), 64),
            chat_dynamics_version=_text(target.get("chat_dynamics_version"), 64),
            validated_host_versions=tuple(
                _text(item, 64) for item in versions if isinstance(item, str))[:16]
            if isinstance(versions, list) else (),
            baseline_config_hash=_text(target.get("baseline_config_hash"), 64),
            shadow_observed=bool(row.get("shadow_observed")),
        ))
    return views


def resolve(
    payload: Any,
    *,
    mode: str,
    host_version: str,
    effective_config: Mapping[str, float],
    expected_policy_id: str = "",
    expected_dataset_fingerprint: str = "",
    policy_id: str = "",
) -> Decision:
    """Decide what to apply. Never raises; every refusal names itself.

    `policy_id` selects a specific published policy; empty means the newest
    promoted one, which is what the producer already ordered the list by.
    """
    if mode not in MODES:
        mode = MODE_OFF
    if mode == MODE_OFF:
        return Decision(mode=mode, status=STATUS_OFF,
                        reasons=("学习策略消费端关闭：不读取发布文件。",))
    views = parse_published(payload)
    if not views:
        return Decision(mode=mode, status=STATUS_NO_POLICY,
                        reasons=("没有读到当前模式可读取的策略。",))
    view = next((item for item in views if item.policy_id == policy_id), None) \
        if policy_id else views[0]
    if view is None:
        return Decision(mode=mode, status=STATUS_NO_POLICY,
                        reasons=(f"发布的策略里没有 {policy_id}。",))

    reasons: list[str] = []
    allowed_states = {"validated", "shadow", "promoted"} if mode == MODE_SHADOW else {"promoted"}
    if view.state not in allowed_states:
        return Decision(mode=mode, status=STATUS_INCOMPATIBLE, policy_id=view.policy_id,
                        view=view, reasons=(f"策略状态 {view.state} 不允许用于 {mode}。",))
    if view.contract_version not in SUPPORTED_POLICY_CONTRACT_VERSIONS:
        return Decision(mode=mode, status=STATUS_INCOMPATIBLE, policy_id=view.policy_id,
                        view=view,
                        reasons=(f"发布协议版本 {view.contract_version} 不在本机支持的 "
                                 f"{list(SUPPORTED_POLICY_CONTRACT_VERSIONS)} 内。",))
    if expected_policy_id and expected_policy_id != view.policy_id:
        return Decision(mode=mode, status=STATUS_INCOMPATIBLE, policy_id=view.policy_id,
                        view=view,
                        reasons=(f"管理员锁定的是 {expected_policy_id}，发布的是 "
                                 f"{view.policy_id}。",))
    if (expected_dataset_fingerprint
            and expected_dataset_fingerprint != view.dataset_fingerprint):
        return Decision(mode=mode, status=STATUS_INCOMPATIBLE, policy_id=view.policy_id,
                        view=view,
                        reasons=(f"管理员锁定的数据指纹是 {expected_dataset_fingerprint}，"
                                 f"策略来自 {view.dataset_fingerprint}。",))
    if view.rejected_params:
        reasons.append("无法应用的参数：" + "、".join(view.rejected_params))
    if not view.params and not view.rejected_params:
        return Decision(mode=mode, status=STATUS_NO_POLICY, policy_id=view.policy_id,
                        view=view, reasons=tuple(reasons + ["策略没有可应用的参数。"]))

    version_ok = bool(host_version) and host_version in view.validated_host_versions
    unverifiable = not view.validated_host_versions
    if not version_ok:
        if unverifiable:
            reasons.append("策略没有记录验证过的本体版本，无法确认兼容性。")
        else:
            reasons.append(f"本体版本 {host_version or '未知'} 不在已验证列表 "
                           f"{list(view.validated_host_versions)} 中。")

    expected_hash = baseline_config_hash(effective_config)
    baseline_ok = bool(view.baseline_config_hash) and view.baseline_config_hash == expected_hash
    if not baseline_ok:
        reasons.append("基线配置摘要不匹配：策略是在另一份配置上校准的"
                       f"（策略 {view.baseline_config_hash or '未记录'}，"
                       f"本机 {expected_hash}）。")

    if mode == MODE_SHADOW:
        return Decision(mode=mode, status=STATUS_SHADOW, policy_id=view.policy_id,
                        applied=False, overrides=dict(view.params), view=view,
                        version_mismatch=not version_ok, baseline_mismatch=not baseline_ok,
                        unverifiable_version=unverifiable,
                        reasons=tuple(reasons + ["shadow 模式：只解析与记录，不应用。"]))
    if view.rejected_params:
        # A policy is a *validated set*. Applying the subset this consumer
        # happens to understand would apply a configuration nobody measured —
        # and the offline result describes the whole set, not the subset.
        # Shadow still resolves it, because observing a partial resolution is
        # useful as long as it is marked as partial.
        return Decision(mode=mode, status=STATUS_INCOMPATIBLE, policy_id=view.policy_id,
                        applied=False, overrides=dict(view.params), view=view,
                        version_mismatch=not version_ok, baseline_mismatch=not baseline_ok,
                        unverifiable_version=unverifiable,
                        reasons=tuple(reasons + ["存在本机无法应用的参数，拒绝部分应用。"]))
    if not version_ok or not baseline_ok:
        return Decision(mode=mode, status=STATUS_INCOMPATIBLE, policy_id=view.policy_id,
                        applied=False, overrides=dict(view.params), view=view,
                        version_mismatch=not version_ok, baseline_mismatch=not baseline_ok,
                        unverifiable_version=unverifiable,
                        reasons=tuple(reasons + ["active 模式要求版本与基线摘要都匹配，拒绝应用。"]))
    return Decision(mode=mode, status=STATUS_ACTIVE, policy_id=view.policy_id,
                    applied=True, overrides=dict(view.params), view=view,
                    reasons=tuple(reasons + [f"已应用策略 {view.policy_id}。"]))


# ---- shadow decisions ---------------------------------------------------
#
# Phase one of a shadow A/B run: the runtime keeps the baseline behaviour and,
# for every turn, the policy's decision is computed beside it and recorded. No
# behaviour changes; what changes is that the two decisions can be compared
# later against a human label.
#
# The comparison is only meaningful if it is exactly the decision the policy
# would have made, so it reproduces the admission rule rather than approximating
# it: structural turns are threshold-independent, a session with no prior bot
# message returns early, and only the ambient additive score is compared against
# the threshold.

# Codes that short-circuit the policy: the decision comes from the evidence, not
# from the score, so moving a threshold cannot change it. Mirrors
# `participation_policy.ParticipationPolicy.explicit`.
EXPLICIT_CODES = frozenset({
    "canonical_recipient", "bot_mention", "other_mention", "vocative", "bot_reply",
    "routed_bot", "routed_other", "bot_subject",
})
# The evidence a turn can carry and still be the host's early return.
AMBIENT_ONLY_CODES = frozenset({"ambient_baseline", "human_quote"})

REASON_STRUCTURAL = "structural"
REASON_EARLY_RETURN = "early_return"
REASON_AMBIENT = "ambient"


def shadow_decision(*, score: float, level: str, evidence_codes,
                    has_prior_bot: bool, baseline_threshold: float,
                    params: Mapping[str, float], policy_id: str,
                    now: float | None = None) -> dict[str, Any] | None:
    """What the policy would have decided, recorded beside what was decided.

    Returns `None` when the policy does not move the admission threshold, which
    is the honest answer: there is nothing to compare.
    """
    target = params.get("strong_addressivity_threshold")
    number = _number(target)
    if number is None:
        return None
    if abs(number - float(baseline_threshold)) < 1e-9:
        # The policy does not move the admission threshold, so there is no
        # comparison to record. Writing a row anyway would put a column of
        # always-equal decisions into the "same" count — rows that were never
        # compared, reported as agreement.
        return None
    codes = {str(code) for code in evidence_codes}
    clamped = max(0.0, min(1.0, float(score)))
    baseline_reply = level == "strong"
    if codes & EXPLICIT_CODES:
        # The evidence decided it, at either threshold.
        shadow_reply, reason = baseline_reply, REASON_STRUCTURAL
    elif not has_prior_bot:
        shadow_reply, reason = baseline_reply, REASON_EARLY_RETURN
    else:
        shadow_reply, reason = clamped >= number, REASON_AMBIENT
    return {
        "policy_id": policy_id,
        "baseline_threshold": round(float(baseline_threshold), 4),
        "shadow_threshold": round(number, 4),
        "baseline_reply": bool(baseline_reply),
        "shadow_reply": bool(shadow_reply),
        "changed": bool(baseline_reply) != bool(shadow_reply),
        "score": round(clamped, 6),
        "baseline_margin": round(clamped - float(baseline_threshold), 6),
        "shadow_margin": round(clamped - number, 6),
        "reason": reason,
        "recorded_at": float(now) if now is not None else 0.0,
    }


class LearningPolicyConsumer:
    """Caches the published policy and answers "what should this key be".

    Refresh is explicit and throttled by the caller: reading another plugin's
    shared preferences on every message would put an unrelated plugin's IO on
    the hot path of every group message.
    """

    def __init__(self, *, mode: str = MODE_OFF, source_id: str = LEARNING_SCOPE_ID,
                 expected_policy_id: str = "", expected_dataset_fingerprint: str = "",
                 host_version: str = "") -> None:
        self.mode = mode if mode in MODES else MODE_OFF
        self.source_id = source_id or LEARNING_SCOPE_ID
        self.expected_policy_id = expected_policy_id
        self.expected_dataset_fingerprint = expected_dataset_fingerprint
        self.host_version = host_version
        self.decision = Decision(mode=self.mode, status=STATUS_OFF)
        self.last_error = ""

    def configure(self, *, mode: str, expected_policy_id: str = "",
                  expected_dataset_fingerprint: str = "", host_version: str = "") -> None:
        self.expected_policy_id = expected_policy_id
        self.expected_dataset_fingerprint = expected_dataset_fingerprint
        if host_version:
            self.host_version = host_version
        if mode not in MODES:
            mode = MODE_OFF
        if mode != self.mode:
            self.mode = mode
            # A mode change invalidates the cached decision: leaving the old one
            # in place would keep applying a policy the operator just switched
            # away from, until the next refresh happened to run.
            self.decision = Decision(mode=mode, status=STATUS_OFF)

    def effective(self, name: str, configured: float) -> float:
        """The value to actually use for one parameter."""
        if not self.decision.applied:
            return configured
        value = self.decision.overrides.get(name)
        return configured if value is None else float(value)

    def overrides(self) -> dict[str, float]:
        return dict(self.decision.overrides) if self.decision.applied else {}

    def shadow_decision(self, *, score: float, level: str, evidence_codes,
                        has_prior_bot: bool, baseline_threshold: float,
                        now: float | None = None) -> dict[str, Any] | None:
        """Record what the policy would have decided, or `None` in any other mode.

        Only `shadow` records. In `active` the policy *is* the runtime, so the
        comparison would be against itself; in `off` there is no policy to
        compare with. Writing rows in either case would put a column of
        always-equal decisions into the disagreement statistics and make the
        subset look large and empty.
        """
        if self.mode != MODE_SHADOW or not self.decision.overrides:
            return None
        return shadow_decision(
            score=score, level=level, evidence_codes=evidence_codes,
            has_prior_bot=has_prior_bot, baseline_threshold=baseline_threshold,
            params=self.decision.overrides, policy_id=self.decision.policy_id, now=now)

    async def refresh(self, *, sp_module: Any = None, effective_config: Mapping[str, float],
                      mode: str | None = None) -> Decision:
        """Re-read the published policy. Never raises: a missing partner plugin
        (or a broken one) must not take this plugin down with it."""
        if mode is not None and mode in MODES:
            self.mode = mode
        if self.mode == MODE_OFF:
            self.decision = Decision(mode=MODE_OFF, status=STATUS_OFF)
            return self.decision
        module = sp_module
        if module is None:
            try:
                from astrbot.core import sp as module  # type: ignore[no-redef]
            except Exception:
                module = None
        payload: Any = None
        if module is not None and hasattr(module, "get_async"):
            try:
                payload = await module.get_async(scope="plugin", scope_id=self.source_id,
                                                 key=CANDIDATE_KEY if self.mode == MODE_SHADOW else PUBLISHED_KEY,
                                                 default=None)
            except Exception as exc:
                self.last_error = type(exc).__name__
                payload = None
        self.decision = resolve(
            payload, mode=self.mode, host_version=self.host_version,
            effective_config=effective_config,
            expected_policy_id=self.expected_policy_id,
            expected_dataset_fingerprint=self.expected_dataset_fingerprint)
        return self.decision


__all__ = [
    "ALLOWED_PARAMS", "AMBIENT_ONLY_CODES", "EXPLICIT_CODES", "MODE_ACTIVE", "MODE_OFF",
    "MODE_SHADOW", "MODES", "PARAM_RANGES", "REASON_AMBIENT", "REASON_EARLY_RETURN",
    "REASON_STRUCTURAL", "shadow_decision",
    "CANDIDATE_KEY", "PUBLISHED_KEY", "SUPPORTED_POLICY_CONTRACT_VERSIONS", "STATUS_ACTIVE",
    "STATUS_INCOMPATIBLE", "STATUS_NO_POLICY", "STATUS_OFF", "STATUS_SHADOW", "Decision",
    "LearningPolicyConsumer", "PolicyView", "baseline_config_hash", "parse_published",
    "resolve",
]
