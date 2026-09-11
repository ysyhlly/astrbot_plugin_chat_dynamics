import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.conversation_context import build_conversation_context
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key


def test_context_bounds_background_and_keeps_current_fragments_separate():
    dag = ConversationDAG()
    dag.add_message("background", "bot", "x" * 2000, timestamp=1)
    dag.add_message("fragment", "alice", "first", timestamp=2, metadata={"turn_id": "turn"})
    node = dag.add_message("trigger", "alice", "second", timestamp=3, reply_to_id="background",
                           metadata={"turn_id": "turn", "consolidated_text": "y" * 2000})
    payload = build_conversation_context(dag, node, "bot")
    assert payload["current_turn"] == "y" * 2000
    assert {item["message_id"] for item in payload["message_semantics"]} == {"fragment", "trigger"}
    assert [item["message_id"] for item in payload["background_conversation_data"]] == ["background"]
    assert len(payload["background_conversation_data"][0]["text"]) == 1200
    # Owned and host-native prompts share this note, so it must stay self-describing.
    for field in ("sender_id", "recipient_ids", "certainty", "untrusted"):
        assert field in payload["attribution_note"]


def test_context_retains_social_hint():
    dag = ConversationDAG()
    node = dag.add_message("poke", "alice", "")
    assert build_conversation_context(dag, node)["social_hint"]


@pytest.mark.asyncio
async def test_owned_hook_does_not_duplicate_context_but_preserves_other_hints():
    plugin = _plugin()
    key = _session_key("context")
    runtime = plugin._get_or_create_runtime(key, group_id="context", umo=key, bot_id="bot")
    runtime.dag.add_message("background", "bot", "x" * 2000)
    node = runtime.dag.add_message("m", "alice", "hello", reply_to_id="background")
    event = MockEvent("hello", group_id="context", message_id="m", self_id="bot")
    injected = []
    plugin._inject_vibe_hint = lambda request, hint: injected.append(hint)
    captured = []

    async def generate(*args, **kwargs):
        captured.append(json.loads(kwargs["text"]))
        return ""

    plugin._run_native_reply = generate
    try:
        await plugin._dispatch_bot_response(runtime, node, GroupChatMode.CHILL_FADE, event, runtime.revision)
        await plugin.on_llm_request(event, SimpleNamespace(prompt="hello"))
        context_hints = [hint for hint in injected if "background_conversation_data" in hint]
        assert len(context_hints) == 1
        assert json.loads(context_hints[0].split("：", 1)[1]) == captured[0]
        injected.clear()
        event._chat_dynamics_owned_request = True
        await plugin.on_llm_request(event, SimpleNamespace(prompt=json.dumps(captured[0])))
        assert injected  # Vibe and other hooks still run.
        assert not any("background_conversation_data" in hint for hint in injected)
    finally:
        await plugin.terminate()
