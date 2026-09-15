"""Periodic drafts use the real plugin, review, storage and lifecycle paths."""
import asyncio
import json

import pytest
import pytest_asyncio

from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from .test_plugin_lifecycle import _plugin, _session_key


class DraftModel:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None
        self.fail_sessions = set()

    def configure(self, *args, **kwargs):
        pass

    def configured_provider(self, purpose="draft"):
        return "test-draft"

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if kwargs["umo"] in self.fail_sessions:
            raise RuntimeError("model failed")
        prompt = kwargs["prompt"]
        data, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])
        return json.dumps({"rows": [
            {"msg_id": row["msg_id"], "expected_reply": True, "bot_targeted": False,
             "confidence": 0.8, "reason": "question"}
            for row in data["messages"] if row["draft_this"]
        ]})


@pytest_asyncio.fixture
async def host():
    plugin = _plugin({"annotation_draft_enabled": True, "annotation_draft_auto_enabled": True,
                      "annotation_draft_interval_minutes": 1}, clock=VirtualClock())
    plugin.llm = DraftModel()
    yield plugin
    await plugin.terminate()


def add_session(host, group="allowed", msg_id="m1"):
    key = _session_key(group)
    runtime = host._get_or_create_runtime(key, group_id=group, umo=key)
    runtime.bot_id = "bot"
    host.dags[key].add_message(msg_id, "human", "Could you help?", timestamp=host.time_service.time())
    return key


@pytest.mark.asyncio
async def test_timer_waits_full_interval_and_repeated_scan_does_not_call_model(host):
    key = add_session(host)
    host._annotation_scheduler.start()
    await host.time_service.advance(59)
    assert host.llm.calls == []
    await host.time_service.advance(1)
    assert len(host.llm.calls) == 1
    assert "m1" in (await host.topic_annotations.read_drafts(key))["drafts"]
    await host.time_service.advance(120)
    assert len(host.llm.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["enabled", "annotation_draft_enabled", "annotation_draft_auto_enabled"])
async def test_each_switch_disables_automatic_generation(host, switch):
    add_session(host)
    setattr(host, switch, False)
    host._annotation_scheduler.start()
    await host.time_service.advance(120)
    await host._annotation_scheduler.run_once()
    assert host.llm.calls == []


@pytest.mark.asyncio
async def test_automatic_generation_obeys_group_scope_and_bot_identity(host):
    allowed = add_session(host)
    add_session(host, "excluded")
    add_session(host, "outside")
    unidentified = add_session(host, "unidentified")
    host._sessions[unidentified].bot_id = ""
    host.takeover_all = False
    host.takeover_groups = {"allowed", "excluded", "unidentified"}
    host.exclude_groups = {"excluded"}
    await host._annotation_scheduler.run_once()
    assert [call["umo"] for call in host.llm.calls] == [allowed]


@pytest.mark.asyncio
async def test_new_messages_append_drafts_without_replacing_old_rows(host):
    key = add_session(host)
    await host._annotation_scheduler.run_once()
    first = (await host.topic_annotations.read_drafts(key))["drafts"]["m1"]
    add_session(host, msg_id="m2")
    await host._annotation_scheduler.run_once()
    drafts = (await host.topic_annotations.read_drafts(key))["drafts"]
    assert set(drafts) == {"m1", "m2"}
    assert drafts["m1"] == first
    assert len(host.llm.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic_first", [True, False])
async def test_manual_and_automatic_generation_share_busy_guard(host, automatic_first):
    key = add_session(host)
    host.llm.release = asyncio.Event()
    first = asyncio.create_task(host._annotation_scheduler.generate(key, automatic=automatic_first))
    await asyncio.wait_for(host.llm.entered.wait(), 1)
    try:
        result = await host._annotation_scheduler.generate(key, automatic=not automatic_first)
        assert result["state"] == "busy"
        assert len(host.llm.calls) == 1
    finally:
        host.llm.release.set()
        await first


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["disable", "reset", "evict", "terminate"])
async def test_lifecycle_changes_cancel_inflight_generation_without_persisting(host, action):
    key = add_session(host)
    host.llm.release = asyncio.Event()
    host._annotation_scheduler.start()
    generation = asyncio.create_task(host._annotation_scheduler.generate(key, automatic=True))
    await asyncio.wait_for(host.llm.entered.wait(), 1)
    if action == "disable":
        host.config["annotation_draft_auto_enabled"] = False
        host.refresh_config()
    elif action == "reset":
        host._reset_session_state(key)
    elif action == "evict":
        host._drop_session(key)
    else:
        await host.terminate()
    result = await asyncio.wait_for(generation, 1)
    assert result["state"] == "cancelled"
    assert not (await host.topic_annotations.read_drafts(key)).get("drafts")
    assert not host._annotation_scheduler.jobs
    if action in {"disable", "terminate"}:
        assert host._annotation_scheduler.loop_task is None or host._annotation_scheduler.loop_task.done()


@pytest.mark.asyncio
async def test_model_failure_does_not_prevent_later_session_drafts(host):
    failed = add_session(host, "failed")
    succeeding = add_session(host, "succeeding")
    host.llm.fail_sessions.add(failed)
    await host._annotation_scheduler.run_once()
    assert [call["umo"] for call in host.llm.calls] == [failed, succeeding]
    assert "m1" in (await host.topic_annotations.read_drafts(succeeding))["drafts"]


@pytest.mark.asyncio
async def test_ignored_drafts_are_not_generated_again(host):
    key = add_session(host)
    await host._annotation_scheduler.run_once()
    await host.annotation_drafts_apply({"action": "dismiss", "session_key": key, "msg_ids": ["m1"]})
    await host._annotation_scheduler.run_once()
    assert len(host.llm.calls) == 1
    assert not (await host.topic_annotations.read_drafts(key))["drafts"]


@pytest.mark.asyncio
async def test_manual_annotation_is_not_submitted_for_automatic_drafting(host):
    from .test_topic_annotations import label
    key = add_session(host)
    await host.topic_annotations.save(label(session=key, msg_id="m1"))
    await host._annotation_scheduler.run_once()
    assert host.llm.calls == []
    assert not (await host.topic_annotations.read_drafts(key)).get("drafts")


@pytest.mark.asyncio
async def test_clearing_rejects_late_result_even_if_provider_ignores_cancellation(host):
    key = add_session(host)
    host.llm.release = asyncio.Event()
    original = host.llm.generate

    async def stubborn(**kwargs):
        try:
            return await original(**kwargs)
        except asyncio.CancelledError:
            return json.dumps({"rows": [{"msg_id": "m1", "expected_reply": True}]})

    host.llm.generate = stubborn
    generation = asyncio.create_task(host.annotation_draft_payload(key))
    await asyncio.wait_for(host.llm.entered.wait(), 1)
    await host.annotation_drafts_apply({"action": "clear_session", "session_key": key})
    assert (await asyncio.wait_for(generation, 1))["state"] == "cancelled"
    assert not (await host.topic_annotations.read_drafts(key)).get("drafts")


@pytest.mark.asyncio
async def test_human_label_written_during_model_call_is_not_replaced_by_draft(host):
    from .test_topic_annotations import label
    key = add_session(host)
    host.llm.release = asyncio.Event()
    generation = asyncio.create_task(host.annotation_draft_payload(key))
    await asyncio.wait_for(host.llm.entered.wait(), 1)
    await host.topic_annotations.save(label(session=key, msg_id="m1"))
    host.llm.release.set()
    result = await asyncio.wait_for(generation, 1)
    assert result["drafted"] == 0
    assert not result["drafts"]
    assert len((await host.topic_annotations.read(key))["records"]) == 1
