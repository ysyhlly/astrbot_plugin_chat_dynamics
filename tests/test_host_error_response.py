"""A host failure report must never be delivered as the bot's own reply.

AstrBot's ToolLoopAgentRunner does not raise when every candidate chat model
fails. It returns ``LLMResponse(role="err", completion_text="All chat models
failed: ...")`` and keeps that object as the final response, so reading
``completion_text`` off it turned an operator diagnostic into a group message:

    All chat models failed: EmptyModelOutputError: Responses API returned no
    usable output. response_id=resp_xU6xavnTGvusz7IPt8u0sQ4, status=completed

The response is a failure, not a completion: it must stay out of the chat and be
counted as an unavailable model like any other LLM failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.agent_bridge import history_with_delivered_reply
from astrbot_plugin_chat_dynamics.core.llm_adapter import (
    LLMAdapter,
    LLMErrorResponse,
    LLMUnavailable,
    completion_text,
    is_error_response,
    reply_system_prompt,
    require_completion_text,
)
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key

HOST_ERROR = ("All chat models failed: EmptyModelOutputError: Responses API returned no usable output. "
              "response_id=resp_xU6xavnTGvusz7IPt8u0sQ4, status=completed")


class HostErrorResponse:
    """The shape every AstrBot runner uses to report a failure instead of raising."""

    def __init__(self, text: str = HOST_ERROR, role: str = "err"):
        self.role = role
        self.completion_text = text


def test_completion_text_never_reads_a_host_failure():
    assert is_error_response(HostErrorResponse())
    assert is_error_response(SimpleNamespace(role="ERR"))
    assert not is_error_response(SimpleNamespace(role="assistant", completion_text="reply"))
    assert not is_error_response(SimpleNamespace(completion_text="reply"))
    assert not is_error_response(None)
    assert completion_text(HostErrorResponse()) == ""
    assert completion_text(None) == ""
    assert completion_text("plain") == "plain"
    assert completion_text(SimpleNamespace(role="assistant", completion_text="reply")) == "reply"
    with pytest.raises(LLMErrorResponse):
        require_completion_text(HostErrorResponse(), "reply")


@pytest.mark.asyncio
async def test_llm_generate_failure_is_unavailable_not_a_reply():
    ctx = SimpleNamespace(llm_generate=AsyncMock(return_value=HostErrorResponse()))
    client = LLMAdapter(ctx, configured_provider_id="provider")
    with pytest.raises(LLMUnavailable) as failure:
        await client.generate(prompt="hello", umo="umo", system_prompt="be brief")
    # The diagnostic is dropped, not carried around waiting to be logged or sent.
    assert HOST_ERROR not in str(failure.value)


@pytest.mark.asyncio
async def test_native_agent_failure_is_unavailable_not_a_reply(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request

    monkeypatch.setattr(native_request, "prepare_request", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_adapter, "collect_media_urls", AsyncMock(return_value=([], [])))
    client = llm_adapter.LLMAdapter(SimpleNamespace(tool_loop_agent=AsyncMock(return_value=HostErrorResponse())),
                                    configured_provider_id="provider")
    with pytest.raises(LLMUnavailable):
        await client.run_native_agent(MockEvent("hello"), "raw query")


@pytest.mark.asyncio
async def test_group_turn_with_a_failed_model_chain_sends_nothing(monkeypatch):
    """End to end: the failed chain is counted as unavailable and says nothing."""
    plugin = _plugin({"vibe_llm_enabled": False})
    event = MockEvent("帮我解释这个问题", message_id="host-error")
    key = _session_key(event.group_id)
    runtime = plugin._get_or_create_runtime(key, group_id=event.group_id, umo=key, bot_id=event.self_id)
    node = runtime.dag.add_message(event.message_id, event.sender_id, event.message_str)
    monkeypatch.setattr(plugin.context, "tool_loop_agent", AsyncMock(return_value=HostErrorResponse()))
    try:
        await plugin._dispatch_bot_response(runtime, node, GroupChatMode.SERIOUS_INQUIRY, event, runtime.revision)
        assert event.replies_sent == []
        assert plugin._metrics.get("llm_reply_unavailable") == 1
        assert plugin._metrics.get("llm_reply_succeeded", 0) == 0
        assert [item.text for item in runtime.dag.nodes.values() if item.user_id == runtime.bot_id] == []
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_native_reply_requires_a_visible_message():
    plugin = _plugin({"vibe_llm_enabled": False})
    captured = {}

    async def run_native_agent(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "在的"

    plugin.llm.run_native_agent = run_native_agent
    try:
        text = await plugin._run_native_reply(
            MockEvent("hello"),
            text='{"current_turn":"hello"}',
            vibe_mode=GroupChatMode.SERIOUS_INQUIRY,
            session_id="room",
        )
    finally:
        await plugin.terminate()
    assert text == "在的"
    assert captured["kwargs"]["system_prompt"] == reply_system_prompt(GroupChatMode.SERIOUS_INQUIRY)
    assert "可见回复" in captured["kwargs"]["system_prompt"]


def test_saved_history_replays_reasoning_without_unsent_prose():
    prior = {"type": "think", "think": "旧思考", "encrypted": "{\"items\":[]}"}
    call_reasoning = {"type": "think", "think": "", "encrypted": "{\"type\":\"openai_responses_reasoning\"}"}
    final_reasoning = {"type": "think", "think": "整理", "encrypted": None}
    history = [
        {"role": "user", "content": "上一句"},
        {"role": "assistant", "content": [prior, {"type": "text", "text": "旧回复"}]},
        {"role": "user", "content": "这一句"},
        {
            "role": "assistant",
            "content": [call_reasoning, {"type": "text", "text": "我先调用工具"}],
            "tool_calls": [{"id": "c1"}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": [final_reasoning, {"type": "text", "text": "未送达草稿"}]},
    ]
    saved = history_with_delivered_reply(history, "实际发出")
    assert history[-1]["content"][1]["text"] == "未送达草稿"
    assert saved[1]["content"][0] == prior
    assert saved[3]["content"] == [call_reasoning]
    assert saved[3]["tool_calls"] == [{"id": "c1"}]
    assert saved[-1]["content"] == [final_reasoning, {"type": "text", "text": "实际发出"}]
    rendered = str(saved)
    assert "我先调用工具" not in rendered
    assert "未送达草稿" not in rendered
    plain = history_with_delivered_reply(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "draft"}],
        "sent",
    )
    assert plain[-1] == {"role": "assistant", "content": "sent"}
