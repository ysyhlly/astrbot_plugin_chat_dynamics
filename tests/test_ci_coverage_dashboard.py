"""Behavioral coverage for dashboard snapshots and native request hook dispatch.

Exercises real snapshot contracts (truncation, masking, task accounting,
read-air scoping, replay timelines) plus the lazy host-import branches of
core.native_request using injected astrbot.core module doubles.
"""

import asyncio
import json
import sys
import types
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_chat_dynamics.core import dashboard as dash
from astrbot_plugin_chat_dynamics.core import native_request
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin


# --------------------------------------------------------------------------- #
# dashboard helpers
# --------------------------------------------------------------------------- #


def test_attr_int_falls_back_on_unparseable_values():
    assert dash._attr_int(NS(quota="7"), "quota", 2) == 7
    assert dash._attr_int(NS(quota=None), "quota", 2) == 2
    assert dash._attr_int(NS(quota="lots"), "quota", 2) == 2
    assert dash._attr_int(NS(quota=["x"]), "quota", 2) == 2
    assert dash._attr_int(NS(missing=True), "absent", 3) == 3


def test_truncate_long_text_adds_ellipsis_and_short_text_passes_through():
    assert dash._truncate(None) == ""
    assert dash._truncate("  hi  ") == "hi"
    short = "短文本" * 30
    assert dash._truncate(short) == short
    long_text = "长" * 200
    trimmed = dash._truncate(long_text)
    assert len(trimmed) == 96
    assert trimmed.endswith("…")
    at_140 = dash._truncate(long_text, 140)
    assert len(at_140) == 140 and at_140.endswith("…")


def test_mask_identifier_keeps_only_tail_for_long_values():
    assert dash._mask_identifier("") == ""
    value = "bot-98765"
    assert dash._mask_identifier(value) == "*****8765"
    assert dash._mask_identifier(123456) == "**3456"


def test_companion_snapshot_reports_missing_when_bridge_absent():
    payload = dash.companion_snapshot(NS())
    assert payload == {
        "status": "missing",
        "lamp": "未安装",
        "weakened": [],
        "detail": "n/a",
        "enabled": False,
    }


def test_replay_lane_routes_codes_and_defaults_to_manners():
    assert dash.replay_lane("anything", action="speak") == "speak"
    assert dash.replay_lane("media_unsupported", "silent") == "media"
    assert dash.replay_lane("wind_down", "silent") == "rhythm"
    assert dash.replay_lane("", "silent") == "manners"
    assert dash.replay_lane("zzz_unlisted", "silent") == "manners"


def test_multimodal_lamp_covers_available_unavailable_and_unknown():
    gate = NS(media=NS(multimodal_available=lambda: True))
    assert dash._multimodal_lamp(NS(decision_gate=gate)) == {
        "lamp": "可用",
        "detail": "宿主可走多模态理解",
        "available": True,
    }
    gate = NS(media=NS(multimodal_available=lambda: False))
    assert dash._multimodal_lamp(NS(decision_gate=gate))["lamp"] == "不支持"
    gate = NS(media=NS(multimodal_available=Mock(side_effect=RuntimeError("probe"))))
    unknown = dash._multimodal_lamp(NS(decision_gate=gate))
    assert unknown["lamp"] == "未检测"
    assert unknown["available"] is None
    assert dash._multimodal_lamp(NS(decision_gate=None))["available"] is None


def test_session_or_none_resolves_known_and_rejects_unknown(monkeypatch):
    plugin = _plugin({"takeover_all": True})
    plugin._resolve_session_key = lambda sid: sid
    known = dash.snapshot_session_or_none(plugin, "missing")
    assert known is None

    plugin._get_or_create_dag("mask_room")
    monkeypatch.setattr(plugin, "_resolve_session_key", lambda sid: "mask_room")
    detail = dash.snapshot_session_or_none(plugin, "anything")
    assert detail is not None
    assert detail["session_key"] == "mask_room"


def test_redacted_session_masks_long_identifiers():
    plugin = _plugin({"takeover_all": True})
    dag = plugin._get_or_create_dag("mask_room")
    node = dag.add_message("bot_mask", "bot-98765", "内部内容不展示", timestamp=1.0)
    plugin._last_bot_nodes["mask_room"] = node
    detail = dash.snapshot_session_or_none(plugin, "mask_room")
    assert detail["content_redacted"] is True
    assert detail["last_bot_text"] == ""
    assert detail["last_bot_user_id"] == "*****8765"


def test_content_visible_session_truncates_bot_text_and_node_text():
    plugin = _plugin({"takeover_all": True, "console_show_message_content": True})
    dag = plugin._get_or_create_dag("long_room")
    plugin._last_bot_nodes["long_room"] = dag.add_message(
        "bot_long", "user_42", "长" * 200, timestamp=1.0
    )
    dag.add_message("user_1", "user_1", "问" * 200, timestamp=2.0, reply_to_id="bot_long")
    detail = dash.snapshot_session_or_none(plugin, "long_room")
    assert detail["content_redacted"] is False
    assert detail["last_bot_user_id"] == "user_42"
    bot_text = detail["last_bot_text"]
    assert len(bot_text) == 96 and bot_text.endswith("…")
    user_node = next(node for node in detail["nodes"] if node["msg_id"] == "user_1")
    assert len(user_node["text"]) == 140 and user_node["text"].endswith("…")


@pytest.mark.asyncio
async def test_overview_reports_active_background_tasks_for_cooling_and_sweep():
    plugin = _plugin({"takeover_all": True})

    async def busy():
        await asyncio.sleep(30)

    cooling = asyncio.create_task(busy())
    sweep = asyncio.create_task(busy())
    try:
        plugin._cooling_persist_task = cooling
        plugin._session_sweep_task = sweep
        data = dash.snapshot_overview(plugin)
        assert data["metrics"]["active_tasks"] >= 2
        assert data["active_tasks"] >= 2
    finally:
        cooling.cancel()
        sweep.cancel()
        for task in (cooling, sweep):
            try:
                await task
            except asyncio.CancelledError:
                pass


# --------------------------------------------------------------------------- #
# dashboard read-air summary
# --------------------------------------------------------------------------- #


def _air_rows():
    return [
        {
            "session_key": "room",
            "session_id": "room",
            "group_id": "g-room",
            "umo": "room",
            "sample_size": 4,
            "dag_nodes": 0,
            "mpm": 0.5,
            "mode": "focus",
            "occasion": {
                "kind": "tech_help",
                "reason_zh": "代码求助",
                "silence_bias": 0.8,
                "length_hint": 2,
            },
        },
        {
            "session_key": "other",
            "session_id": "other",
            "group_id": "g-other",
            "umo": "other",
            "sample_size": 6,
            "dag_nodes": 0,
            "mpm": 3.0,
            "mode": "chill",
            "occasion": {},
        },
    ]


def _air_plugin(gate):
    return NS(
        decision_gate=gate,
        presence_knob="sensible",
        daily_rhythm_enabled=True,
        _metrics={"speech_withheld": 2},
    )


def test_read_air_scopes_rows_and_builds_occasion_with_media_rows():
    stats_rows = [
        {"reason_code": "media_unsupported", "reason_zh": "媒体未就绪", "ts": 1.0},
        {"reason_code": "arbiter_wts", "reason_zh": "权重不足", "ts": 2.0},
        {"reason_code": "rhythm_sleep", "reason_zh": "作息安静", "ts": 3.0},
    ]
    rhythm_rows = [
        {"reason_code": "rhythm_sleep", "reason_zh": "作息安静", "ts": 3.0},
        {"reason_code": "goodnight_quota", "reason_zh": "晚安额度用尽", "ts": 4.0},
    ]
    gate = NS(
        manners=NS(
            today_stats=Mock(
                return_value={"intervene": 3, "quiet": 7, "why_silent": stats_rows}
            )
        ),
        useful=None,
        rhythm=NS(
            status=Mock(
                return_value={
                    "state": "asleep",
                    "state_zh": "已入睡",
                    "last_reason_zh": "夜深了",
                }
            ),
            why_silent_rows=Mock(return_value=rhythm_rows),
        ),
    )
    result = dash._read_air_summary(_air_plugin(gate), _air_rows(), session_key="room")

    assert result["session_key"] == "room"
    assert result["occasion"]["kind"] == "tech_help"
    assert result["confidence"] == 0.62
    assert result["intervene_count"] == 3
    assert result["quiet_count"] == 7
    assert result["thermometer"]["quiet_ratio"] == 0.7
    codes = [item["reason_code"] for item in result["why_silent"]]
    assert codes == ["media_unsupported", "arbiter_wts", "rhythm_sleep", "goodnight_quota"]
    assert [item["reason_code"] for item in result["media_why_silent"]] == [
        "media_unsupported"
    ]
    assert result["daily_rhythm"]["state"] == "asleep"
    assert "夜深了" in result["rhythm_summary"]


def test_read_air_bad_silence_bias_falls_back_to_0_6_confidence():
    rows = [
        {
            "session_key": "room",
            "session_id": "room",
            "group_id": "g",
            "umo": "room",
            "sample_size": 1,
            "dag_nodes": 0,
            "mpm": 0.2,
            "mode": "chill",
            "occasion": {"kind": "noisy", "reason_zh": "暂无热度信号", "silence_bias": "not-a-number"},
        }
    ]
    gate = NS(manners=None, useful=None, rhythm=None)
    result = dash._read_air_summary(_air_plugin(gate), rows, session_key="room")
    assert result["confidence"] == 0.6
    assert result["occasion"]["kind"] == "noisy"


def test_read_air_quota_and_rhythm_failures_fall_back_to_defaults():
    gate = NS(
        manners=NS(
            today_stats=Mock(
                return_value={"intervene": 0, "quiet": 1, "why_silent": []}
            )
        ),
        useful=NS(quota_status=Mock(side_effect=RuntimeError("quota down"))),
        rhythm=NS(
            status=Mock(side_effect=RuntimeError("rhythm down")),
            why_silent_rows=Mock(side_effect=RuntimeError("rows down")),
        ),
    )
    result = dash._read_air_summary(_air_plugin(gate), [], session_key="")
    assert result["proactive_used"] == 0
    assert result["proactive_cap"] == 2
    assert result["daily_rhythm"]["state"] == "awake"
    assert result["why_silent"] == []
    assert result["empty"] is True


# --------------------------------------------------------------------------- #
# dashboard scene replay timeline

def test_replay_topics_isolate_sessions_and_ambiguous_decisions():
    def node(topic, ts, text):
        return NS(metadata={"routing": {"topic_id": topic}}, thread_id=topic,
                  msg_id=topic, timestamp=ts, text=text)

    plugin = NS(dags={
        "room-a": NS(nodes={"a": node("same", 10, "讨论部署问题"),
                            "b": node("other", 20, "周末去哪玩")}),
        "room-b": NS(nodes={"a": node("same", 10, "另一群的内容")}),
    }, console_show_message_content=False)
    events = [{"session_id": "room-a", "ts": 11, "action": "speak"},
              {"session_id": "room-a", "ts": 21, "action": "silent"}]
    rows = dash.replay_topic_blocks(plugin, events, "room-a")
    assert len(rows) == 2
    assert all(row["session_id"] == "room-a" for row in rows)
    assert rows[0]["topic_title"] == "话题 1"
    assert rows[0]["events"][0]["association"] == "按时间关联"
    assert rows[-1]["events"] == []
    plugin.console_show_message_content = True
    assert dash.replay_topic_blocks(plugin, [], "room-a")[0]["topic_title"] == "讨论部署问题"


def test_replay_topics_convert_monotonic_nodes_to_wall_time():
    node = NS(metadata={"routing": {"topic_id": "topic", "topic_status": "committed"}},
              thread_id="topic", msg_id="m", timestamp=990, text="话题")
    plugin = NS(dags={"room": NS(nodes={"m": node})},
                time_service=NS(time=lambda: 1000, wall_time=lambda: 1800000000))
    event = {"session_id": "room", "ts": 1799999995, "action": "silent"}
    rows = dash.replay_topic_blocks(plugin, [event], "room")
    assert len(rows) == 1
    assert rows[0]["start_ts"] == 1799999990
    assert rows[0]["end_ts"] == 1799999995
    assert rows[0]["events"][0]["association"] == "按时间关联"
    assert node.timestamp == 990


def test_replay_leaves_unassigned_media_and_pending_messages_blank():
    nodes = {
        str(i): NS(metadata=metadata, thread_id="same-reply-thread", msg_id=str(i),
                   timestamp=i, text="[图片]")
        for i, metadata in enumerate([
            {}, {"routing": {"topic_id": None}},
            {"routing": {"topic_id": "UNKNOWN"}},
            {"routing": {"topic_id": "candidate", "topic_status": "pending"}},
        ])
    }
    plugin = NS(dags={"room": NS(nodes=nodes), "other": NS(nodes=nodes)})
    event = {"session_id": "room", "ts": 5, "action": "silent"}
    assert dash.replay_topic_blocks(plugin, [event], "room") == []
    snapshot = dash.scene_replay_snapshot(plugin, session_key="room")
    assert snapshot["topic_blocks"] == []
    assert snapshot["unassigned_message_count"] == 4


def test_unassigned_messages_prevent_false_temporal_event_association():
    nodes = {
        "a": NS(metadata={"routing": {"topic_id": "real", "topic_status": "committed"}},
                timestamp=1, msg_id="a", text="连续讨论"),
        "b": NS(metadata={"routing": {"topic_id": None}}, timestamp=2, msg_id="b", text="[图片]"),
    }
    rows = dash.replay_topic_blocks(NS(dags={"room": NS(nodes=nodes)}),
                                    [{"session_id": "room", "ts": 3, "action": "silent"}], "room")
    assert len(rows) == 1
    assert rows[0]["message_count"] == 1
    assert rows[0]["events"] == []
# --------------------------------------------------------------------------- #


def test_scene_replay_dedupes_and_merges_blocks(monkeypatch):
    monkeypatch.setattr(
        dash,
        "snapshot_sessions",
        Mock(
            return_value=[
                {"session_key": "room", "session_id": "room", "group_id": "g-room"}
            ]
        ),
    )
    scene = [
        {"ts": 1.0, "action": "silent", "reason_code": "media_gate", "reason_zh": "媒体"},
        {"ts": 1.0, "action": "silent", "reason_code": "media_gate", "reason_zh": "媒体"},
        {"ts": 2.0, "action": "silent", "reason_code": "media_voice", "reason_zh": "语音"},
    ]
    plugin = NS(
        decision_gate=NS(
            manners=NS(scene_track=Mock(return_value=scene)),
            rhythm=NS(
                why_silent_rows=Mock(
                    return_value=[
                        {
                            "ts": 3.0,
                            "reason_code": "wind_down",
                            "reason_zh": "夜深了",
                            "session_id": "room",
                        }
                    ]
                )
            ),
        )
    )
    result = dash.scene_replay_snapshot(plugin, session_key="room")
    assert result["session_key"] == "room"
    assert [event["ts"] for event in result["events"]] == [1.0, 2.0, 3.0]
    assert result["speak_count"] == 0
    assert result["silent_count"] == 3
    assert result["empty"] is False
    assert result["sessions"] == [
        {"session_key": "room", "session_id": "room", "group_id": "g-room"}
    ]
    blocks = result["blocks"]
    assert len(blocks) == 2
    assert blocks[0]["lane"] == "media"
    assert blocks[0]["count"] == 2
    assert [event["reason_zh"] for event in blocks[0]["events"]] == ["媒体", "语音"]
    assert blocks[1]["lane"] == "rhythm"
    assert blocks[1]["end_ts"] == 23.0
    assert result["content_redacted"] is True


def test_scene_replay_survives_rhythm_and_session_failures(monkeypatch):
    monkeypatch.setattr(
        dash, "snapshot_sessions", Mock(side_effect=RuntimeError("sessions down"))
    )
    plugin = NS(
        decision_gate=NS(
            manners=NS(scene_track=Mock(return_value=[])),
            rhythm=NS(why_silent_rows=Mock(side_effect=RuntimeError("rows down"))),
        )
    )
    result = dash.scene_replay_snapshot(plugin, session_key="room")
    assert result["events"] == []
    assert result["sessions"] == []
    assert result["empty"] is True


# --------------------------------------------------------------------------- #
# core.native_request host hook dispatch
# --------------------------------------------------------------------------- #

FAKE_HOST_MODULES = (
    "astrbot.core.pipeline.context_utils",
    "astrbot.core.provider.entities",
    "astrbot.core.star.star_handler",
)


class FakeProviderRequest:
    def __init__(self, prompt="", image_urls=(), audio_urls=()):
        self.prompt = prompt
        self.image_urls = list(image_urls)
        self.audio_urls = list(audio_urls)
        self.extra_user_content_parts = []


class FakeEventType:
    OnLLMRequestEvent = "OnLLMRequestEvent"


class FakeEvent:
    def __init__(self, extras=None):
        self.continue_calls = 0
        if extras is not None:
            self._extras = extras

    def continue_event(self):
        self.continue_calls += 1


def install_host(monkeypatch, hook=None, provider=FakeProviderRequest):
    context_utils = types.ModuleType("astrbot.core.pipeline.context_utils")
    context_utils.call_event_hook = hook if hook is not None else AsyncMock(return_value=False)
    entities = types.ModuleType("astrbot.core.provider.entities")
    entities.ProviderRequest = provider
    star_handler = types.ModuleType("astrbot.core.star.star_handler")
    star_handler.EventType = FakeEventType
    monkeypatch.setitem(sys.modules, FAKE_HOST_MODULES[0], context_utils)
    monkeypatch.setitem(sys.modules, FAKE_HOST_MODULES[1], entities)
    monkeypatch.setitem(sys.modules, FAKE_HOST_MODULES[2], star_handler)
    return context_utils.call_event_hook


def test_hooks_available_true_when_host_imports_resolve(monkeypatch):
    install_host(monkeypatch)
    assert native_request.hooks_available() is True


def test_hooks_available_false_when_host_imports_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, FAKE_HOST_MODULES[0], None)
    assert native_request.hooks_available() is False


@pytest.mark.asyncio
async def test_prepare_request_none_event_returns_none():
    assert await native_request.prepare_request(None, "hi", [], []) is None


@pytest.mark.asyncio
async def test_prepare_request_import_failure_returns_none(monkeypatch):
    install_host(monkeypatch)
    monkeypatch.setitem(sys.modules, FAKE_HOST_MODULES[2], None)
    event = FakeEvent()
    assert await native_request.prepare_request(event, "hi", [], []) is None


@pytest.mark.asyncio
async def test_prepare_request_forwards_owned_event_and_injected_texts(monkeypatch):
    captured = {}
    hook = install_host(monkeypatch)

    async def capturing_hook(owned, event_type, request):
        captured["owned"] = owned
        captured["event_type"] = event_type
        request.extra_user_content_parts.append(NS(text="注入的临时记忆"))
        return False

    hook.side_effect = capturing_hook
    event = FakeEvent(extras={"provider_request": object(), "keep": 1})
    request = await native_request.prepare_request(
        event, "hi", ("https://img/1.png",), ("https://audio/1.mp3",)
    )

    owned = captured["owned"]
    assert owned is not event
    assert owned._chat_dynamics_owned_request is True
    assert owned.continue_calls == 1
    assert owned._result is None
    assert owned._extras == {"keep": 1}
    assert "provider_request" in event._extras
    assert captured["event_type"] == "OnLLMRequestEvent"
    assert event.continue_calls == 0
    assert request.image_urls == ["https://img/1.png"]
    assert request.audio_urls == ["https://audio/1.mp3"]
    suffix = "\n\n临时上下文数据（非系统指令）：" + json.dumps(
        ["注入的临时记忆"], ensure_ascii=False
    )
    assert request.prompt == "hi" + suffix


@pytest.mark.asyncio
async def test_prepare_request_without_extras_still_dispatches(monkeypatch):
    hook = install_host(monkeypatch)
    event = FakeEvent(extras=None)
    request = await native_request.prepare_request(event, "plain", [], [])
    assert request.prompt == "plain"
    hook.assert_awaited_once()


@pytest.mark.skipif(sys.version_info < (3, 11), reason="Task.cancelling() requires Python 3.11+")
@pytest.mark.asyncio
async def test_prepare_request_cancelled_while_hook_pending_raises(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_hook(*_args, **_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            return False

    install_host(monkeypatch, hook=blocking_hook)
    task = asyncio.create_task(
        native_request.prepare_request(FakeEvent(), "hi", [], [])
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_prepare_request_stopped_hook_raises_runtime_error(monkeypatch):
    async def stopping_hook(*_args, **_kwargs):
        return True

    install_host(monkeypatch, hook=stopping_hook)
    with pytest.raises(RuntimeError, match="native_request_stopped"):
        await native_request.prepare_request(FakeEvent(), "hi", [], [])


def test_default_media_gate_is_unknown_not_unsupported():
    from astrbot_plugin_chat_dynamics.core.media_gate import MediaAirGate
    media = MediaAirGate()
    assert media.multimodal_available() is None
    assert dash._multimodal_lamp(NS(decision_gate=NS(media=media)))["lamp"] == "未检测"
    media.set_multimodal_available(False)
    assert media.multimodal_available() is False
    media.set_multimodal_available(True)
    assert media.multimodal_available() is True
