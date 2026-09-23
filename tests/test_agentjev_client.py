"""AgentJev's public wire contract and safe decision-learning fallback."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.integrations.agentjev import (
    AgentJevClient, build_request, parse_response,
)
from astrbot_plugin_chat_dynamics.core.agentjev_state import MAX_PATH_BYTES
from astrbot_plugin_chat_dynamics.core.decision_learning import DecisionLearning


STATE = {
    "conversation": {"text": "请解释一下", "author": "u1", "messages": [
        {"message_id": "m1", "author": "u1", "text": "请解释一下"}]},
    "target_candidates": {"target.0": {"message_id": "m1", "author": "u1",
                                        "text": "请解释一下", "text_missing": False}},
}
QUESTIONS = {
    "join": {"type": "noul", "instructions": "是否参与？"},
    "recipient_choice": {"type": "choice", "instructions": "主要对谁？",
                         "criteria": {"u1": "群友 u1", "none": "全群"}},
    "action": {"type": "choice", "instructions": "如何做？",
               "criteria": {"reply": "回复", "ignore": "忽略"}},
    "reply_length": {"type": "choice", "instructions": "回多长？",
                     "criteria": {"short": "简短", "long": "详细"}},
    "target.0": {"type": "noul", "instructions": "Should the response address message m1?"},
}


def response():
    return {"api_version": "agentjev.decision.v1", "model": "fine-tuned",
            "results": [{"id": "0", "answers": [
                {"id": "target", "type": "choice", "value": "m1",
                 "distribution": {"m1": .92, "none": .08}},
                {"id": "join", "type": "choice", "value": "true",
                 "distribution": {"false": .03, "true": .97}},
                {"id": "recipient", "type": "choice", "value": "u1",
                 "distribution": {"u1": .85, "none": .15}},
                {"id": "action", "type": "choice", "value": "reply",
                 "distribution": {"reply": .9, "ignore": .1}},
                {"id": "reply_length", "type": "choice", "value": "short",
                 "distribution": {"short": .8, "long": .2}},
            ]}]}


def test_groups_whole_case_and_maps_typed_answers():
    payload, target_map = build_request(STATE, QUESTIONS)
    assert {q["id"] for q in payload["questions"]} == {
        "join", "recipient", "target", "action", "reply_length"}
    assert target_map == {"m1": "target.0"}
    assert "请解释一下" in payload["questions"][0]["options"]["m1"]
    assert payload["questions"][0]["options"]["m1"].endswith("]")
    answers = parse_response(response(), QUESTIONS, target_map)
    assert answers["join"]["noul"] == .97
    assert answers["target.0"]["noul"] == .92
    assert answers["recipient_choice"]["choice"] == "u1"
    assert answers["reply_length"]["choice"] == "short"


def test_production_instruction_shape_and_addressivity_are_preserved():
    state = copy.deepcopy(STATE)
    questions = copy.deepcopy(QUESTIONS)
    questions["join"]["instructions"] = {"question": "是否参与？", "focus": "检查回复关系"}
    state["conversation"]["messages"][0]["semantics"] = {"bot_is_addressee": True}
    first, _ = build_request(state, questions)
    assert "检查回复关系" in next(q["question"] for q in first["questions"] if q["id"] == "join")
    assert '"bot_is_addressee":true' in first["state"]
    state["conversation"]["messages"][0]["semantics"]["bot_is_addressee"] = False
    second, _ = build_request(state, questions)
    assert first["state"] != second["state"]


def test_real_turn_questions_reach_agentjev_wire_contract():
    from astrbot_plugin_chat_dynamics.core.decision_tasks import turn_questions
    from astrbot_plugin_chat_dynamics.core.jev_decision import build_learning_state
    from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext

    messages = tuple(MessageSnapshot(f"m{index}", "user", "这段对话需要看看具体的错误原因")
                     for index in range(8))
    turn = TurnContext("room", "user", "帮我看下", messages, (), 0, 0, 0, True)
    questions, candidates = turn_questions(turn)
    state = build_learning_state(turn, candidates=candidates,
                                 persona_prompt="我是群聊里的助手。" * 30)
    built = build_request(state, questions)
    assert built is not None
    payload, _ = built
    assert {item["id"] for item in payload["questions"]} >= {"join", "action", "target"}


def test_long_agentjev_path_is_bounded_locally():
    state = copy.deepcopy(STATE)
    state["conversation"]["messages"] = [{"message_id": "m1", "author": "u1",
                                            "text": "长日志" * 2000}]
    state["persona"] = "设定" * 1000
    request, _ = build_request(state, QUESTIONS)
    for question in request["questions"]:
        for option in question["options"].values():
            assert len(("[STATE] " + request["state"] + "\n[QUESTION] "
                        + question["question"] + "\n[CANDIDATE] " + option).encode("utf-8")) <= MAX_PATH_BYTES
    assert "设定" * 1000 not in request["state"]


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(api_version="wrong"),
    lambda value: value["results"][0]["answers"][0]["distribution"].update(m1=1.2),
    lambda value: value["results"][0]["answers"][1].update(value="false"),
    lambda value: value["results"][0]["answers"].pop(),
])
def test_malformed_or_inconsistent_model_output_is_rejected(mutate):
    value = copy.deepcopy(response())
    mutate(value)
    with pytest.raises(ValueError):
        parse_response(value, QUESTIONS, {"m1": "target.0"})


@pytest.mark.asyncio
async def test_client_calls_agentjev_endpoint_once():
    client = AgentJevClient(base_url="http://127.0.0.1:18765")
    seen = []

    async def fake(path, payload, **kwargs):
        seen.append((path, payload))
        return response()

    client.transport.request_json = fake
    try:
        result = await client.evaluate(state=STATE, questions=QUESTIONS)
        assert result["model_version"] == "fine-tuned"
        assert result["answers"]["action"]["choice"] == "reply"
        assert len(seen) == 1 and seen[0][0] == "/api/evaluate"
        assert len(seen[0][1]["questions"]) == 5
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_active_mode_keeps_teacher_until_agentjev_is_calibrated(tmp_path):
    cfg = SimpleNamespace(decision_learning_mode="active", decision_learning_sessions=(),
                          decision_learning_student_backend="agentjev",
                          decision_learning_jev_fallback=False, decision_timeout=1.0,
                          agentjev_timeout=.2, decision_provider_id="teacher")
    student = SimpleNamespace(evaluate=AsyncMock(return_value={
        "answers": {"join": {"type": "noul", "noul": .99}},
        "model_version": "unchecked"}))
    host = SimpleNamespace(_runtime_config=cfg, _sessions={}, agentjev=student)
    runtime = DecisionLearning(host, tmp_path)
    runtime._teacher = AsyncMock(return_value={"join": {"type": "noul", "noul": 0.0}})
    result = await runtime.evaluate(session_id="room", state=STATE,
                                    questions={"join": QUESTIONS["join"]})
    assert result["join"]["noul"] == 0.0
    assert student.evaluate.await_count == 1
    assert runtime.stats["agentjev"] == 0
    await runtime.close()
