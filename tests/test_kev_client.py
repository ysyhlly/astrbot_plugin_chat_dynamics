"""Kev shadow contract: exact evidence, task semantics, and model identity."""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.decision_state import (
    build_decision_snapshot, snapshot_json,
)
from astrbot_plugin_chat_dynamics.core.integrations.kev import (
    KevClient, validate_kev_answers,
)


STATE = {
    "bot_speaker_id": "bot",
    "persona": "安安话少；明确被问到时可以简短帮忙。",
    "environment": {"schema_version": 1, "messages_30s": 4,
                    "seconds_since_bot_reply": None},
    "target_candidates": {
        "target.0": {"message_id": "m1", "author": "u1",
                     "text": "@安安 看看这个问题", "text_missing": False},
        "target.1": {"message_id": "m2", "author": "u1",
                     "text": "还有这个", "text_missing": False},
    },
    "conversation": {
        "background": [],
        "messages": [
            {"message_id": "m1", "timestamp": 100, "author": "u1",
             "text": "@安安 看看这个问题", "reply_to": None,
             "mentioned_users": ["bot"], "semantics": {}},
            {"message_id": "m2", "timestamp": 101, "author": "u1",
             "text": "还有这个", "reply_to": "m1",
             "mentioned_users": [], "semantics": {"quoted_author_id": "u1"}},
        ],
    },
}
QUESTIONS = {
    "join": {"type": "noul", "instructions": "本轮是否参与？"},
    "action": {"type": "choice", "instructions": "采取什么动作？",
               "criteria": {"ignore": "不参与", "reply": "回复"}},
    "reply_length": {"type": "choice", "instructions": "回复长度？",
                     "criteria": {key: key for key in
                                  ("tiny", "short", "medium", "long", "very_long")}},
    "target.0": {"type": "noul", "instructions": "回应 m1？"},
    "target.1": {"type": "noul", "instructions": "回应 m2？"},
}
ENVELOPE = {
    "model": "kev-latest",
    "answers": {
        "join": {"type": "noul", "noul": .9},
        "action": {"type": "choice", "choice": "reply", "confidence": .8,
                   "probabilities": {"ignore": .1, "reply": .9}},
        "reply_length": {"type": "choice", "choice": "short", "confidence": .6,
                         "probabilities": {"tiny": .1, "short": .5, "medium": .2,
                                           "long": .1, "very_long": .1}},
        "target.0": {"type": "noul", "noul": .8},
        "target.1": {"type": "noul", "noul": .7},
    },
}


def test_snapshot_keeps_persona_environment_and_addressivity():
    snapshot = build_decision_snapshot(STATE, QUESTIONS)
    assert snapshot["schema_version"] == "chat_decision_v1"
    assert snapshot["persona"] == STATE["persona"]
    assert snapshot["environment"]["seconds_since_bot_reply"] is None
    assert snapshot["bot_speaker_id"] == "bot"
    assert snapshot["chat"][0]["mentions"] == ["bot"]
    assert snapshot["chat"][1]["reply_to"] == "m1"
    assert snapshot["target_candidates"]["target.1"]["text"] == "还有这个"
    assert snapshot_json(snapshot) == snapshot_json(build_decision_snapshot(STATE, QUESTIONS))


def test_snapshot_refuses_missing_evidence():
    state = copy.deepcopy(STATE)
    del state["environment"]
    assert build_decision_snapshot(state, QUESTIONS) is None
    state = copy.deepcopy(STATE)
    state["conversation"]["messages"][0]["text_missing"] = True
    assert build_decision_snapshot(state, QUESTIONS) is None


def test_kev_keeps_independent_targets_and_five_lengths():
    answers = validate_kev_answers(ENVELOPE, QUESTIONS)
    assert answers["target.0"]["noul"] == .8
    assert answers["target.1"]["noul"] == .7
    assert set(answers["reply_length"]["probabilities"]) == {
        "tiny", "short", "medium", "long", "very_long"}


@pytest.mark.parametrize("change", [
    lambda value: value["answers"].pop("target.1"),
    lambda value: value["answers"]["target.0"].update(noul=float("nan")),
    lambda value: value["answers"]["action"]["probabilities"].update(reply=.4),
    lambda value: value["answers"]["reply_length"]["probabilities"].pop("very_long"),
])
def test_kev_rejects_partial_or_invalid_answers(change):
    envelope = copy.deepcopy(ENVELOPE)
    change(envelope)
    with pytest.raises(ValueError):
        validate_kev_answers(envelope, QUESTIONS)


def test_client_verifies_actual_run_before_predicting():
    client = KevClient(enabled=True, base_url="http://127.0.0.1:18766",
                       checkpoint_id="pinned/kev-0.8b")
    client.transport.request_json = AsyncMock(side_effect=[
        {"models": [{"name": "kev-latest", "run": "pinned/kev-0.8b",
                     "device": "cuda:0", "dtype": "bf16", "strict_context": True}]},
        {**ENVELOPE, "latency_ms": 81.5, "usage": {"input_tokens": 123}},
    ])
    diagnostics = {}
    result = asyncio.run(client.evaluate(state=STATE, questions=QUESTIONS,
                                         diagnostics=diagnostics))
    assert result["answers"]["target.1"]["noul"] == .7
    assert diagnostics["server_model_ms"] == 81.5
    assert diagnostics["input_tokens"] == 123
    assert diagnostics["identity_check_ms"] >= 0
    assert client.transport.request_json.call_args_list[0].args[0] == "/v1/models"
    assert client.transport.request_json.call_args_list[1].args[0] == "/v1/systemone"
    assert client.transport.request_json.call_args_list[1].args[1]["state"]["environment"] == STATE["environment"]
    assert client.snapshot()["available"] is True
    assert client.snapshot()["status"] == "available"
    asyncio.run(client.close())


def test_client_rechecks_identity_when_server_changes_run_between_turns():
    client = KevClient(enabled=True, base_url="http://127.0.0.1:18766",
                       checkpoint_id="pinned/kev-0.8b")
    client.transport.request_json = AsyncMock(side_effect=[
        {"models": [{"name": "kev-latest", "run": "pinned/kev-0.8b",
                     "device": "cuda:0", "strict_context": True}]},
        ENVELOPE,
        {"models": [{"name": "kev-latest", "run": "replacement-run",
                     "device": "cuda:0", "strict_context": True}]},
    ])

    async def evaluate_twice():
        first = await client.evaluate(state=STATE, questions=QUESTIONS)
        second = await client.evaluate(state=STATE, questions=QUESTIONS)
        return first, second

    first, second = asyncio.run(evaluate_twice())
    assert first is not None and second is None
    assert client.snapshot()["detail"] == "checkpoint_mismatch"
    assert client.snapshot()["available"] is False
    assert [call.args[0] for call in client.transport.request_json.call_args_list] == [
        "/v1/models", "/v1/systemone", "/v1/models",
    ]
    asyncio.run(client.close())


def test_client_refuses_checkpoint_mismatch():
    client = KevClient(enabled=True, base_url="http://127.0.0.1:18766",
                       checkpoint_id="pinned/kev-0.8b")
    client.transport.request_json = AsyncMock(return_value={
        "models": [{"name": "kev-latest", "run": "different-run",
                    "device": "cuda:0"}]})
    assert asyncio.run(client.evaluate(state=STATE, questions=QUESTIONS)) is None
    assert client.transport.request_json.await_count == 1
    asyncio.run(client.close())


def test_client_refuses_a_server_that_can_silently_truncate_state():
    client = KevClient(enabled=True, base_url="http://127.0.0.1:18766",
                       checkpoint_id="pinned/kev-0.8b")
    client.transport.request_json = AsyncMock(return_value={
        "models": [{"name": "kev-latest", "run": "pinned/kev-0.8b",
                    "device": "cuda:0"}]})
    assert asyncio.run(client.evaluate(state=STATE, questions=QUESTIONS)) is None
    assert client.transport.request_json.await_count == 1
    asyncio.run(client.close())
