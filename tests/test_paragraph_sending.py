"""Natural paragraphs reach the platform independently, with accurate partial history."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import AgentOutput
from astrbot_plugin_chat_dynamics.core.pacer import PacingShaper, scale_delay
from astrbot_plugin_chat_dynamics.core.persona_engine import delivery_fragments
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult, build_plain_chain, chain_plain_text
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_member_stop import NativeEvent, _bot_texts, _drain, _stop_event
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, _plugin
from .test_poke import Poke


PARAGRAPHS = [
    "（慢吞吞地往卫衣兜帽里缩了缩，任由你凑过来，整个人像没有骨头一样靠着）……",
    "……好重。千织怎么也突然跑来索取肢体接触。",
    "（把手里的厚素描本往旁边挪了半寸，声音闷闷的）……就只能抱一小会儿。不准乱蹭，"
    "吾辈下午写大纲消耗的微弱电量……全被你当成暖炉抽走了。",
]
REPLY = "\n\n".join(PARAGRAPHS)


def test_short_roleplay_example_keeps_three_natural_paragraphs():
    assert len(REPLY) < 1200
    assert PacingShaper.persona_fragments(REPLY) == PARAGRAPHS


def test_repeated_conversational_paragraphs_are_not_dropped():
    assert delivery_fragments((), "……\n\n……\n\n好重。", PacingShaper()) == ["……", "……", "好重。"]


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_tool_caption_is_removed_before_merging_remaining_prose(limit):
    chain = build_plain_chain(PARAGRAPHS[0])
    expected = ["\n\n".join(PARAGRAPHS[1:])] if limit == 1 else PARAGRAPHS[1:]
    assert delivery_fragments((chain,), REPLY, PacingShaper(), max_fragments=limit) == [chain, *expected]


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n", "\n \t\n\n"])
def test_only_blank_lines_split_action_and_dialogue(separator):
    action_and_dialogue = "（靠在椅背上）\n……只能抱一小会儿。"
    assert PacingShaper.persona_fragments("\n " + action_and_dialogue + separator + "晚点再聊。\n") == [
        action_and_dialogue, "晚点再聊。",
    ]


@pytest.mark.parametrize("limit, expected", [
    (1, ["第一段\n\n第二段\n\n第三段\n\n第四段\n\n第五段"]),
    (2, ["第一段", "第二段\n\n第三段\n\n第四段\n\n第五段"]),
    (3, ["第一段", "第二段", "第三段\n\n第四段\n\n第五段"]),
])
def test_segment_limit_merges_excess_paragraphs_without_losing_text(limit, expected):
    text = "第一段\n\n第二段\n\n第三段\n\n第四段\n\n第五段"
    assert delivery_fragments((), text, PacingShaper(), max_fragments=limit) == expected
    for mode in GroupChatMode:
        assert PacingShaper().shape_and_fragment(text, mode, max_fragments=limit) == expected


@pytest.mark.parametrize("text", [
    "运行这段代码：\n\n```python\nprint('first')\n\nprint('second')\n```\n\n完成。",
    "请先运行 `python -m pytest`。\n\n然后检查结果。",
    "推导过程：\n\n$$x^2 + y^2 = 1$$\n\n以上是公式。",
    "操作步骤：\n\n- 安装依赖\n- 保存配置\n\n完成后重启。",
    "操作步骤：\n\n1. 安装依赖\n2. 保存配置\n\n完成后重启。",
    "比较：\n\n| 方案 | 时间 |\n| --- | --- |\n| A | 1 秒 |\n\n选择 A。",
    "打开 https://example.invalid/guide 。\n\n按页面说明操作。",
])
def test_structured_answers_stay_intact_in_persona_and_native_modes(text):
    assert PacingShaper.persona_fragments(text) == [text]
    for mode in GroupChatMode:
        assert PacingShaper().shape_and_fragment(text, mode) == [text]


@pytest.mark.parametrize("mode", list(GroupChatMode))
def test_native_roleplay_uses_paragraphs_without_rewriting_prose(mode):
    assert PacingShaper().shape_and_fragment(REPLY, mode) == PARAGRAPHS


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.asyncio
async def test_persona_sends_actual_paragraphs_and_chains_reply_identity(jev_plugin, monkeypatch, limit):
    p, bridge = jev_plugin
    p._runtime_config = replace(p._runtime_config, max_fragments=limit)
    bridge.response = REPLY
    event = MockEvent("抱一下", message_id="roleplay", components=[At("bot_42")])
    network, delays, spoke = [], [], []
    record_spoke = p.arbiter.record_bot_spoke

    def record(*args, **kwargs):
        spoke.append(kwargs)
        return record_spoke(*args, **kwargs)

    async def sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    async def send(_runtime, _event, text, *, reply_to_id=None):
        network.append((text, reply_to_id))
        return SendResult(True, f"delivered-{len(network)}")

    monkeypatch.setattr(p.arbiter, "record_bot_spoke", record)
    p.time_service.sleep = sleep
    p._send_owned = send
    expected = [*PARAGRAPHS[:limit - 1], "\n\n".join(PARAGRAPHS[limit - 1:])]
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert network == [(text, "roleplay" if index == 0 else f"delivered-{index}")
                           for index, text in enumerate(expected)]
        runtime = p._sessions[event.unified_msg_origin]
        # The same clock also serves zero-delay ingress and the 30s topic timer.
        assert [delay for delay in delays if 0 < delay < 10] == [
            pytest.approx(scale_delay(1.2, runtime.last_delay_scale)),
        ] * (limit - 1)
        assert len(spoke) == 1
        assert len(bridge.requests) == 1 and bridge.commits == [REPLY]
        nodes = [node for node in runtime.dag.nodes.values() if node.user_id == runtime.bot_id]
        assert [node.text for node in nodes] == expected
        assert [node.reply_to_id for node in nodes] == [parent for _, parent in network]
        assert all(node.metadata["trigger_user_id"] == event.sender_id for node in nodes)
        assert runtime.model_admission._value == 9 and not runtime.owned_turn_tasks
    finally:
        await p.terminate()


def prepare_poke(p, bridge):
    async def generate(*args, **kwargs):
        return AgentOutput(REPLY, "conv", "[]", [])

    bridge.generate = generate
    p.poke_policy = SimpleNamespace(decide=lambda **kwargs: SimpleNamespace(speak=True, poke_back=False))
    return MockEvent("", message_id="paragraph-poke", components=[Poke("bot_42")])


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.asyncio
async def test_poke_sends_paragraphs_with_shared_limit_and_interval(jev_plugin, limit):
    p, bridge = jev_plugin
    p._runtime_config = replace(p._runtime_config, max_fragments=limit)
    event = prepare_poke(p, bridge)
    delays = []

    async def sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    p.time_service.sleep = sleep
    try:
        await p.on_group_message(event)
        expected = [*PARAGRAPHS[:limit - 1], "\n\n".join(PARAGRAPHS[limit - 1:])]
        assert event.replies_sent == expected
        assert delays == [pytest.approx(1.2)] * (limit - 1)
        assert bridge.commits == [REPLY]
        assert _bot_texts(p._sessions[event.unified_msg_origin]) == expected
    finally:
        await p.terminate()


@pytest.mark.parametrize("path", ["persona", "poke"])
@pytest.mark.asyncio
async def test_member_stop_during_paragraph_pause_commits_only_sent_head(jev_plugin, path):
    p, bridge = jev_plugin
    bridge.response = REPLY
    event = (prepare_poke(p, bridge) if path == "poke" else
             MockEvent("抱一下", message_id="stop-paragraphs", components=[At("bot_42")]))
    paused, release = asyncio.Event(), asyncio.Event()
    task = None

    async def sleep(delay):
        if event.replies_sent and 0 < delay < 10:
            paused.set()
            await release.wait()
        else:
            await asyncio.sleep(0)

    p.time_service.sleep = sleep
    try:
        if path == "poke":
            task = asyncio.create_task(p.on_group_message(event))
        else:
            await p.on_group_message(event)
            await flush(p, event)
        await asyncio.wait_for(paused.wait(), 2)
        assert event.replies_sent[0].endswith(PARAGRAPHS[0])
        await p.cmd_dynamics_stop(_stop_event(event, event.sender_id))
        release.set()
        if task:
            await asyncio.wait_for(task, 2)
        await drain(p)
        assert len(event.replies_sent) == 1
        assert bridge.commits == [PARAGRAPHS[0]]
        runtime = p._sessions[event.unified_msg_origin]
        assert _bot_texts(runtime) == [PARAGRAPHS[0]]
        assert not runtime.owned_turn_tasks and runtime.model_admission._value == 9
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await p.terminate()


@pytest.mark.asyncio
async def test_poke_persona_change_during_pause_cancels_unsent_paragraphs(jev_plugin):
    p, bridge = jev_plugin
    event = prepare_poke(p, bridge)

    async def switch_persona(_delay):
        bridge.persona = replace(bridge.persona, persona_id="switched")

    p.time_service.sleep = switch_persona
    try:
        await p.on_group_message(event)
        assert event.replies_sent == [PARAGRAPHS[0]]
        assert bridge.commits == [PARAGRAPHS[0]]
    finally:
        await p.terminate()


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.asyncio
async def test_native_host_head_and_owned_tails_send_natural_paragraphs(limit):
    p = _plugin({"base_thinking_delay": 0, "daily_rhythm_enabled": False, "max_fragments": limit})
    p.time_service.sleep = lambda delay: asyncio.sleep(0)
    network = []

    async def send(chain):
        network.append(chain_plain_text(chain))
        return f"native-{len(network)}"

    try:
        event = NativeEvent("小助手帮我看下这段回复", group_id=f"natural-paragraphs-{limit}",
                            message_id="native-trigger", is_at_or_wake_command=True)
        await p.on_group_message(event)
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.native_trigger_node is not None
        event.send = send
        event.set_result(build_plain_chain(REPLY))
        await p.on_decorating_result(event)
        await event.send(event.get_result())
        await p.after_message_sent(event)
        await _drain(p)
        expected = [*PARAGRAPHS[:limit - 1], "\n\n".join(PARAGRAPHS[limit - 1:])]
        assert network == expected
        assert _bot_texts(runtime) == expected
        assert not runtime.followup_queue
    finally:
        await p.terminate()
