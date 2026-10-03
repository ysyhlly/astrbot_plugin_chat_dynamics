"""Regressions for the complete ingress, generation and delivery audit."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, _plugin
from .test_member_stop import NativeEvent, _stop_event, _bot_texts
from .test_paragraph_sending import prepare_poke
from .test_topic_annotations import fixture_plugin, label
from astrbot_plugin_chat_dynamics.core.platform_bridge import build_plain_chain, chain_plain_text
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore
from astrbot_plugin_chat_dynamics.core import mood_memory
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.core.media_gate import MediaAirGate
from astrbot_plugin_chat_dynamics.core.deferred_media import preserve_event_media, release_event_media

@pytest.mark.asyncio
@pytest.mark.parametrize('field,new_value', [('reply_provider', 'replacement-provider'), ('reply_prompt', '请使用新的回复规则')])
async def test_old_generation_must_not_send_after_reply_config_change(jev_plugin, field, new_value):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    async def block():
        entered.set()
        await release.wait()
    bridge.before_reply = block
    event = MockEvent("帮我处理这条请求", message_id="audit-config", components=[At("bot_42")])
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await asyncio.wait_for(entered.wait(), 2)
        p.config[field] = new_value
        p._sync_runtime_from_config()
        release.set()
        await drain(p)
        assert event.replies_sent == [], f"old configuration still sent {event.replies_sent!r}"
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_new_request_after_config_change_does_not_inherit_stale_fragments(jev_plugin):
    p, bridge = jev_plugin
    old = MockEvent("旧配置下的待处理内容", message_id="old-buffer")
    new = MockEvent("帮我回答新的请求", message_id="new-config", components=[At("bot_42")])
    try:
        await p.on_group_message(old)
        assert p.debounce.get_pending_count() == 1
        p.config["reply_prompt"] = "新的规则"
        p._sync_runtime_from_config()
        await p.on_group_message(new)
        await flush(p, new)
        await drain(p)
        assert new.replies_sent and not old.replies_sent
        assert "旧配置下" not in bridge.requests[-1][0]["conversation"]["text"]
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_poke_honors_configured_three_second_interval(jev_plugin):
    p, bridge = jev_plugin
    p.config["inter_burst_interval"] = 3.0
    p._sync_runtime_from_config()
    event = prepare_poke(p, bridge)
    delays = []
    async def record(delay):
        delays.append(delay)
        await asyncio.sleep(0)
    p.time_service.sleep = record
    try:
        await p.on_group_message(event)
        assert len(event.replies_sent) == 3
        assert delays == [3.0, 3.0], delays
    finally:
        await p.terminate()


def test_successful_forget_survives_restart(tmp_path, monkeypatch):
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.remember("audit-room", "peer", ["工作压力"])
    assert store.recall("audit-room", "peer")
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(mood_memory, "atomic_write_json", fail)
    with pytest.raises(OSError, match="disk full"):
        store.forget("audit-room", "peer")
    assert store.recall("audit-room", "peer")
    restarted = MoodMemoryStore(tmp_path)
    restarted.configure(enabled=True)
    assert restarted.recall("audit-room", "peer")

@pytest.mark.asyncio
async def test_terminate_cancels_owned_work_even_when_connector_close_fails(jev_plugin):
    p, bridge = jev_plugin
    entered, release = asyncio.Event(), asyncio.Event()
    async def block():
        entered.set()
        await release.wait()
    bridge.before_reply = block
    event = MockEvent("帮我处理这条请求", message_id="audit-terminate", components=[At("bot_42")])
    original = p.selflearning.close
    async def fail():
        raise RuntimeError("connector failed to close")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await asyncio.wait_for(entered.wait(), 2)
        running = [task for task in p._background_tasks if not task.done()]
        assert running
        p.selflearning.close = fail
        with pytest.raises(RuntimeError):
            await p.terminate()
        await asyncio.sleep(0)
        assert all(task.done() for task in running), [task.get_name() for task in running if not task.done()]
    finally:
        p.selflearning.close = original
        release.set()
        await p.terminate()

@pytest.mark.asyncio
async def test_due_reminder_is_delivered_and_then_visible_in_next_reply(jev_plugin, tmp_path):
    p, bridge = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    event = MockEvent("帮我看看今天有没有安排", message_id="audit-reminder", components=[At("bot_42")])
    marker = "AUDIT_REMINDER_请带会议材料"
    p.group_memory.add_reminder(event.unified_msg_origin, text=marker, due_at=p.time_service.wall_time() - 1)
    delivered = []
    async def send(umo, chain):
        delivered.append((umo, chain_plain_text(chain)))
        return "reminder-sent"
    p.context.send_message = send
    try:
        await p.reminders.dispatch()
        assert delivered == [(event.unified_msg_origin, "提醒：" + marker)]
        assert p.group_memory.due_reminders(event.unified_msg_origin) == []
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bridge.requests
        assert marker in json.dumps([request[0] for request in bridge.requests], ensure_ascii=False)
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_cooling_invalidates_buffered_old_mentions(jev_plugin):
    p, bridge = jev_plugin
    event = MockEvent("帮我处理这条请求", message_id="audit-cool-buffer", components=[At("bot_42")])
    try:
        await p.on_group_message(event)
        assert not bridge.requests
        assert await p._cool_session_async(event.unified_msg_origin, 15)
        await flush(p, event)
        await drain(p)
        assert event.replies_sent == [], event.replies_sent
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_anniversary_fractional_date_is_rejected(jev_plugin, tmp_path):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    try:
        with pytest.raises(ValueError):
            p.notebook_mutate("add_anniversary", {"umo": "audit-date", "title": "生日", "month": 3.9, "day": 14.9})
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_two_annotation_editors_do_not_silently_overwrite(jev_plugin):
    plugin, _ = fixture_plugin()
    store = TopicAnnotations(plugin)
    initial = await store.save(label(expected_topic="CORRECT"))
    revision = initial["revision"]
    assert initial["saved"]
    await store.save(label(expected_topic="UNKNOWN", error_type="premature_assignment"))
    # The page retains the token from the editor that was loaded earlier.
    with pytest.raises(ValueError, match="修改"):
        await store.save(label(expected_topic="NEW", error_type="topic_merge", expected_revision=revision))

@pytest.mark.asyncio
@pytest.mark.parametrize('kind,key', [('Image', 'media_image_gate_enabled'), ('Record', 'media_voice_gate_enabled')])
async def test_disabling_media_gate_keeps_addressed_media(jev_plugin, kind, key):
    p, bridge = jev_plugin
    p.config[key] = False
    p._sync_runtime_from_config()
    component = type(kind, (), {"url": "https://example.invalid/audit-media"})()
    event = MockEvent("帮我分析这个附件", message_id="audit-media-off", components=[At("bot_42"), component])
    flags = []
    generate = bridge.generate
    async def capture(*args, **kwargs):
        flags.append(kwargs.get("media_understand"))
        return await generate(*args, **kwargs)
    bridge.generate = capture
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert flags == [True], flags
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_native_strict_privacy_does_not_forward_sensitive_image():
    p = _plugin({"decision_mode": "legacy", "base_thinking_delay": 0})
    image = type("Image", (), {"url": "https://example.invalid/passport"})()
    event = NativeEvent("请帮我看看这张身份证", message_id="audit-native-private", components=[At("bot_42"), image])
    request = SimpleNamespace(prompt=event.message_str, image_urls=[image.url], audio_urls=[], extra_user_content_parts=[])
    try:
        await p.on_group_message(event)
        runtime = p._sessions[event.unified_msg_origin]
        assert runtime.last_media_gate["privacy_hit"] is True
        assert runtime.request_media_understand is False
        assert event.call_llm is True, "host call_llm=True suppresses default preprocessing"
        await p.on_llm_request(event, request)
        assert event.is_stopped or not request.image_urls, request.image_urls
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_legacy_owned_privacy_ack_does_not_read_or_forward_media():
    from astrbot_plugin_chat_dynamics.core.platform_bridge import iter_message_components
    p = _plugin({"base_thinking_delay": 0, "daily_rhythm_enabled": False})
    image = type("Image", (), {"url": "https://example.invalid/passport"})()
    event = NativeEvent("请帮我看看这张身份证", message_id="private-owned", components=[At("bot_42"), image])
    calls = []
    async def agent(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text="这张图涉及隐私，请先遮住敏感内容。")
    p.context.tool_loop_agent = agent
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert calls and event.replies_sent
        assert not calls[0].get("image_urls") and not calls[0].get("audio_urls")
        assert all(type(part).__name__ != "Image" for part in iter_message_components(calls[0]["event"]))
        assert event.message_obj.message[-1] is image
    finally:
        await p.terminate()

@pytest.mark.asyncio
async def test_native_successful_tail_is_recorded_even_if_stopped_during_send():
    p = _plugin({"decision_mode": "legacy", "base_thinking_delay": 0})
    event = NativeEvent("小助手帮我看下这段回复", message_id="audit-tail-stop", is_at_or_wake_command=True)
    entered, release = asyncio.Event(), asyncio.Event()
    network = []
    task = None
    async def send(chain):
        text = chain_plain_text(chain)
        if text == "第二段":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A platform API may finish delivery while cancellation arrives.
                pass
        network.append(text)
        return f"delivered-{len(network)}"
    async def sleep(delay):
        await asyncio.sleep(0)
    event.send = send
    p.time_service.sleep = sleep
    try:
        await p.on_group_message(event)
        runtime = p._sessions[event.unified_msg_origin]
        event.set_result(build_plain_chain("第一段\n\n第二段\n\n第三段"))
        await p.on_decorating_result(event)
        await event.send(event.get_result())
        task = asyncio.create_task(p.after_message_sent(event))
        await asyncio.wait_for(entered.wait(), 2)
        await p.cmd_dynamics_stop(_stop_event(event, event.sender_id, "audit-stop-tail"))
        release.set()
        await asyncio.wait_for(task, 2)
        assert network == ["第一段", "第二段"]
        assert _bot_texts(runtime) == network, _bot_texts(runtime)
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await p.terminate()

@pytest.mark.asyncio
async def test_name_addressed_reply_honors_reply_timeout(jev_plugin):
    p, bridge = jev_plugin
    # Scaled deadlines exercise the same selection without a five-second test.
    p._runtime_config = replace(p._runtime_config, reply_timeout=.03, tool_agent_timeout=.3)
    async def slow():
        await asyncio.sleep(.09)
    bridge.before_reply = slow
    event = MockEvent("小助手帮我处理这条请求", message_id="audit-name-timeout", is_at_or_wake_command=True)
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert bridge.requests and p.jev.calls
        assert bridge.response not in event.replies_sent, event.replies_sent
    finally:
        await p.terminate()


@pytest.mark.parametrize("addressed", [False, True])
def test_l1_disabled_still_enforces_image_privacy(addressed):
    gate = MediaAirGate()
    verdict = gate.evaluate(text="请看看身份证", has_media=True, media_component_types=["Image"],
                            explicit=addressed, media_image_gate_enabled=False)
    assert verdict.privacy_hit and not verdict.request_understand
    assert verdict.allow_speak is addressed


def test_disabling_only_image_gate_does_not_silence_mixed_helpful_media():
    verdict = MediaAirGate().evaluate(text="帮忙看看", has_media=True,
        media_component_types=["Image", "Record"], explicit=True, media_image_gate_enabled=False)
    assert verdict.request_understand


def test_media_copy_preserves_original_and_owns_only_its_copy(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"audio")
    record = type("Record", (), {"file": path.as_uri(), "path": str(path), "url": ""})()
    event = MockEvent("听听", components=[record])
    event._temporary_local_files = [str(path)]
    owned = preserve_event_media(event)
    copied = owned.message_obj.message[0]
    assert copied is not record and copied.path != record.path
    assert owned._temporary_local_files == []
    assert event._temporary_local_files == [str(path)]
    from pathlib import Path
    assert Path(copied.path).read_bytes() == path.read_bytes()
    release_event_media(owned)
    assert path.exists() and not Path(copied.path).exists()


@pytest.mark.asyncio
async def test_waiting_media_survives_until_followup_completes(jev_plugin, tmp_path):
    from .test_jev_decision_layer import answers
    p, bridge = jev_plugin
    path = tmp_path / "voice.wav"
    path.write_bytes(b"audio")
    record = type("Record", (), {"file": str(path), "path": str(path), "url": ""})()
    first = MockEvent("小助手先听我说，我想问", message_id="media-wait", components=[record],
                      is_at_or_wake_command=True)
    second = MockEvent("就是这个问题，能帮我看看吗", message_id="media-complete")
    p.jev.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": .9})
    try:
        await p.on_group_message(first)
        await flush(p, first)
        await drain(p)
        lease = next(lease for lease in p._media_leases if lease.alive)
        path.unlink()
        assert not bridge.requests and lease.alive
        p.jev.payload = answers()
        await p.on_group_message(second)
        await flush(p, second)
        await drain(p)
        assert bridge.requests and not lease.alive
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_unload_releases_buffered_media(jev_plugin, tmp_path):
    p, _ = jev_plugin
    path = tmp_path / "voice.wav"
    path.write_bytes(b"audio")
    record = type("Record", (), {"file": str(path), "url": ""})()
    event = MockEvent("还有一些话", message_id="media-unload", components=[record])
    await p.on_group_message(event)
    lease = next(lease for lease in p._media_leases if lease.alive)
    await p.terminate()
    assert not lease.alive and path.exists()


def test_mood_mute_failed_write_does_not_change_cache(tmp_path, monkeypatch):
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.remember("room", "peer", ["工作压力"])
    original = store._load("room")
    original["peers"].clear()
    assert store.recall("room", "peer"), "readers cannot mutate the cache"
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(mood_memory, "atomic_write_json", fail)
    with pytest.raises(OSError):
        store.mute_tonight("room")
    assert store._load("room")["mute_until"] == 0
    assert MoodMemoryStore(tmp_path)._load("room")["mute_until"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["shadow", "scope", "mute", "cooling", "disabled"])
async def test_due_reminders_respect_delivery_policies(jev_plugin, tmp_path, restriction):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    event = MockEvent("", group_id="room")
    await p.on_group_message(event)
    umo = event.unified_msg_origin
    row = p.group_memory.add_reminder(umo, text="提醒事项", due_at=0)
    delivered = []
    async def send(umo, chain):
        delivered.append(chain)
        return True
    p.context.send_message = send
    try:
        if restriction == "shadow":
            p.config["shadow_mode"] = True
            p._sync_runtime_from_config()
        elif restriction == "scope":
            p.config["exclude_groups"] = ["room"]
            p._sync_runtime_from_config()
        elif restriction == "mute":
            p.group_memory.mute_tonight(umo)
        elif restriction == "cooling":
            p.arbiter.trigger_cooling(umo, duration_seconds=60, current_time=p.time_service.time())
        else:
            p.group_memory.enabled = False
        await p.reminders.dispatch()
        assert not delivered
        assert not p.group_memory.list_all(umo)["reminders"][0]["nudged"]
        assert p.group_memory.list_all(umo)["reminders"][0]["id"] == row["id"]
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_reminder_send_failure_retries_and_restart_does_not_duplicate(jev_plugin, tmp_path):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    umo = "mock:GroupMessage:room"
    row = p.group_memory.add_reminder(umo, text="请带材料", due_at=0)
    attempts = []
    async def send(umo, chain):
        attempts.append(chain_plain_text(chain))
        return False if len(attempts) == 1 else "delivered"
    p.context.send_message = send
    try:
        await p.reminders.dispatch()
        assert p.group_memory.due_reminders(umo)[0]["id"] == row["id"]
        await p.reminders.dispatch()
        p.group_memory = GroupMemoryNotebook(tmp_path)
        await p.reminders.dispatch()
        assert attempts == ["提醒：请带材料"] * 2
        assert p.group_memory.list_all(umo)["reminders"][0]["nudged"]
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_reminder_ack_failure_does_not_resend_in_same_process(jev_plugin, tmp_path, monkeypatch):
    from astrbot_plugin_chat_dynamics.core import group_memory
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    umo = "mock:GroupMessage:room"
    p.group_memory.add_reminder(umo, text="请带材料", due_at=0)
    attempts = []
    async def send(umo, chain):
        attempts.append(chain)
        return True
    p.context.send_message = send
    original = group_memory.atomic_write_json
    def fail(path, data):
        if any(row.get('nudged') for row in data.get('reminders', [])):
            raise OSError("disk full")
        return original(path, data)
    monkeypatch.setattr(group_memory, "atomic_write_json", fail)
    try:
        await p.reminders.dispatch()
        await p.reminders.dispatch()
        assert len(attempts) == 1 and p.reminders.delivered
        monkeypatch.setattr(group_memory, "atomic_write_json", original)
        await p.reminders.dispatch()
        assert len(attempts) == 1 and not p.reminders.delivered
        assert not p.group_memory.due_reminders(umo)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_reminder_read_does_not_consume_and_timer_dispatches_without_new_message(jev_plugin, tmp_path):
    p, _ = jev_plugin
    p.group_memory = GroupMemoryNotebook(tmp_path)
    umo = "mock:GroupMessage:room"
    delivered = asyncio.Event()
    async def send(umo, chain):
        delivered.set()
        return True
    p.context.send_message = send
    try:
        p.group_memory.add_reminder(umo, text="提醒", due_at=0)
        assert p.notebook_mutate("due_reminders", {"umo": umo})["items"]
        assert p.notebook_mutate("due_reminders", {"umo": umo})["items"]
        p.reminders.start()
        await asyncio.wait_for(delivered.wait(), 1)
        await asyncio.sleep(0)
        assert not p.group_memory.due_reminders(umo)
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_annotation_revision_survives_web_redaction(monkeypatch, offline_web_responses):
    from astrbot_plugin_chat_dynamics.core import web_api
    plugin, _ = fixture_plugin()
    api = web_api.ConsoleWebAPI(plugin)
    saved = await api.topic_annotations.save(label(recipient_ids=["person-private"]))
    monkeypatch.setattr(web_api, "_query_param", lambda key: "a")
    response = await api.annotations_get()
    data = response["data"]
    assert data["records"][0]["recipient_ids"] == []
    assert data["revisions"]["m"] == saved["revision"]
    await api.topic_annotations.save(label(expected_revision=data["revisions"]["m"]))
    with pytest.raises(ValueError, match="修改"):
        await api.topic_annotations.save(label(expected_revision=data["revisions"]["m"]))
