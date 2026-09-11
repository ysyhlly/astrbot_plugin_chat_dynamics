"""Golden behavior captured before extracting the participation policy."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG


def legacy_case(name):
    dag = ConversationDAG()
    trigger = dag.add_message("trigger", "alice", "start", timestamp=1)
    bot = dag.add_message("botmsg", "bot", "alpha", timestamp=10)
    node = dag.add_message("candidate", "alice", "beta", timestamp=20)
    bot.thread_id, node.thread_id = "bot-thread", "candidate-thread"
    node.parent_ids.clear()
    kwargs = {"last_bot_node": bot, "semantic_match_fn": lambda *_: SimpleNamespace(score=.17, embedding_cosine=.1)}
    if name == "mention":
        node.mentioned_users = ["bot"]
    elif name == "other_mention":
        node.mentioned_users = ["carol"]
    elif name == "vocative":
        node.text = "bot, help"
    elif name == "reply_bot":
        node.reply_to_id = bot.msg_id
    elif name.startswith("canonical"):
        node.metadata["routing"] = {"routing_schema_version": 2, "addressee_ids": ["bot"],
            "bot_addressee_confidence": .81, "addressee_confidence": .82, "addressee_ambiguous": False, "topic_confidence": .63}
        if name == "canonical_human":
            node.metadata["routing"]["addressee_ids"] = ["carol"]
            node.mentioned_users = ["bot"]
        if name == "canonical_topic_ambiguous":
            node.metadata["routing"]["topic_ambiguous"] = True
        if name == "canonical_ambiguous":
            node.metadata["routing"]["addressee_ambiguous"] = True
    elif name == "legacy_bot":
        node.metadata["routing"] = {"bot_is_addressee": True, "bot_addressee_confidence": .91, "addressee_ambiguous": False}
    elif name == "legacy_human":
        node.metadata["routing"] = {"addressee_ids": ["carol"], "addressee_confidence": .91, "addressee_ambiguous": False}
    elif name == "subject":
        node.metadata["routing"] = {"subject_is_bot": True}
    elif name in {"no_bot", "no_bot_quote"}:
        kwargs["last_bot_node"] = None
        if name.endswith("quote"):
            node.reply_to_id = trigger.msg_id
    elif name.startswith("time_"):
        node.timestamp = bot.timestamp + float(name.split("_")[1])
    elif name == "human_quote":
        node.reply_to_id = trigger.msg_id
    elif name == "wake":
        node.metadata["is_wake"] = True
    elif name == "divergence":
        for i in range(3):
            dag.add_message(f"intervening{i}", "other", "chatter", timestamp=11+i)
    elif name == "cue":
        node.text = "细说"
    elif name == "overlap":
        node.text = "alpha beta"
    elif name == "minor_overlap":
        bot.text, node.text = "alpha one two three four", "alpha five six seven eight"
    elif name == "embedding":
        bot.text, node.text = "", ""
        kwargs["semantic_match_fn"] = lambda *_: SimpleNamespace(score=.8, embedding_cosine=.8)
    elif name == "routing_bonus":
        node.metadata["routing"] = {"bot_addressee_confidence": .51, "topic_confidence": .7}
    elif name == "active":
        bot.reply_to_id = trigger.msg_id
        node.metadata["routing"] = {"topic_id": "topic"}
        bot.metadata["routing"] = {"topic_id": "topic"}
    elif name == "thread":
        node.thread_id = bot.thread_id
    elif name in {"hover", "hover_cap"}:
        hovers = [dag.add_message(f"hover{i}", "alice", "beta", timestamp=12+i) for i in range(3 if name == "hover_cap" else 1)]
        kwargs["prior_hovers"] = hovers
    return AddressivityRouter(bot_id="bot", bot_names=["bot"]), node, dag, kwargs


CASES = ["mention", "other_mention", "vocative", "reply_bot", "canonical_bot", "canonical_human",
         "canonical_topic_ambiguous", "canonical_ambiguous", "legacy_bot", "legacy_human", "subject",
         "no_bot", "no_bot_quote", "time_14.9", "time_15", "time_45", "time_120", "human_quote",
         "wake", "divergence", "cue", "overlap", "minor_overlap", "embedding", "routing_bonus", "active", "thread", "hover", "hover_cap"]


def public_result(result):
    return {"score": result.score, "level": result.level.value, "is_bot_targeted": result.is_bot_targeted,
            "target_user_id": result.target_user_id, "reasons": result.reasons, "topic_relevance": result.topic_relevance}


@pytest.mark.parametrize("name", CASES)
def test_legacy_golden(name):
    expected = json.loads((Path(__file__).parent / "fixtures" / "participation_legacy.json").read_text(encoding="utf-8"))
    router, node, dag, kwargs = legacy_case(name)
    assert public_result(router.compute_addressivity(node, dag, **kwargs)) == expected[name]


@pytest.mark.parametrize("name", CASES)
def test_production_evidence_accounts_for_score(name):
    router, node, dag, kwargs = legacy_case(name)
    result = router.compute_addressivity(node, dag, **kwargs)
    assert result.evidence
    assert round(sum(item.strength for item in result.evidence), 4) == result.contribution_total
    assert round(sum(result.family_contributions.values()), 4) == result.contribution_total
    assert result.score == round(max(0, min(1, result.contribution_total)), 4)


def test_pure_policy_snapshots_are_immutable_and_preserve_existing_hover_cap():
    from dataclasses import FrozenInstanceError
    from astrbot_plugin_chat_dynamics.core.participation_policy import (
        Evidence, ParticipationPolicy, ParticipationSnapshot, RecipientSnapshot,
    )
    snapshot = ParticipationSnapshot("bot", RecipientSnapshot(bot_confidence=.5),
        observations=(Evidence("temporal_gap", "temporal", 10, "message.timestamp"),
            Evidence("intervening_messages", "dialogue", 0, "dag.recent"),
            Evidence("platform_wake", "platform", 1, "message.is_wake"),
            Evidence("continuation_cue", "dialogue", 1, "continuation_matcher"),
            Evidence("active_interlocutor", "dialogue", 1, "dag.dialogue"),
            Evidence("explicit_thread", "dialogue", 1, "dag.thread"),
            Evidence("pending_hover", "dialogue", 1, "pending_hover"),
            Evidence("pending_hover", "dialogue", 1, "pending_hover")),
        reason_details=(("continuation_cue", "Contains continuation cue '细说'"),), has_prior_bot=True)
    with pytest.raises(FrozenInstanceError):
        snapshot.recipient.confidence = .9
    with pytest.raises(FrozenInstanceError):
        snapshot.observations[0].strength = 99
    result = ParticipationPolicy().evaluate(snapshot)
    assert result.score == 1.0 and result.contribution_total > 1.0
    assert next(item.strength for item in result.evidence if item.code == "pending_hover") == .28
    assert dict(result.family_contributions)["dialogue"] == .68  # No new per-family cap.
    assert ParticipationPolicy().evaluate(snapshot) == result


def test_policy_canonical_recipient_overrides_conflicting_legacy_observations():
    from astrbot_plugin_chat_dynamics.core.participation_policy import (
        Evidence, ParticipationPolicy, ParticipationSnapshot, RecipientSnapshot,
    )
    snapshot = ParticipationSnapshot("bot", RecipientSnapshot(ids=("human",), canonical=True, ambiguous=False),
        observations=(Evidence("bot_mention", "recipient", 1, "message.mentions"),),
        reason_details=(("bot_mention", "Explicit @mention of bot (bot)"),))
    result = ParticipationPolicy().evaluate(snapshot)
    assert result.level == "weak" and result.target_user_id == "human"
    assert result.evidence[0].code == "canonical_recipient"


def test_missing_reason_detail_falls_back_to_the_code():
    from astrbot_plugin_chat_dynamics.core.participation_policy import (
        Evidence, ParticipationPolicy, ParticipationSnapshot, RecipientSnapshot,
    )
    policy = ParticipationPolicy()
    # A caller-supplied fact without a human-readable detail must not raise, and
    # an other-mention without its target cannot claim a recipient.
    bare = ParticipationSnapshot("bot", RecipientSnapshot(),
        observations=(Evidence("vocative", "recipient", 1, "identity_matcher"),))
    decision = policy.explicit(bare)
    assert decision.reasons == ("vocative",) and decision.is_bot_targeted
    untargeted = ParticipationSnapshot("bot", RecipientSnapshot(),
        observations=(Evidence("other_mention", "recipient", 1, "message.mentions"),))
    assert policy.explicit(untargeted) is None


def test_trace_exports_safe_evidence_and_family_totals():
    from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace
    router, node, dag, kwargs = legacy_case("routing_bonus")
    result = router.compute_addressivity(node, dag, **kwargs)
    part = dict(vars(result))
    part["evidence"] = [*result.evidence,
        {"code": "human_quote", "family": "recipient", "strength": -.15, "source": "private message"},
        {"code": "private message", "family": "recipient", "strength": 1, "source": "policy"}]
    part["family_contributions"] = {**result.family_contributions, "private message": 99}
    part["text"] = "private message"
    trace = build_routing_trace(routing={}, participation=part,
        state={"waiting_for_answer": None, "last_bot_was_question": True, "last_bot_message_id": "b", "text": "private message"})
    diagnostics = trace["participation"]
    assert len(diagnostics["evidence"]) == len(result.evidence)
    assert diagnostics["family_contributions"] == result.family_contributions
    assert diagnostics["contribution_total"] == result.contribution_total
    assert trace["state"]["waiting_for_answer"] is None
    assert trace["state"]["last_bot_was_question"] is True
    assert trace["state"]["last_bot_message_id"] == "b"
    assert "private message" not in json.dumps(trace)
    diagnostics["family_contributions"]["recipient"] = 999
    assert result.family_contributions["recipient"] != 999
