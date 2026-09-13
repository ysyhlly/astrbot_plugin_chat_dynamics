"""The policy consumer: three modes, three checks, and what each one refuses.

The consumer is the only place in this plugin that knows Dynamics Learning
exists, so these tests are the whole contract between the two repositories
viewed from this side. They are written around the refusals rather than the
happy path: a consumer that applies what it is handed is not a consumer.
"""
from __future__ import annotations

import json

import pytest

from astrbot_plugin_chat_dynamics.core import learning_policy as lp
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config

BASE = {
    "strong_addressivity_threshold": 0.70,
    "safe_hover_threshold": 0.40,
    "topic_commit_threshold": 0.58,
    "topic_join_threshold": 0.48,
    "topic_margin_threshold": 0.06,
    "parent_accept_threshold": 0.72,
}


def publish(*, policy_id="policy_v3", state="promoted", params=None,
            host_version="v1.6.2", validated=None, baseline=None,
            contract=1, dataset="9f2c1a", trace_schema=3, shadow=False):
    """A payload shaped exactly like Dynamics Learning publishes."""
    resolved = dict(BASE)
    resolved.update(params or {"strong_addressivity_threshold": 0.67})
    # The published digest is of the **baseline** the policy assumed, not of the
    # parameters it proposes — otherwise it would always match itself.
    return {
        "policy_contract_version": 1,
        "generated_at": 1.0,
        "policies": [{
            "policy_contract_version": contract,
            "policy_id": policy_id,
            "issued_at": 1.0,
            "state": state,
            "source": {"trace_schema_version": trace_schema,
                       "trace_schema_versions": {"3": 100},
                       "dataset_fingerprint": dataset,
                       "learning_version": "0.9.0"},
            "target": {"chat_dynamics_version": host_version,
                       "baseline_config_hash": (lp.baseline_config_hash(BASE)
                                                if baseline is None else baseline),
                       "validated_host_versions": ([host_version] if validated is None
                                                   else validated)},
            "params": resolved,
            "baseline": dict(BASE),
            "changed": ["strong_addressivity_threshold"],
            "target_error": "missed_bot",
            "confidence": "moderate",
            "shadow_observed": shadow,
            "evidence": {},
        }],
        "note": "…",
    }


def decide(payload, *, mode="active", host_version="v1.6.2", config=None, **kwargs):
    return lp.resolve(payload, mode=mode, host_version=host_version,
                      effective_config=config or BASE, **kwargs)


# ---- modes --------------------------------------------------------------

def test_off_is_the_default_and_reads_nothing():
    decision = decide(None, mode="off")
    assert decision.status == lp.STATUS_OFF
    assert decision.applied is False

    cfg, _ = parse_runtime_config({})
    assert cfg.learning_policy_mode == "off", "默认必须是 off"
    consumer = lp.LearningPolicyConsumer()
    assert consumer.decision.applied is False


def test_an_unknown_mode_falls_back_to_off_rather_than_to_active():
    assert decide(publish(), mode="ACTIVE").status == lp.STATUS_OFF
    cfg, warnings = parse_runtime_config({"learning_policy_mode": "yolo"})
    assert cfg.learning_policy_mode == "off"
    assert any("learning_policy_mode" in warning for warning in warnings)


def test_no_published_policy_is_its_own_status():
    assert decide({"policy_contract_version": 1, "policies": []}).status == lp.STATUS_NO_POLICY
    assert decide(None).status == lp.STATUS_NO_POLICY


# ---- the happy path -----------------------------------------------------

def test_active_applies_a_fully_compatible_policy():
    decision = decide(publish())

    assert decision.status == lp.STATUS_ACTIVE
    assert decision.applied is True
    assert decision.policy_id == "policy_v3"
    assert decision.overrides["strong_addressivity_threshold"] == pytest.approx(0.67)
    assert decision.version_mismatch is False
    assert decision.baseline_mismatch is False


def test_the_canonical_baseline_hash_is_byte_identical_to_the_producer_side():
    """The digest is the cross-repo half of "same baseline"; it cannot drift."""
    import hashlib

    canonical = json.dumps({key: f"{value:.4f}" for key, value in BASE.items()},
                           sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    assert lp.baseline_config_hash(BASE) == expected
    assert lp.baseline_config_hash(dict(reversed(list(BASE.items())))) == expected


# ---- the three checks ---------------------------------------------------

def test_a_version_outside_the_validated_list_blocks_active():
    decision = decide(publish(validated=["v1.6.1"]))

    assert decision.status == lp.STATUS_INCOMPATIBLE
    assert decision.applied is False
    assert decision.version_mismatch is True
    assert any("v1.6.2" in reason for reason in decision.reasons)


def test_an_empty_validated_list_is_unverifiable_not_matching():
    decision = decide(publish(validated=[]))

    assert decision.applied is False
    assert decision.unverifiable_version is True
    assert any("无法确认兼容性" in reason for reason in decision.reasons)


def test_shadow_allows_both_mismatches_but_still_applies_nothing():
    decision = decide(publish(validated=["v1.7.0"], baseline="deadbeef"), mode="shadow")

    assert decision.status == lp.STATUS_SHADOW
    assert decision.applied is False, "shadow 绝不实际应用"
    assert decision.version_mismatch is True
    assert decision.baseline_mismatch is True
    # …but it still resolves what it *would* apply, which is the whole point of
    # collecting disagreement data before trusting the policy.
    assert decision.overrides["strong_addressivity_threshold"] == pytest.approx(0.67)


def test_a_changed_baseline_blocks_active():
    decision = decide(publish(baseline="0000000000000000"))

    assert decision.status == lp.STATUS_INCOMPATIBLE
    assert decision.baseline_mismatch is True
    assert any("基线配置摘要不匹配" in reason for reason in decision.reasons)


def test_a_newer_publish_protocol_is_refused_rather_than_half_read():
    decision = decide(publish(contract=2))

    assert decision.status == lp.STATUS_INCOMPATIBLE
    assert decision.applied is False


def test_only_a_promoted_policy_is_eligible():
    for state in ("proposed", "validated", "shadow", "rolled_back"):
        decision = decide(publish(state=state))
        assert decision.status == lp.STATUS_INCOMPATIBLE
        assert decision.applied is False


# ---- pins and the whitelist ---------------------------------------------

def test_the_admin_lock_is_the_policy_id_not_the_dataset_fingerprint():
    mismatch = decide(publish(policy_id="policy_v9"), expected_policy_id="policy_v3")

    assert mismatch.status == lp.STATUS_INCOMPATIBLE
    assert any("policy_v3" in reason for reason in mismatch.reasons)

    schema_drift = decide(publish(dataset="other"), expected_dataset_fingerprint="wanted")
    assert schema_drift.status == lp.STATUS_INCOMPATIBLE

    # The fingerprint is provenance: unconfigured means unpinned, not rejected.
    assert decide(publish(dataset="whatever")).status == lp.STATUS_ACTIVE


def test_an_unlisted_parameter_makes_the_set_unapplicable():
    """A partial application is an unvalidated configuration, so it is refused."""
    payload = publish(params={"strong_addressivity_threshold": 0.67, "nuke_everything": 1})
    decision = decide(payload)

    assert decision.applied is False
    assert decision.status == lp.STATUS_INCOMPATIBLE
    assert "nuke_everything" in decision.view.rejected_params
    assert "nuke_everything" not in decision.overrides
    # Shadow resolves the subset it *could* apply, marked as partial.
    shadow = decide(payload, mode="shadow")
    assert shadow.status == lp.STATUS_SHADOW
    assert shadow.overrides["strong_addressivity_threshold"] == pytest.approx(0.67)


def test_an_out_of_range_value_is_refused_rather_than_clamped():
    """A clamped 0.70 and a published 0.96 are different policies."""
    decision = decide(publish(params={"strong_addressivity_threshold": 0.96,
                                      "safe_hover_threshold": 0.40}))

    assert decision.applied is False
    assert decision.status == lp.STATUS_INCOMPATIBLE
    assert any("超出" in item for item in decision.view.rejected_params)


def test_a_malformed_row_does_not_take_out_the_policies_beside_it():
    payload = publish()
    payload["policies"].insert(0, "not a mapping")
    payload["policies"].insert(1, {"policy_id": ""})

    views = lp.parse_published(payload)

    assert [view.policy_id for view in views] == ["policy_v3"]


def test_a_specific_policy_can_be_selected_by_id():
    payload = publish()
    # The producer orders the list newest first, so that is how a newer policy
    # arrives here too.
    payload["policies"].insert(0, {**payload["policies"][0], "policy_id": "policy_v4"})

    assert decide(payload, policy_id="policy_v4").policy_id == "policy_v4"
    assert decide(payload, policy_id="policy_v99").status == lp.STATUS_NO_POLICY
    assert decide(payload).policy_id == "policy_v4", "默认取最新的 promoted"


# ---- the consumer object ------------------------------------------------

@pytest.mark.asyncio
async def test_the_consumer_never_raises_when_the_partner_is_missing():
    class Broken:
        @staticmethod
        async def get_async(**_kwargs):
            raise RuntimeError("no such scope")

    consumer = lp.LearningPolicyConsumer(mode="active", host_version="v1.6.2")
    decision = await consumer.refresh(sp_module=Broken(), effective_config=BASE)

    assert decision.status == lp.STATUS_NO_POLICY
    assert consumer.last_error == "RuntimeError"


@pytest.mark.asyncio
async def test_the_consumer_reads_the_agreed_key_from_the_agreed_scope():
    seen = {}

    class Sp:
        @staticmethod
        async def get_async(**kwargs):
            seen.update(kwargs)
            return publish()

    consumer = lp.LearningPolicyConsumer(mode="active", host_version="v1.6.2")
    decision = await consumer.refresh(sp_module=Sp(), effective_config=BASE)

    assert seen["scope"] == "plugin"
    assert seen["scope_id"] == lp.LEARNING_SCOPE_ID
    assert seen["key"] == lp.PUBLISHED_KEY
    assert decision.applied is True


def test_effective_returns_the_configured_value_unless_applied():
    consumer = lp.LearningPolicyConsumer(mode="shadow", host_version="v1.6.2")
    consumer.decision = decide(publish(), mode="shadow")

    assert consumer.effective("strong_addressivity_threshold", 0.70) == pytest.approx(0.70)
    assert consumer.overrides() == {}

    active = lp.LearningPolicyConsumer(mode="active", host_version="v1.6.2")
    active.decision = decide(publish())
    assert active.effective("strong_addressivity_threshold", 0.70) == pytest.approx(0.67)
    assert active.effective("safe_hover_threshold", 0.40) == pytest.approx(0.40)


# ---- shadow decisions ---------------------------------------------------

def shadow_of(*, mode="shadow", evidence=("ambient_baseline",), score=0.68,
              level="hover", has_prior_bot=True, payload=None, baseline_threshold=0.70):
    consumer = lp.LearningPolicyConsumer(mode=mode, host_version="v1.6.2")
    consumer.decision = decide(payload if payload is not None else publish(), mode=mode)
    return consumer.shadow_decision(
        score=score, level=level, evidence_codes=evidence,
        has_prior_bot=has_prior_bot, baseline_threshold=baseline_threshold)


def test_a_shadow_decision_reproduces_the_admission_rule_exactly():
    """The comparison is only worth recording if it is the decision the policy
    would have made, not an approximation of it."""
    above = shadow_of(score=0.68, baseline_threshold=0.70)
    below = shadow_of(score=0.66, baseline_threshold=0.70)

    assert above["baseline_reply"] is False, "0.68 低于基线阈值"
    assert above["shadow_reply"] is True, "0.68 高于策略阈值 0.67"
    assert above["changed"] is True
    assert above["reason"] == "ambient"
    assert below["shadow_reply"] is False, "0.66 连策略阈值也没到"
    assert below["changed"] is False
    assert above["baseline_margin"] == pytest.approx(-0.02)
    assert above["shadow_margin"] == pytest.approx(0.01)


def test_a_structural_turn_is_the_same_decision_at_any_threshold():
    decision = shadow_of(evidence=("bot_mention", "ambient_baseline"), level="strong",
                         score=0.9)

    assert decision["changed"] is False
    assert decision["reason"] == "structural"
    assert decision["baseline_reply"] is True and decision["shadow_reply"] is True


def test_a_turn_with_no_prior_bot_message_returns_early_for_both():
    decision = shadow_of(evidence=("ambient_baseline",), level="weak", score=0.2,
                         has_prior_bot=False)

    assert decision["changed"] is False
    assert decision["reason"] == "early_return"


def test_shadow_records_nothing_outside_shadow_mode():
    """active would compare the policy with itself; off has nothing to compare."""
    assert shadow_of(mode="active") is None
    assert shadow_of(mode="off") is None


def test_shadow_records_nothing_when_the_policy_does_not_move_the_threshold():
    payload = publish(params={"topic_commit_threshold": 0.62})

    assert shadow_of(payload=payload) is None


def test_the_shadow_block_is_narrowed_and_carries_no_identifiers():
    from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace

    trace = build_routing_trace(routing={"topic_id": "t1"}, shadow=shadow_of())

    assert trace["shadow"]["policy_id"] == "policy_v3"
    assert trace["shadow"]["changed"] is True
    assert set(trace["shadow"]) == {
        "policy_id", "baseline_threshold", "shadow_threshold", "baseline_reply",
        "shadow_reply", "changed", "reason", "score", "baseline_margin", "shadow_margin"}


def test_a_trace_without_a_shadow_decision_has_no_shadow_block():
    from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace

    assert "shadow" not in build_routing_trace(routing={})


def test_switching_the_mode_away_from_active_drops_the_cached_decision():
    consumer = lp.LearningPolicyConsumer(mode="active", host_version="v1.6.2")
    consumer.decision = decide(publish())
    assert consumer.decision.applied is True

    consumer.configure(mode="off")

    assert consumer.decision.applied is False
    assert consumer.effective("strong_addressivity_threshold", 0.70) == pytest.approx(0.70)
