"""Unit tests for AstrBot event parsing and official send helpers."""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from astrbot_plugin_chat_dynamics.core.platform_bridge import (
    build_plain_chain,
    chain_plain_text,
    collect_media_urls,
    extract_mentions_and_reply,
    has_understandable_media,
    host_command_activated,
    is_command_like,
    is_streaming_host_result,
    media_placeholder_text,
    parse_group_event,
    SendResult,
    send_plain,
)


class At:
    def __init__(self, qq: str):
        self.qq = qq


class Reply:
    def __init__(self, id: str):
        self.id = id


class FakeEvent:
    def __init__(
        self,
        text: str,
        group_id: str = "g1",
        sender_id: str = "u1",
        self_id: str = "bot",
        message_id: str = "m1",
        comps: Optional[List[Any]] = None,
        is_at: bool = False,
    ):
        self.message_str = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self.message_id = message_id
        self.is_at_or_wake_command = is_at
        self.unified_msg_origin = f"mock:GroupMessage:{group_id}"
        self.message_obj = type("Obj", (), {"message_id": message_id, "message": list(comps or [])})()
        self.sent: List[str] = []

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return self._self_id

    def get_messages(self) -> List[Any]:
        return list(self.message_obj.message)

    async def send(self, chain: Any) -> str:
        self.sent.append(chain_plain_text(chain))
        return "platform-mid-9"


def test_is_command_like_detects_slash_prefix():
    assert is_command_like("/dynamics status") is True
    assert is_command_like("／help") is True
    assert is_command_like("今天天气真好") is False
    assert is_command_like("a / b") is False


def test_extract_at_and_reply_components():
    event = FakeEvent("第二点", comps=[Reply(id="prev1"), At(qq="bot")])
    mentions, reply = extract_mentions_and_reply(event, event.message_str)
    assert mentions == ["bot"]
    assert reply == "prev1"


def test_extract_at_from_plain_text_token():
    mentions, reply = extract_mentions_and_reply(FakeEvent("你好 @小助手"), "你好 @小助手")
    assert "小助手" in mentions
    assert reply is None


def test_parse_group_event_wake_does_not_inject_self_id():
    parsed = parse_group_event(FakeEvent("在吗", self_id="bot_42", is_at=True))
    assert parsed.is_at_or_wake is True
    assert "bot_42" not in parsed.mentions
    assert parsed.is_command is False


def test_parse_group_event_detects_face_media():
    class Face:
        def __init__(self):
            self.id = "1"

    event = FakeEvent("", comps=[Face()])
    parsed = parse_group_event(event)
    assert parsed.has_media is True
    assert parsed.text == ""


def test_parse_group_event_does_not_treat_poke_as_media():
    class Poke:
        def __init__(self):
            self.qq = "bot"
            self.id = "bot"

        def target_id(self):
            return "bot"

    event = FakeEvent("", self_id="bot", comps=[Poke()])
    parsed = parse_group_event(event)
    assert parsed.has_media is False
    assert parsed.has_poke is True
    assert parsed.poke_at_bot is True
    assert parsed.media_only is False
    assert "戳一戳" in parsed.text


def test_parse_group_event_marks_commands():
    parsed = parse_group_event(FakeEvent("/dynamics cool 10"))
    assert parsed.is_command is True


def test_parse_group_event_detects_wake_stripped_slash_command():
    event = FakeEvent("签到")
    event.message_obj.message_str = "/签到"
    parsed = parse_group_event(event)
    assert parsed.is_command is True
    assert parsed.text == "签到"


def test_host_command_activated_skips_foreign_command_handlers():
    class CommandFilter:
        command_name = "签到"

    class Handler:
        handler_name = "checkin"
        event_filters = [CommandFilter()]

        def handler(self):
            return None

    event = FakeEvent("签到")
    extras = {
        "activated_handlers": [Handler()],
        "handlers_parsed_params": {"plugin.checkin": {}},
    }

    def get_extra(key, default=None):
        return extras.get(key, default)

    event.get_extra = get_extra
    assert host_command_activated(event) is True
    assert parse_group_event(event).is_command is True


@pytest.mark.asyncio
async def test_send_plain_uses_event_send_and_returns_id():
    event = FakeEvent("hi")
    result = await send_plain(event, None, "g1", "你好")
    assert result.success is True
    assert result.message_id == "platform-mid-9"
    assert event.sent == ["你好"]


@pytest.mark.asyncio
async def test_send_plain_attaches_reply_component():
    event = FakeEvent("hi")
    captured = {}

    async def _send(chain):
        captured["chain"] = chain
        event.sent.append(chain_plain_text(chain))
        return "mid-r"

    event.send = _send
    await send_plain(event, None, "g1", "回你", reply_to_id="m-origin")
    chain = captured["chain"]
    types = [type(item).__name__.lower() for item in getattr(chain, "chain", [])]
    assert "reply" in types


def test_build_plain_chain_roundtrip():
    chain = build_plain_chain("abc")
    assert chain_plain_text(chain) == "abc"


def test_is_streaming_host_result_detects_finish_and_live_stream():
    from types import SimpleNamespace

    assert is_streaming_host_result(None) is False
    assert is_streaming_host_result(SimpleNamespace()) is False
    assert is_streaming_host_result(SimpleNamespace(result_content_type=SimpleNamespace(name="GENERAL_RESULT"))) is False
    assert is_streaming_host_result(SimpleNamespace(result_content_type=SimpleNamespace(name="STREAMING_FINISH"))) is True
    assert is_streaming_host_result(SimpleNamespace(result_content_type=SimpleNamespace(name="STREAMING_RESULT"))) is True
    try:
        from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType

        streamed = MessageEventResult().message("同一答案").set_result_content_type(
            ResultContentType.STREAMING_FINISH
        )
        assert is_streaming_host_result(streamed) is True
        assert streamed.result_content_type == ResultContentType.STREAMING_FINISH
        plain = MessageEventResult().message("同一答案")
        assert is_streaming_host_result(plain) is False
    except ImportError:
        pass


@pytest.mark.asyncio
async def test_send_plain_reports_failure_separately_from_missing_message_id():
    class NoIdEvent(FakeEvent):
        async def send(self, chain):
            self.sent.append(chain_plain_text(chain))
            return None

    class FailedEvent(FakeEvent):
        async def send(self, chain):
            raise RuntimeError("network down")

    success = await send_plain(NoIdEvent("hi"), None, "g1", "ok")
    failure = await send_plain(FailedEvent("hi"), None, "g1", "no")
    assert success.success is True and success.message_id is None
    assert failure.success is False and "network down" in (failure.error or "")


@pytest.mark.asyncio
async def test_send_plain_treats_false_from_event_as_failure():
    event = FakeEvent("hi")

    async def _reject(_chain):
        return False

    event.send = _reject
    result = await send_plain(event, None, "g1", "no")
    assert result.success is False
    assert "rejected" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_result", "expected_id"),
    [
        ({"message_id": "dict-mid"}, "dict-mid"),
        (type("Result", (), {"msg_id": "object-mid"})(), "object-mid"),
        (None, None),
    ],
)
async def test_send_plain_uses_context_fallback_and_extracts_ids(platform_result, expected_id):
    class EventWithoutSend:
        unified_msg_origin = "adapter:GroupMessage:g1"

    class Context:
        def __init__(self):
            self.calls = []

        async def send_message(self, target, chain):
            self.calls.append((target, chain_plain_text(chain)))
            return platform_result

    context = Context()
    result = await send_plain(EventWithoutSend(), context, "fallback", "你好")

    assert result.success is True
    assert result.message_id == expected_id
    assert context.calls == [("adapter:GroupMessage:g1", "你好")]


@pytest.mark.asyncio
async def test_send_plain_reports_false_from_context_fallback():
    class Context:
        async def send_message(self, _target, _chain):
            return False

    result = await send_plain(None, Context(), "adapter:GroupMessage:g1", "no")
    assert result.success is False


@pytest.mark.asyncio
async def test_send_plain_accepts_synchronous_legacy_send_adapters():
    class SyncEvent:
        unified_msg_origin = "sync:room"

        def send(self, chain):
            return {"message_id": "sync-1"}

    result = await send_plain(SyncEvent(), None, "sync:room", "hello")
    assert result.success is True
    assert result.message_id == "sync-1"


@pytest.mark.asyncio
async def test_send_plain_preserves_structured_send_result_failures():
    class StructuredEvent:
        def __init__(self, result):
            self.result = result

        async def send(self, _chain):
            return self.result

    failed = await send_plain(
        StructuredEvent({"success": False, "error": "rejected", "message_id": "m-fail"}),
        None,
        "room",
        "no",
    )
    succeeded = await send_plain(
        StructuredEvent(SendResult(True, "m-ok")),
        None,
        "room",
        "yes",
    )
    assert failed.success is False and failed.message_id == "m-fail"
    assert succeeded.success is True and succeeded.message_id == "m-ok"


def test_result_has_rich_media_detects_image_not_plain():
    from astrbot_plugin_chat_dynamics.core.platform_bridge import result_has_rich_media

    class Image:
        pass

    class Result:
        def __init__(self, chain):
            self.chain = chain

    assert result_has_rich_media(Result([Image()])) is True
    assert result_has_rich_media(Result(["hello"])) is False
    assert result_has_rich_media(None) is False


def test_understandable_media_excludes_stickers():
    image = parse_group_event(_event_with_comp("Image"))
    face = parse_group_event(_event_with_comp("Face"))
    record = parse_group_event(_event_with_comp("Record"))
    poke = parse_group_event(_event_with_comp("Poke"))
    assert has_understandable_media(image) is True
    assert has_understandable_media(record) is True
    assert has_understandable_media(face) is False
    assert has_understandable_media(poke) is False
    assert poke.has_media is False
    assert poke.has_poke is True
    assert media_placeholder_text(image) == "[图片]"
    assert media_placeholder_text(record) == "[语音]"


def _event_with_comp(name: str):
    class Event:
        message_str = ""
        message_id = "m"
        unified_msg_origin = "g"
        is_at_or_wake_command = False
        message_obj = type("M", (), {"message_id": "m", "message": [type(name, (), {})()]})()

        def get_group_id(self):
            return "g"

        def get_sender_id(self):
            return "u"

        def get_self_id(self):
            return "bot"

        def get_messages(self):
            return list(self.message_obj.message)

        def get_message_outline(self):
            return ""

    return Event()


@pytest.mark.asyncio
async def test_collect_media_urls_from_image_and_record():
    class Image:
        url = "https://example.test/a.png"

        async def convert_to_file_path(self):
            return "/tmp/a.png"

    class Record:
        url = "https://example.test/a.wav"

        async def convert_to_file_path(self):
            return "/tmp/a.wav"

    class Event:
        message_obj = type("M", (), {"message": [Image(), Record()]})()

        def get_messages(self):
            return list(self.message_obj.message)

    images, audios = await collect_media_urls(Event())
    assert images == ["/tmp/a.png"]
    assert audios == ["/tmp/a.wav"]


@pytest.mark.asyncio
async def test_run_native_agent_forwards_image_and_audio_urls():
    from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter

    captured: dict[str, Any] = {}

    class Image:
        async def convert_to_file_path(self):
            return "/tmp/pic.png"

    class Record:
        async def convert_to_file_path(self):
            return "/tmp/voice.wav"

    class Event:
        unified_msg_origin = "room"
        message_obj = type("M", (), {"message": [Image(), Record()]})()

        def get_messages(self):
            return list(self.message_obj.message)

    class Ctx:
        async def get_current_chat_provider_id(self, _umo):
            return "p"

        async def tool_loop_agent(self, **kwargs):
            captured.update(kwargs)
            return type("R", (), {"completion_text": "seen"})()

    text = await LLMAdapter(Ctx()).run_native_agent(Event(), "帮我看")
    assert text == "seen"
    assert captured.get("image_urls") == ["/tmp/pic.png"]
    assert captured.get("audio_urls") == ["/tmp/voice.wav"]

