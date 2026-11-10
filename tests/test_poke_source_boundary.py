import pytest
from .test_jev_decision_layer import jev_plugin as jev_plugin
from .test_plugin_lifecycle import MockEvent
from astrbot_plugin_chat_dynamics.core.agent_bridge import AgentOutput
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult


@pytest.mark.parametrize("sources_current", [True, False])
@pytest.mark.asyncio
async def test_poke_checks_sources_before_send_and_passes_validator_to_sender(jev_plugin, sources_current):
    plugin, bridge = jev_plugin
    event = MockEvent("[戳一戳]", message_id="poke-source")
    runtime = plugin._get_or_create_runtime(event.unified_msg_origin, group_id=event.group_id,
                                           umo=event.unified_msg_origin, bot_id=event.self_id)
    async def validate():
        return sources_current
    output = AgentOutput("收到这次戳戳。", "conv", "[]", [],
                         native_memory_validator=validate, native_memory_event=event)
    async def generate(*args, **kwargs):
        return output
    async def provider(*args, **kwargs):
        return "offline"
    sent = []
    async def send(*args, **kwargs):
        assert await kwargs["native_source_validator"]()
        sent.append(args[2])
        return SendResult(True, "sent")
    remembered = []
    async def remember(receipt, value):
        remembered.append(value)
    plugin.persona_engine.generate = generate
    plugin.llm.resolve_provider_id = provider
    plugin._send_owned = send
    try:
        result = await plugin._speak_poke_with_persona(runtime, event, prompt="poke", history_text="[戳一戳]",
                                                      current=lambda: True, remember=remember)
        assert bool(result) is sources_current
        assert bool(sent) is sources_current
        assert bool(remembered) is sources_current
        assert bool(bridge.commits) is sources_current
    finally:
        await plugin.terminate()
