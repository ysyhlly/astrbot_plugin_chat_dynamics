"""Version-aware AstrBot LLM invocation.

The 4.16+ path always resolves ``chat_provider_id`` first, then calls
``context.llm_generate``. Older Context objects are used only when those
methods are explicitly missing — never via a broad ``except``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from .platform_bridge import collect_media_urls
from .vibe_analyzer import GroupChatMode


class LLMUnavailable(RuntimeError):
    """Raised when the host Context cannot satisfy a chat completion request."""


SYSTEM_PROMPTS = {
    GroupChatMode.FAST_BANTER: (
        "你正在一个群聊里用口语短句说话。不要使用 Markdown、列表或客服式收尾，"
        "也不要说“如果还有问题随时问我”。语气轻松，像群友。"
    ),
    GroupChatMode.SERIOUS_INQUIRY: (
        "你正在群聊里认真回答问题。可以保留必要结构，但不要客服式结尾，"
        "不要主动拉长对话。"
    ),
    GroupChatMode.CHILL_FADE: (
        "群里比较冷清。回复简短自然，不要主动开启新话题，不要客服式收尾。"
    ),
}

# Short, per-turn style hints appended as temporary user context — never as system_prompt.
VIBE_HINTS = {
    GroupChatMode.FAST_BANTER: "用口语短句回答，不要 Markdown、列表或客服式收尾。",
    GroupChatMode.SERIOUS_INQUIRY: "认真回答，可保留必要结构，不要客服式结尾，不要主动拉长对话。",
    GroupChatMode.CHILL_FADE: "简短自然地回答，不要主动开新话题，不要客服式收尾。",
}


def system_prompt_for(mode: GroupChatMode) -> str:
    return SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS[GroupChatMode.CHILL_FADE])


def vibe_hint_for(mode: GroupChatMode) -> str:
    return VIBE_HINTS.get(mode, VIBE_HINTS[GroupChatMode.CHILL_FADE])


POKE_HINT = "这是戳一戳，不是图片或文件。用一两句口语回应即可，不要描述附件。"


def poke_hint_for() -> str:
    return POKE_HINT


def _media_kwargs(image_urls: list[str] | None, audio_urls: list[str] | None) -> dict[str, list[str]]:
    payload: dict[str, list[str]] = {}
    if image_urls:
        payload["image_urls"] = list(image_urls)
    if audio_urls:
        payload["audio_urls"] = list(audio_urls)
    return payload


def _supported_kwargs(fn: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    if not candidate:
        return {}
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(candidate)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return dict(candidate)
    return {key: value for key, value in candidate.items() if key in signature.parameters}


def completion_text(resp: Any) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    text = getattr(resp, "completion_text", None)
    if text:
        return str(text)
    return ""


class LLMAdapter:
    """Thin adapter over AstrBot Context chat APIs."""

    def __init__(
        self,
        context: Any,
        configured_provider_id: str = "",
        *,
        reply_provider_id: str = "",
        vibe_provider_id: str = "",
        reply_timeout: float = 60.0,
        tool_agent_timeout: float = 120.0,
    ) -> None:
        self.context = context
        self.configured_provider_id = str(configured_provider_id or "").strip()
        self.reply_provider_id = str(reply_provider_id or "").strip()
        self.vibe_provider_id = str(vibe_provider_id or "").strip()
        self.reply_timeout = reply_timeout
        self.tool_agent_timeout = tool_agent_timeout

    def configure(
        self,
        provider_id: str,
        *,
        reply_provider_id: str = "",
        vibe_provider_id: str = "",
        reply_timeout: float = 60.0,
        tool_agent_timeout: float = 120.0,
    ) -> None:
        self.configured_provider_id = str(provider_id or "").strip()
        self.reply_provider_id = str(reply_provider_id or "").strip()
        self.vibe_provider_id = str(vibe_provider_id or "").strip()
        self.reply_timeout = reply_timeout
        self.tool_agent_timeout = tool_agent_timeout

    def configured_provider(self, purpose: str = "reply") -> str:
        """Return the configured preference before UMO-specific resolution."""
        dedicated = self.reply_provider_id if purpose == "reply" else self.vibe_provider_id
        return dedicated or self.configured_provider_id

    async def resolve_provider_id(self, umo: str, purpose: str = "reply") -> str:
        explicit = self.reply_provider_id if purpose == "reply" else self.vibe_provider_id
        if explicit:
            return explicit
        # ``provider`` is the v1.1 unified override. Keep it as a fallback for
        # both paths so existing installations retain their behavior.
        if self.configured_provider_id:
            return self.configured_provider_id
        ctx = self.context
        if ctx is None:
            raise LLMUnavailable("AstrBot context is missing")
        getter = getattr(ctx, "get_current_chat_provider_id", None)
        if not callable(getter):
            raise LLMUnavailable("No provider configured and current provider lookup is unavailable")
        try:
            prov_id = getter(umo)
        except TypeError:
            prov_id = getter()
        if inspect.isawaitable(prov_id):
            prov_id = await prov_id
        if not prov_id:
            raise LLMUnavailable(f"No chat provider is available for {umo}")
        return str(prov_id)

    async def generate(
        self,
        *,
        prompt: str,
        umo: str,
        system_prompt: str,
        purpose: str = "reply",
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
    ) -> str:
        """Bound provider lookup and completion by one shared deadline."""
        try:
            return await asyncio.wait_for(
                self._generate(prompt=prompt, umo=umo, system_prompt=system_prompt,
                               purpose=purpose, image_urls=image_urls, audio_urls=audio_urls),
                timeout=self.reply_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise LLMUnavailable(f"LLM reply timed out after {self.reply_timeout:g}s") from exc

    async def _generate(
        self,
        *,
        prompt: str,
        umo: str,
        system_prompt: str,
        purpose: str = "reply",
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
    ) -> str:
        ctx = self.context
        if ctx is None:
            raise LLMUnavailable("AstrBot context is missing")

        generate = getattr(ctx, "llm_generate", None)
        if callable(generate):
            prov_id = await self.resolve_provider_id(umo, purpose=purpose)
            resp = generate(
                chat_provider_id=prov_id,
                prompt=prompt,
                system_prompt=system_prompt,
                **_supported_kwargs(generate, _media_kwargs(image_urls, audio_urls)),
            )
            if inspect.isawaitable(resp):
                resp = await resp
            return completion_text(resp)

        return await self._generate_via_legacy_provider(
            prompt,
            umo,
            system_prompt,
            purpose=purpose,
        )

    async def run_native_agent(
        self,
        event: Any,
        prompt: str,
        *,
        umo: str = "",
        vibe_hint: str = "",
    ) -> str:
        """Bound media, hooks, provider lookup, and tool execution together."""
        try:
            return await asyncio.wait_for(
                self._run_native_agent(event, prompt, umo=umo, vibe_hint=vibe_hint),
                timeout=self.tool_agent_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise LLMUnavailable(f"Native agent timed out after {self.tool_agent_timeout:g}s") from exc

    async def _run_native_agent(
        self,
        event: Any,
        prompt: str,
        *,
        umo: str = "",
        vibe_hint: str = "",
    ) -> str:
        """Run AstrBot's native agent so persona, history, and tools stay attached.

        Prefers ``tool_loop_agent(event=...)``. Falls back to ``llm_generate`` only
        when that API is missing — still not a replacement system prompt.
        """
        ctx = self.context
        if ctx is None:
            raise LLMUnavailable("AstrBot context is missing")
        target_umo = str(
            umo
            or getattr(event, "unified_msg_origin", "")
            or ""
        ).strip()
        user_prompt = prompt if not vibe_hint else f"{prompt}\n\n({vibe_hint})"
        image_urls, audio_urls = await collect_media_urls(event)
        if image_urls:
            from .vision_context import MAIN_VISION_HINT
            user_prompt += "\n\n" + MAIN_VISION_HINT
        media_kwargs = _media_kwargs(image_urls, audio_urls)
        from .native_request import prepare_request
        request = await prepare_request(event, user_prompt, image_urls, audio_urls)
        request_kwargs = {}
        if request is not None:
            user_prompt = request.prompt
            media_kwargs = _media_kwargs(request.image_urls, getattr(request, "audio_urls", audio_urls))
            request_kwargs = dict(system_prompt=request.system_prompt,
                                  contexts=request.contexts, tools=request.func_tool)
        tool_loop = getattr(ctx, "tool_loop_agent", None)
        if callable(tool_loop):
            prov_id = await self.resolve_provider_id(target_umo, purpose="reply")
            resp = tool_loop(
                event=event,
                chat_provider_id=prov_id,
                prompt=user_prompt,
                **_supported_kwargs(tool_loop, request_kwargs),
                **_supported_kwargs(tool_loop, media_kwargs),
            )
            if inspect.isawaitable(resp):
                resp = await resp
            return completion_text(resp)
        if request is not None and (request.contexts or request.func_tool):
            raise LLMUnavailable("Native request tools/history require tool_loop_agent")
        return await self.generate(
            prompt=user_prompt,
            umo=target_umo,
            system_prompt=request.system_prompt if request is not None else "",
            image_urls=request.image_urls if request is not None else image_urls,
            audio_urls=getattr(request, "audio_urls", audio_urls) if request is not None else audio_urls,
        )

    async def _generate_via_legacy_provider(
        self,
        prompt: str,
        umo: str,
        system_prompt: str,
        *,
        purpose: str = "reply",
    ) -> str:
        """AstrBot builds that expose get_using_provider but not llm_generate."""
        ctx = self.context
        explicit = self.reply_provider_id if purpose == "reply" else self.vibe_provider_id
        if not explicit:
            explicit = self.configured_provider_id
        provider = None
        by_id = getattr(ctx, "get_provider_by_id", None)
        if explicit:
            # A configured provider is an explicit routing decision. If the
            # legacy host cannot resolve it, fail closed instead of silently
            # switching to the session's default provider (which can change
            # persona, cost, or data routing).
            if not callable(by_id):
                raise LLMUnavailable("Configured provider lookup is unavailable")
            provider = by_id(explicit)
            if inspect.isawaitable(provider):
                provider = await provider
            if provider is None:
                raise LLMUnavailable("Configured provider is unavailable")
        else:
            getter = getattr(ctx, "get_using_provider", None)
            if not callable(getter):
                raise LLMUnavailable("AstrBot context does not expose llm_generate")
            try:
                provider = getter(umo)
            except TypeError:
                # A few pre-4.x Context shims expose a no-argument getter;
                # retain compatibility without broadening the modern path.
                provider = getter()
            if inspect.isawaitable(provider):
                provider = await provider
        if provider is None:
            raise LLMUnavailable(f"No provider instance is available for {umo}")
        text_chat = getattr(provider, "text_chat", None)
        if not callable(text_chat):
            raise LLMUnavailable("Provider does not expose text_chat")
        resp = text_chat(prompt=prompt, system_prompt=system_prompt)
        if inspect.isawaitable(resp):
            resp = await resp
        return completion_text(resp)
