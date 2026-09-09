"""Adapter compatibility and graceful fallback contracts."""
from types import SimpleNamespace as NS
from unittest.mock import Mock, AsyncMock
import pytest
from astrbot_plugin_chat_dynamics.core import platform_bridge as p


def component(name, **attrs):
    obj = type(name, (), {})()
    vars(obj).update(attrs)
    return obj


def test_command_detection_handles_legacy_extra_signature_and_filters():
    own = NS(handler_name="on_group_message", event_filters=[component("CommandFilter")])
    other = NS(handler_name="other", event_filters=[component("Custom", command_name="help")])
    event = NS(get_extra=lambda key, default: [own, other] if key == "activated_handlers" else {})
    assert p.host_command_activated(event)
    for error in (TypeError, RuntimeError):
        assert not p.host_command_activated(NS(get_extra=Mock(side_effect=error)))
    assert not p.host_command_activated(None)
    assert not p.host_command_activated(NS(get_extra=lambda key: [own] if key == "activated_handlers" else {}))


def test_original_command_text_and_failed_component_getters():
    assert p._original_message_text(NS(get_message_str=lambda: "/help"), "help") == "/help"
    event = NS(get_message_str=Mock(side_effect=RuntimeError), get_messages=Mock(side_effect=RuntimeError),
               message_obj=NS(message=["fallback"]))
    assert p._original_message_text(event, "hello") == "hello"
    assert p.iter_message_components(event) == ["fallback"]
    assert p._safe_call(NS(value=Mock(side_effect=RuntimeError)), "value", "fallback") == "fallback"
    assert p._poke_target_id(NS(target_id=Mock(side_effect=RuntimeError), user_id=32)) == "32"
    assert p._poke_target_id(NS(id=0)) == ""


@pytest.mark.parametrize("kind,expected", [("Voice", "[语音]"), ("Video", "[视频]"),
    ("File", "[文件]"), ("Poke", "[戳一戳]"), ("Unknown", "[媒体附件]")])
def test_media_placeholder_preserves_attachment_kind(kind, expected):
    parsed = p.ParsedEvent("g", "u", "b", "m", "", media_component_types=[kind])
    assert p.media_placeholder_text(parsed) == expected


@pytest.mark.asyncio
async def test_media_refs_fallback_after_conversion_error_and_deduplicate():
    image = component("Image", convert_to_file_path=Mock(side_effect=RuntimeError), get_file=AsyncMock(return_value="image.png"))
    audio = component("Record", url="audio.ogg")
    wrapper = component("Node", chain=[audio, image])
    event = NS(get_messages=lambda: [image, wrapper, component("Image")])
    assert await p.collect_media_urls(event) == (["image.png"], ["audio.ogg"])
    assert image.get_file.await_count == 2
    assert await p._component_file_ref(NS(convert_to_file_path=lambda: "local")) == "local"


def test_poke_construction_supports_old_required_type(monkeypatch):
    class Poke:
        def __init__(self, *, type, qq, id):
            self.qq = None
            self.id = None
    import astrbot.api.message_components as message_components
    monkeypatch.setattr(message_components, "Poke", Poke, raising=False)
    poke = p._make_poke_component("123")
    assert poke.qq == poke.id == "123"


def test_rich_result_and_plain_text_fallbacks():
    assert p.result_has_rich_media(NS(get_chain=lambda: [component("Image")]))
    assert not p.result_has_rich_media(NS(get_chain=Mock(side_effect=RuntimeError)))
    assert p.result_has_rich_media(NS(chain=[component("Node", chain=["caption"])]))
    assert p.chain_plain_text(NS(get_plain_text=lambda flag: "legacy")) == "legacy"
    assert p.chain_plain_text(NS(get_plain_text=Mock(side_effect=RuntimeError), chain=["a", NS(text="b")])) == "ab"
    assert p.chain_plain_text(42) == "42"
    assert not p.is_streaming_host_result(NS())


@pytest.mark.asyncio
async def test_send_result_passthrough_and_identifier_only_object():
    result = p.SendResult(False, error="rejected")
    assert await p.send_plain(NS(send=lambda chain: result), None, "room", "hello") is result
    sent = await p.send_plain(NS(send=lambda chain: NS(id="42")), None, "room", "hello")
    assert sent.success and sent.message_id == "42"
