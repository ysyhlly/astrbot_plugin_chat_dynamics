"""Public-topic participation and editable operator guidance in the native path."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.jev_decision import build_state
from astrbot_plugin_chat_dynamics.core.prompt_policy import MAX_PROMPT_CHARS, PROMPT_DEFAULTS
from astrbot_plugin_chat_dynamics.core.social_manners import SocialMannersGate
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, Reply


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("presence,should_join", [("lively", True), ("sensible", False), ("ghost", False)])
@pytest.mark.asyncio
async def test_public_topic_between_two_humans_can_be_joined_in_lively_mode(jev_plugin, quoted, presence, should_join):
    p, bridge = jev_plugin
    try:
        await p.save_config_values({"presence_knob": presence})
        p.jev.payload = answers(action="ignore", reason="low_value_chatter")
        for index, (author, text) in enumerate([
            ("alice", "最近在玩星露谷"), ("bob", "我也是，种地挺放松"), ("alice", "我最喜欢钓鱼"),
        ]):
            if index == 2:
                p.jev.payload = answers(reason="open_group_topic", state="casual")
            event = MockEvent(text, sender_id=author, message_id=f"topic-{index}",
                              components=[Reply(f"topic-{index - 1}")] if quoted and index else [])
            await p.on_group_message(event)
            await flush(p, event)
            await drain(p)
        assert bool(event.replies_sent) is should_join
        assert bool(bridge.requests) is should_join
        runtime = p._sessions[event.unified_msg_origin]
        if should_join:
            assert runtime.model_diagnostic["reason_code"] == "jev_open_group_topic"
            assert len(runtime.ambient_openings) == 1
            conversation = p.jev.calls[-1]["state"]["conversation"]
            assert conversation["wake_kind"] == "none" and not conversation["explicit"]
            assert p.jev.calls[-1]["state"]["participation_policy"]["presence_knob"] == "lively"
    finally:
        await p.terminate()


@pytest.mark.parametrize("reason", ["other_recipient", "boundary_or_sensitive", "low_value_chatter"])
@pytest.mark.asyncio
async def test_lively_still_observes_when_the_decision_rejects_joining(jev_plugin, reason):
    p, bridge = jev_plugin
    try:
        await p.save_config_values({"presence_knob": "lively"})
        p.jev.payload = answers(action="ignore", reason=reason)
        event = MockEvent("这件事我们私下再说", message_id="private")
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not event.replies_sent and not bridge.requests
    finally:
        await p.terminate()


def test_public_topic_hint_keeps_explicit_human_recipient_and_conflict_boundaries():
    nodes = [SimpleNamespace(msg_id=str(i), user_id=author, reply_to_id="", mentioned_users=[])
             for i, author in enumerate(["alice", "bob", "alice"])]
    gate = SocialMannersGate()
    kwargs = dict(session_id="room", user_id="alice", text="这个话题", recent_nodes=nodes,
                  bot_id="bot", presence_knob="lively", public_topic=True)
    assert gate.evaluate(**kwargs).allow
    nodes[-1].mentioned_users = ["bob"]
    assert gate.evaluate(**kwargs).reason_code == "relay_baton"
    nodes[-1].mentioned_users = []
    assert gate.evaluate(**kwargs, occasion_kind="conflict").reason_code == "conflict_silence"


@pytest.mark.asyncio
async def test_lively_public_topic_still_has_an_opening_budget_and_at_bypasses_it(jev_plugin):
    p, bridge = jev_plugin
    try:
        await p.save_config_values({"presence_knob": "lively"})
        p.jev.payload = answers(reason="open_group_topic", state="casual")
        for index in range(5):
            event = MockEvent("最近有什么好玩的游戏", sender_id=f"user-{index}", message_id=f"budget-{index}")
            await p.on_group_message(event)
            await flush(p, event)
            await drain(p)
            assert bool(event.replies_sent) is (index < 4)
        runtime = p._sessions[event.unified_msg_origin]
        assert len(bridge.requests) == 4 and len(runtime.ambient_openings) == 4
        assert runtime.model_diagnostic["reason_code"] == "ambient_budget"
        event = MockEvent("推荐一下", sender_id="addressed", message_id="at", components=[At("bot_42")])
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert event.replies_sent and len(bridge.requests) == 5
        assert len(p.jev.calls) == 5 and len(runtime.ambient_openings) == 4
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_custom_prompts_apply_to_next_turn_and_empty_restores_defaults(jev_plugin):
    p, bridge = jev_plugin
    custom = {"decision_prompt": "主动聊游戏\n也可以交流做饭经验", "reply_prompt": "用两句话回答\n先接住话题"}
    try:
        panel = await p.save_config_values(custom)
        assert all(panel["effective"][key] == value for key, value in custom.items())
        event = MockEvent("刚做了番茄炒蛋", message_id="custom")
        p.jev.payload = answers(reason="open_group_topic")
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert p.jev.calls[-1]["state"]["decision_prompt"] == custom["decision_prompt"]
        assert bridge.requests[-1][0]["reply_guidance"] == custom["reply_prompt"]
        assert bridge.requests[-1][1] == bridge.persona
        panel = await p.save_config_values({key: "" for key in custom})
        assert all(panel["effective"][key] == value for key, value in PROMPT_DEFAULTS.items())
        assert not set(custom).intersection(panel["mismatches"])
        event = MockEvent("你好", message_id="default", components=[At("bot_42")])
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bridge.requests[-1][0]["reply_guidance"] == PROMPT_DEFAULTS["reply_prompt"]
    finally:
        await p.terminate()


@pytest.mark.parametrize("key", list(PROMPT_DEFAULTS))
@pytest.mark.parametrize("invalid", [123, "x" * (MAX_PROMPT_CHARS + 1)])
@pytest.mark.asyncio
async def test_invalid_prompt_save_preserves_configuration(jev_plugin, key, invalid):
    p, _ = jev_plugin
    try:
        original = dict(p.config)
        with pytest.raises(ValueError, match="提示词"):
            await p.save_config_values({key: invalid, "presence_knob": "lively"})
        assert dict(p.config) == original
        assert p._runtime_config.presence_knob == "sensible"
    finally:
        await p.terminate()


def test_prompt_defaults_match_schema_and_runtime_and_bound_external_config():
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
    cfg, warnings = parse_runtime_config({})
    assert not warnings
    for key, default in PROMPT_DEFAULTS.items():
        assert getattr(cfg, key) == schema[key]["default"] == default
        cfg, warnings = parse_runtime_config({key: "x" * (MAX_PROMPT_CHARS + 1)})
        assert len(getattr(cfg, key)) == MAX_PROMPT_CHARS and any(key in warning for warning in warnings)
    turn = TurnContext("room", "user", "正文" * 4000, (MessageSnapshot("m", "user", ""),), (), 0, 0, 0, False)
    state = build_state(turn, decision_prompt="自定义" * 2000)
    assert len(state["decision_prompt"]) == MAX_PROMPT_CHARS
    assert len(json.dumps(state, ensure_ascii=False)) <= 12000


@pytest.mark.parametrize("max_chars", [3000, 12000])
def test_long_custom_prompt_respects_a_smaller_decision_state_budget(max_chars):
    turn = TurnContext("room", "user", "聊游戏", (MessageSnapshot("m", "user", ""),), (), 0, 0, 0, False)
    state = build_state(turn, decision_prompt="自定义" * 2000, max_chars=max_chars)
    assert len(json.dumps(state, ensure_ascii=False)) <= max_chars
    assert state["conversation"]["text"] == "聊游戏"
    assert state["decision_prompt"].startswith("自定义")
