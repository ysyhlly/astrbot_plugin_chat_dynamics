"""Delivered dialogue scope, frozen Jev context and event-driven continuation."""
import json
from dataclasses import replace

import pytest

from astrbot_plugin_chat_dynamics.core.active_dialogue import dialogue_for_node
from astrbot_plugin_chat_dynamics.core.dialogue_context import capture_dialogue
from astrbot_plugin_chat_dynamics.core.dialogue_continuity import evaluate
from astrbot_plugin_chat_dynamics.core.recipient_resolver import RecipientResolver
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.prompt_policy import PROMPT_DEFAULTS, PREVIOUS_PROMPT_DEFAULTS
from astrbot_plugin_chat_dynamics.core.runtime_persistence import export_runtime_state, restore_runtime_state
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import (
    MAX_TOTAL_REQUEST_CHARS, bound_request_state, build_questions, build_state, request_size,
)
from astrbot_plugin_chat_dynamics.core.persona_engine import is_request_supplement, turn_is_continuation
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext, clip_conversation_text
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_jev_turn_completion import completion_plugin as completion_plugin, tick
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent
from .test_runtime_persistence import plugin as persistence_plugin


def route(node, topic="task", confidence=.9, **extra):
    node.metadata["routing"] = dict(topic_id=topic, topic_status="committed", topic_ambiguous=False,
                                    topic_confidence=confidence, **extra)
    return node


def room():
    return SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG())


def delivered(runtime, owner="alice", topic="task", stamp=100, text="先检查配置", action="reply", state="focused"):
    human = route(runtime.dag.add_message(f"u-{owner}-{stamp}", owner, "帮我修复插件，必须离线", timestamp=stamp), topic)
    bot = route(runtime.dag.add_message(f"b-{owner}-{stamp}", "bot", text, timestamp=stamp + 1,
        reply_to_id=human.msg_id, metadata={"dialogue_delivered": True, "trigger_user_id": owner,
            "dialogue_stop_revision": runtime.topic_stop_revisions.get(owner, 0), "dialogue_state": state,
            "dialogue_action": action}), topic)
    runtime.last_bot_node, runtime.last_interlocutor, runtime.last_model_send = bot, owner, stamp + 1
    runtime.remember_sent(bot.msg_id)
    return human, bot


def turn(node, runtime, **extra):
    return TurnContext(runtime.session_key, node.user_id, node.text,
                       (MessageSnapshot(node.msg_id, node.user_id, "", source_text=node.text,
                                        timestamp=node.timestamp),), (), runtime.epoch, 0, node.timestamp,
                       False, wake_kind="none", bot_id=runtime.bot_id, **extra)


def test_interleaved_delivered_dialogue_continues_its_own_topic():
    runtime = room()
    _, first = delivered(runtime)
    delivered(runtime, "bob", "movies", 103)
    current = route(runtime.dag.add_message("current", "alice", "已经检查了，还是不行", timestamp=110))
    dialogue = dialogue_for_node(runtime, current)
    assert dialogue.last_bot_message_id == first.msg_id
    assert turn_is_continuation(runtime, turn(current, runtime), 110)
    assert capture_dialogue(runtime, current, frozenset(runtime.dag.nodes)).last_bot_text == first.text


@pytest.mark.parametrize("kind", ["other_recipient", "other_topic", "other_owner", "expired", "closed", "stopped", "reset"])
def test_scoped_continuation_preserves_admission_boundaries(kind):
    runtime = room()
    delivered(runtime, action="close" if kind == "closed" else "reply")
    topic, owner, stamp, extra = "task", "alice", 110, {}
    if kind == "other_recipient":
        extra = dict(addressee_ids=["bob"], addressee_confidence=1)
    elif kind == "other_topic":
        topic = "movies"
    elif kind == "other_owner":
        owner = "bob"
    elif kind == "expired":
        stamp = 1002
    elif kind == "stopped":
        runtime.user_revisions["alice"] = 1
        runtime.topic_stop_revisions["alice"] = 1
    elif kind == "reset":
        runtime.reset_conversation_state()
    node = route(runtime.dag.add_message("current", owner, "继续解释", timestamp=stamp), topic, **extra)
    assert not turn_is_continuation(runtime, turn(node, runtime), stamp)


@pytest.mark.parametrize("state,confidence,accepted", [("focused", .9, True), ("supportive", .9, False),
    ("casual", .9, False), ("focused", .5, False)])
def test_only_confirmed_task_topics_keep_a_longer_continuation_window(state, confidence, accepted):
    runtime = room()
    delivered(runtime, state=state)
    node = route(runtime.dag.add_message("current", "alice", "改完了还是不行", timestamp=500), confidence=confidence)
    assert turn_is_continuation(runtime, turn(node, runtime), 500) is accepted


def test_a_newer_unsent_draft_cannot_replace_a_delivered_anchor():
    runtime = room()
    human, bot = delivered(runtime)
    route(runtime.dag.add_message("draft", "bot", "草稿里的问题？", timestamp=105, reply_to_id=human.msg_id))
    node = route(runtime.dag.add_message("current", "alice", "继续", timestamp=110))
    assert dialogue_for_node(runtime, node).last_bot_message_id == bot.msg_id


def test_recipient_resolver_uses_the_owned_question_after_another_dialogue():
    runtime = room()
    _, bot = delivered(runtime, text="你使用哪个版本？")
    delivered(runtime, "bob", "movies", 103)
    node = route(runtime.dag.add_message("current", "alice", "1.21.4", timestamp=110))
    result = RecipientResolver().infer(node=node, dag=runtime.dag, runtime=runtime,
        topic_id="task", ranked_topics=[(.9, "task")], recent_nodes=runtime.dag.get_recent_nodes(limit=0))
    assert result.bot_targeted and "active_dialogue_answer" in result.evidence
    assert result.parent_override[0] == bot.msg_id


def test_question_in_an_earlier_delivered_fragment_remains_pending():
    runtime = room()
    _, bot = delivered(runtime, text="你使用哪个版本？")
    tail = route(runtime.dag.add_message("tail", "bot", "可以从设置页查看", timestamp=102,
        reply_to_id=bot.msg_id, metadata={**bot.metadata, "dialogue_delivered": True}))
    runtime.last_bot_node = tail
    node = route(runtime.dag.add_message("current", "alice", "1.21.4", timestamp=103))
    snapshot = capture_dialogue(runtime, node, frozenset(runtime.dag.nodes))
    assert snapshot.question_pending and snapshot.pending_question == bot.text
    assert snapshot.last_bot_text == bot.text + "\n" + tail.text


@pytest.mark.parametrize("confidence,ambiguous,topic,accepted", [
    (.9, False, "movies", True), (.5, False, "movies", False),
    (.9, True, "movies", False), (.9, False, "task", False),
])
def test_only_confirmed_unrelated_chatter_is_excluded_from_question_interruptions(confidence, ambiguous, topic, accepted):
    runtime = room()
    delivered(runtime, text="你使用哪个版本？")
    node = route(runtime.dag.add_message("current", "alice", "1.21.4", timestamp=110))
    chatter = route(runtime.dag.add_message("chatter", "bob", "今晚看电影", timestamp=105), topic, confidence)
    chatter.metadata["routing"]["topic_ambiguous"] = ambiguous
    assert evaluate(dialogue_for_node(runtime, node), node, [chatter]).accepted is accepted


def test_dialogue_snapshot_excludes_late_sources_and_freezes_text():
    runtime = room()
    _, bot = delivered(runtime)
    correction = route(runtime.dag.add_message("correction", "alice", "改成只用 CPU", timestamp=105))
    node = route(runtime.dag.add_message("current", "alice", "继续", timestamp=110))
    visible = frozenset(runtime.dag.nodes)
    snapshot = capture_dialogue(runtime, node, visible)
    delivered(runtime, "alice", "task", 110, text="后到内容")
    correction.text = "后改的原文"
    bot.text = "后来修改的回复"
    assert snapshot.last_bot_text == "先检查配置"
    assert ("correction", "改成只用 CPU") in snapshot.user_updates
    next_snapshot = capture_dialogue(runtime, node, visible)
    assert next_snapshot.last_bot_message_id == bot.msg_id
    assert all(mid in visible for mid in next_snapshot.source_message_ids)


def test_answered_question_is_not_presented_as_pending_again():
    runtime = room()
    _, bot = delivered(runtime, text="你使用哪个版本？")
    answer = route(runtime.dag.add_message("answer", "alice", "1.21.4", timestamp=105, reply_to_id=bot.msg_id))
    node = route(runtime.dag.add_message("current", "alice", "还有一个问题", timestamp=110))
    snapshot = capture_dialogue(runtime, node, frozenset(runtime.dag.nodes))
    assert (answer.msg_id, answer.text) in snapshot.user_updates
    assert not snapshot.question_pending and not snapshot.pending_question


def test_request_supplement_survives_more_than_twelve_unrelated_messages():
    runtime = room()
    wake = route(runtime.dag.add_message("wake", "alice", "帮我修复插件", timestamp=100,
                                        metadata={"is_wake": True}))
    for i in range(20):
        route(runtime.dag.add_message(f"chatter-{i}", "bob", "电影讨论", timestamp=101 + i), "movies")
    route(runtime.dag.add_message("current", "alice", "必须离线", timestamp=123))
    assert is_request_supplement(runtime, "alice", 123)
    assert not is_request_supplement(runtime, "alice", 221)
    wake.metadata["dialogue_stop_revision"] = 0
    runtime.user_revisions["alice"] = 1
    runtime.topic_stop_revisions["alice"] = 1
    assert not is_request_supplement(runtime, "alice", 123)


def test_scoped_stop_revision_and_delivered_anchor_survive_restart():
    source = persistence_plugin(120)
    runtime = source._registry.get_or_create("room")
    runtime.bot_id = "bot"
    _, old = delivered(runtime, stamp=100)
    runtime.topic_stop_revisions["alice"] = 1
    # Counter persistence must not truncate at the generic JSON dict's 128 keys.
    for i in range(140):
        owner = f"user-{i}"
        route(runtime.dag.add_message(f"human-{i}", owner, "背景", timestamp=110))
        runtime.topic_stop_revisions[owner] = 2
    target = persistence_plugin(120)
    restore_runtime_state(target, export_runtime_state(source))
    restored = target._registry.get("room")
    assert restored.topic_stop_revisions["user-139"] == 2
    node = route(restored.dag.add_message("current", "alice", "继续", timestamp=120))
    assert dialogue_for_node(restored, node) is None
    assert restored.dag.get_node(old.msg_id).metadata["dialogue_delivered"]
    _, fresh = delivered(restored, stamp=120)
    later = route(restored.dag.add_message("later", "alice", "继续", timestamp=123))
    assert dialogue_for_node(restored, later).last_bot_message_id == fresh.msg_id


@pytest.mark.parametrize("key", ["decision_prompt", "reply_prompt"])
def test_previous_default_guidance_upgrades_without_overwriting_custom_text(key):
    cfg, warnings = parse_runtime_config({key: PREVIOUS_PROMPT_DEFAULTS[key]})
    assert not warnings and getattr(cfg, key) == PROMPT_DEFAULTS[key]
    custom = PREVIOUS_PROMPT_DEFAULTS[key] + "\n只讨论游戏"
    cfg, warnings = parse_runtime_config({key: custom})
    assert not warnings and getattr(cfg, key) == custom


@pytest.mark.parametrize("value,expected", [(1, 1), (300, 300), (0, 60), (float("nan"), 60)])
def test_pending_input_timeout_is_validated(value, expected):
    cfg, _ = parse_runtime_config({"pending_input_timeout": value})
    assert cfg.pending_input_timeout == expected


@pytest.mark.asyncio
async def test_pending_input_setting_is_saved_and_reported_as_effective(jev_plugin):
    p, _ = jev_plugin
    try:
        panel = await p.save_config_values({"pending_input_timeout": 12})
        assert panel["effective"]["pending_input_timeout"] == 12
        assert p.debounce.pending_input_timeout == 12
        assert p.config["pending_input_timeout"] == 12
    finally:
        await p.terminate()


def fragment_turn(parts):
    return TurnContext("room", "alice", "\n".join(parts), tuple(
        MessageSnapshot(f"m{i}", "alice", "", source_text=p, timestamp=100 + i)
        for i, p in enumerate(parts)), (), 0, 0, 100, False, bot_id="bot")


def test_source_fragment_boundaries_are_visible_without_repeating_current_text():
    first = fragment_turn(["第一段\n第二段", "第三段"])
    second = fragment_turn(["第一段", "第二段\n第三段"])
    a, b = first.payload(), second.payload()
    assert a["text"] == b["text"] and a["messages"] != b["messages"]
    for part, message in zip(["第一段\n第二段", "第三段"], a["messages"]):
        assert "text" not in message
        assert "".join(a["text"][start:end] for start, end in message["text_spans"]) == part


def test_successive_text_clips_preserve_exact_ranges_and_omitted_fragment_excerpts():
    context = fragment_turn(["开头条件" + "a" * 200, "中间片段" + "b" * 200, "c" * 200 + "最后条件"])
    payload = context.payload()
    clip_conversation_text(payload, 180)
    clip_conversation_text(payload, 100)
    assert "开头条件" in payload["text"] and "最后条件" in payload["text"]
    assert "中间片段" in payload["messages"][1]["text_excerpt"]
    for message in (payload["messages"][0], payload["messages"][2]):
        assert all(0 <= start < end <= len(payload["text"]) for start, end in message["text_spans"])
        excerpt = "".join(payload["text"][start:end] for start, end in message["text_spans"])
        assert excerpt in context.messages[int(message["message_id"][1:])].source_text
    assert len(context.text) > 600


def test_unknown_text_transform_uses_bounded_source_excerpts_not_invented_ranges():
    payload = replace(fragment_turn(["第一段", "第二段"]), text="经过转换的正文").payload()
    assert [m["text_excerpt"] for m in payload["messages"]] == ["第一段", "第二段"]
    assert all("text_spans" not in m for m in payload["messages"])


def test_tail_only_clipping_attributes_repeated_text_to_the_last_fragment():
    payload = fragment_turn(["重复字", "中间", "重复字"]).payload()
    clip_conversation_text(payload, 3)
    assert "text_spans" not in payload["messages"][0]
    assert payload["messages"][-1]["text_spans"] == [[0, 3]]


def test_background_age_is_relative_to_the_frozen_turn():
    context = replace(fragment_turn(["继续"]), background=(MessageSnapshot("old", "bot", "先前回复", timestamp=90),))
    assert context.payload()["background"][0]["age_seconds"] == 10
    assert replace(context, snapshot_at=105).payload()["background"][0]["age_seconds"] == 15


def test_state_compression_keeps_completion_signals_and_all_request_budgets():
    context = fragment_turn(["补充条件" * 40] * 24)
    topics = {f"topic_{i}": {"label": "主题" * 24, "excerpt": "历史" * 90} for i in range(80)}
    essential = dict(completion_waited=True, pending_input=True, utterance_pause_seconds=.25,
                     utterance_age_seconds=10, queue_delay_seconds=2, ambient_openings_used=2)
    state = build_state(context, observations={**essential, "auxiliary": "x" * 10000}, active_topics=topics)
    assert state["observations"] == essential
    assert len(json.dumps(state, ensure_ascii=False)) <= 12000
    context = fragment_turn(["补充条件" * 40] * 6)
    state = build_state(context, observations={**essential, "auxiliary": "x" * 10000}, active_topics=topics)
    questions = build_questions(context)
    bounded = bound_request_state(state, questions, max_chars=12000)
    assert request_size(bounded, questions) <= 12000 < MAX_TOTAL_REQUEST_CHARS
    assert bounded["observations"] == essential
    assert context.text == "\n".join(["补充条件" * 40] * 6)


@pytest.mark.asyncio
@pytest.mark.parametrize("cooling", [False, True])
async def test_jev_accepted_interleaved_followup_is_not_charged_as_a_new_opening(jev_plugin, monkeypatch, cooling):
    p, bridge = jev_plugin
    original_route, original_bot = p._route_message, p._observe_routed_bot
    topics = {"a": "task", "b": "movies", "again": "task"}

    def route_current(runtime, node):
        result = original_route(runtime, node)
        if node.msg_id in topics:
            route(node, topics[node.msg_id], addressee_ids=[], bot_addressee_confidence=0)
        return result

    def observe_bot(runtime, bot):
        original_bot(runtime, bot)
        route(bot, runtime.dag.get_node(bot.reply_to_id).metadata["routing"]["topic_id"])

    monkeypatch.setattr(p, "_route_message", route_current)
    monkeypatch.setattr(p, "_observe_routed_bot", observe_bot)
    p.decision_gate = None
    try:
        for mid, owner, text in [("a", "alice", "插件启动报错"), ("b", "bob", "最近什么电影好看")]:
            p.jev.payload = answers(reason="open_group_topic")
            event = MockEvent(text, sender_id=owner, message_id=mid)
            await p.on_group_message(event)
            await flush(p, event)
            await drain(p)
            assert event.replies_sent
        runtime = p._sessions[event.unified_msg_origin]
        assert len(runtime.ambient_openings) == 2
        if cooling:
            monkeypatch.setattr(p.arbiter, "is_in_deep_cooling", lambda *args, **kwargs: True)
        p.jev.payload = answers(reason="ongoing_thread")
        event = MockEvent("已经检查了，还是不行", sender_id="alice", message_id="again")
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert event.replies_sent and len(bridge.requests) == 3, runtime.model_diagnostic
        assert len(runtime.ambient_openings) == 2
        state = p.jev.calls[-1]["state"]
        assert state["conversation"]["dialogue"]["user_id"] == "alice"
        assert state["previous_state"] == "focused"
        assert bridge.requests[-1][0]["conversation"]["dialogue"]["user_id"] == "alice"
        assert request_size(state, p.jev.calls[-1]["questions"]) <= MAX_TOTAL_REQUEST_CHARS
        if cooling:
            event = MockEvent("换个新话题聊游戏", sender_id="new-user", message_id="new-opening")
            count = len(p.jev.calls)
            await p.on_group_message(event)
            await flush(p, event)
            await drain(p)
            assert len(p.jev.calls) == count and not event.replies_sent
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_promised_input_resumes_after_the_short_burst_cap_without_polling(completion_plugin):
    p = completion_plugin
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": .9})
    first = MockEvent("先等我贴完日志，必须离线修复", message_id="first", is_at_or_wake_command=True)
    try:
        await p.on_group_message(first)
        await tick(p)
        await tick(p, 10)
        assert len(p.jev.calls) == 1 and p.debounce.get_pending_count() == 1
        p.jev.payload = answers()
        second = MockEvent("日志是 PermissionError，应该怎么修复", message_id="second")
        await p.on_group_message(second)
        await tick(p)
        assert second.replies_sent and not first.replies_sent
        assert len(p.jev.calls) == 2
        state = p.jev.calls[-1]["state"]
        assert state["conversation"]["text"] == first.message_str + "\n" + second.message_str
        assert state["observations"]["completion_waited"] and state["observations"]["pending_input"]
        assert p.debounce.get_pending_count() == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_repeated_new_fragments_do_not_extend_the_original_input_deadline(completion_plugin):
    p = completion_plugin
    p.debounce.pending_input_timeout = 5
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": .9})
    try:
        first = MockEvent("我还没说完", message_id="first", is_at_or_wake_command=True)
        await p.on_group_message(first)
        await tick(p)
        await tick(p, 3)
        second = MockEvent("还有一段", message_id="second")
        await p.on_group_message(second)
        await tick(p)
        assert len(p.jev.calls) == 2 and p.debounce.get_pending_count() == 2
        await tick(p, 1.5)
        assert p.debounce.get_pending_count() == 0 and len(p.jev.calls) == 2
        assert p._sessions[first.unified_msg_origin].model_diagnostic["reason_code"] == "jev_incomplete_expired"
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_expired_input_is_not_merged_into_a_new_request(completion_plugin):
    p = completion_plugin
    p.debounce.pending_input_timeout = 1
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": .9})
    try:
        first = MockEvent("尚未完成的旧问题", message_id="first", is_at_or_wake_command=True)
        await p.on_group_message(first)
        await tick(p)
        await tick(p, 1)
        p.jev.payload = answers()
        second = MockEvent("小助手，换个话题聊电影", message_id="second", is_at_or_wake_command=True)
        await p.on_group_message(second)
        await tick(p)
        assert p.jev.calls[-1]["state"]["conversation"]["text"] == second.message_str
        assert second.replies_sent
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_real_at_can_finish_a_parked_input_without_waiting_for_jev(completion_plugin):
    p = completion_plugin
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": .9})
    try:
        first = MockEvent("等我补充日志", message_id="first", is_at_or_wake_command=True)
        await p.on_group_message(first)
        await tick(p)
        second = MockEvent("日志贴好了，解释一下", message_id="second", components=[At("bot_42")])
        await p.on_group_message(second)
        await tick(p, 0)
        assert second.replies_sent and len(p.jev.calls) == 1
        assert p.debounce.get_pending_count() == 0
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_queue_delay_does_not_become_speaker_pause(completion_plugin, monkeypatch):
    p = completion_plugin
    entered = []
    original = p.persona_engine._decide_layer

    async def queued(*args, **kwargs):
        entered.append(True)
        await p.time_service.advance(5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(p.persona_engine, "_decide_layer", queued)
    try:
        event = MockEvent("小助手，这个错误如何处理", message_id="queued", is_at_or_wake_command=True)
        await p.on_group_message(event)
        await tick(p)
        assert entered and event.replies_sent
        observations = p.jev.calls[-1]["state"]["observations"]
        assert observations["utterance_pause_seconds"] == pytest.approx(.25)
        assert observations["queue_delay_seconds"] == pytest.approx(5)
    finally:
        await p.terminate()
