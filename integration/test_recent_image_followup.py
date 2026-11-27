"""Exercise passive image follow-ups through the real SDK with an offline provider."""

import gc
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image as PILImage

from .test_persona_agent import Event, host_fixture as host_fixture
from astrbot.core.message.components import Image, Plain, Reply
from astrbot_plugin_chat_dynamics.core import member_boundary
from astrbot_plugin_chat_dynamics.core.agent_bridge import AstrBotAgentBridge
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.media_archive import MediaArchive
from astrbot_plugin_chat_dynamics.core.recent_images import RecentImages, MAX_ENTRIES
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from astrbot_plugin_chat_dynamics.tests.test_jev_decision_layer import answers, jev_plugin as jev_plugin
from astrbot_plugin_chat_dynamics.tests.test_persona_model import flush, drain


@pytest.mark.asyncio
async def test_silent_image_reaches_next_text_reply_without_becoming_current_request(
    host_fixture, jev_plugin, tmp_path
):
    ctx, event, calls = host_fixture
    p, _ = jev_plugin
    p.media_archive = MediaArchive(tmp_path / "archive")
    p.persona_engine.bridge = AstrBotAgentBridge(ctx)
    path = tmp_path / "two.png"
    PILImage.new("RGB", (2, 1), (200, 30, 20)).save(path)
    event.message_obj.message_id = "picture"
    event.message_obj.message = [Image.fromFileSystem(str(path))]
    event.message_str = "[图片]"
    p.jev.payload = answers(action="ignore", reason="low_value_chatter")
    try:
        await p.on_group_message(event)
        await flush(p, event)
        await drain(p)
        assert not calls and not event.sent
        path.unlink()  # The host may clean its file after the silent turn.
        p.jev.payload = answers(action="acknowledge", state="casual", reason="social_signal")
        followup = Event()
        followup.message_obj.message_id = "followup"
        followup.message_obj.message = [Plain("两只小可爱我都吃")]
        followup.message_str = "两只小可爱我都吃"
        await p.on_group_message(followup)
        await flush(p, followup)
        await drain(p)
        assert calls and followup.sent
        messages = [m if isinstance(m, dict) else m.model_dump() for m in calls[0]["contexts"]]
        current = [m for m in messages if m.get("role") == "user"][-1]
        content = current["content"]
        assert any(part.get("type") == "image_url" for part in content)
        texts = [part["text"] for part in content if part.get("type") == "text"]
        payload = next(json.loads(text) for text in texts if '"response_plan"' in text)
        assert payload["conversation"]["text"] == "两只小可爱我都吃"
        assert [m["message_id"] for m in payload["conversation"]["messages"]] == ["followup"]
        assert payload["recent_image_context"][0]["source_message_id"] == "picture"
        assert payload["response_plan"]["action"] == "acknowledge"
        assert "[群聊基本约定]" in json.dumps(messages, ensure_ascii=False)
        saved = json.loads(ctx.conv.history)
        assert any(
            "Earlier same-speaker image context" in json.dumps(row) and "picture" in json.dumps(row)
            for row in saved
            if row.get("role") == "user"
        )
        assert p._sessions[event.unified_msg_origin].model_diagnostic["reason_code"] != "recent_image_context"
    finally:
        await p.terminate()


@pytest.fixture
def recent_fixture(tmp_path):
    event = Event()
    path = tmp_path / "source.png"
    PILImage.new("RGB", (2, 1), "red").save(path)
    event.message_obj.message = [Image.fromFileSystem(str(path))]
    event._chat_dynamics_boundary_token = None
    cache = RecentImages()
    archive = MediaArchive(tmp_path / "archive")
    dag = ConversationDAG()
    node = dag.add_message("picture", "u", "[图片]", timestamp=10)
    cache.remember(event, node, archive, 10)
    dag.add_message("text", "u", "两只小可爱我都吃", timestamp=24)
    runtime = NS(scope_kind="group", dag=dag)
    background = (MessageSnapshot("picture", "u", "[图片]", timestamp=10),)
    turn = TurnContext(
        event.unified_msg_origin,
        "u",
        "两只小可爱我都吃",
        (MessageSnapshot("text", "u", "两只小可爱我都吃", timestamp=24),),
        background,
        0,
        0,
        24,
        False,
        bot_id="bot",
        visible_node_ids=frozenset({"picture", "text"}),
    )
    return cache, archive, runtime, turn, event


@pytest.mark.parametrize(
    "case",
    [
        "expired",
        "other_author",
        "other_session",
        "later_image",
        "intervening_text",
        "filtered_source",
        "explicit_other",
        "current_image",
        "changed_policy",
        "replaced_node",
    ],
)
def test_unrelated_or_invalid_images_are_not_attached(recent_fixture, case):
    cache, archive, runtime, turn, event = recent_fixture
    now = 24
    host = NS()
    assert cache.select(runtime, turn, event, host, now)
    if case == "expired":
        now = 131
    elif case == "other_author":
        turn = replace(turn, author="other")
    elif case == "other_session":
        turn = replace(turn, session_key="test:GroupMessage:other")
    elif case == "later_image":
        turn = replace(turn, visible_node_ids=frozenset({"text"}))
    elif case == "intervening_text":
        runtime.dag.add_message("intervening", "u", "换个话题", timestamp=20)
        turn = replace(turn, visible_node_ids=turn.visible_node_ids | {"intervening"})
    elif case == "filtered_source":
        turn = replace(turn, background=())
    elif case == "explicit_other":
        turn = replace(turn, messages=(replace(turn.messages[0], mentioned_users=("other",)),))
    elif case == "current_image":
        turn = replace(turn, messages=(replace(turn.messages[0], attachments=("Image",)),))
    elif case == "changed_policy":
        cache.entries["picture"].event._chat_dynamics_boundary_token = {"target_revision": 1}
    elif case == "replaced_node":
        runtime.dag.nodes["picture"] = NS(msg_id="picture", user_id="u", timestamp=10)
    assert not cache.select(runtime, turn, event, host, now)


def test_source_is_rechecked_against_member_filter(recent_fixture, monkeypatch):
    cache, _, runtime, turn, event = recent_fixture
    monkeypatch.setattr(member_boundary, "filter_frames", lambda *a, **k: ([], member_boundary.BoundaryState()))
    assert not cache.select(runtime, turn, event, NS(), 24)


def test_confirmed_topic_change_does_not_reuse_image(recent_fixture):
    cache, _, runtime, turn, event = recent_fixture
    turn = replace(
        turn,
        messages=(
            replace(turn.messages[0], semantics=NS(topic_id="new-task", topic_ambiguous=False, topic_confidence=0.95)),
        ),
        background=(
            replace(
                turn.background[0], semantics=NS(topic_id="picture-topic", topic_ambiguous=False, topic_confidence=0.95)
            ),
        ),
    )
    assert not cache.select(runtime, turn, event, NS(), 24)


def test_cache_eviction_keeps_inflight_reader_file_alive_and_then_cleans(recent_fixture):
    cache, _, runtime, turn, event = recent_fixture
    selected = cache.select(runtime, turn, event, NS(), 24)
    directory = Path(selected[0].event._chat_dynamics_media_directory)
    cache.clear()
    gc.collect()
    assert directory.is_dir()
    del selected
    gc.collect()
    assert not directory.exists()


def test_cache_limits_and_stop_release_only_the_owner(recent_fixture):
    cache, archive, runtime, _, event = recent_fixture
    for index in range(MAX_ENTRIES + 2):
        node = runtime.dag.add_message(f"photo-{index}", "u" if index % 2 else "v", "[图片]", timestamp=10)
        cache.remember(event, node, archive, 10)
    assert len(cache.entries) == MAX_ENTRIES
    cache.discard("u")
    assert cache.entries and all(source.node.user_id == "v" for source in cache.entries.values())
    cache.prune(131)
    gc.collect()
    assert not cache.entries and not archive.active


def test_quoted_images_are_not_cached_as_the_speakers_own(recent_fixture):
    cache, archive, runtime, _, event = recent_fixture
    event.message_obj.message = [Reply(id="someone-else", chain=event.message_obj.message)]
    node = runtime.dag.add_message("quote", "u", "引用他人图片", timestamp=25)
    cache.remember(event, node, archive, 25)
    assert "quote" not in cache.entries
