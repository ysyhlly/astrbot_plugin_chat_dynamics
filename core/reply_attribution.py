"""Pass owned-reply identity to an optional local provider observer."""

import re
from contextlib import nullcontext


class ReplyAttributionProvider:
    def __init__(self, provider, scope: str, turn_id: str):
        self.provider = provider
        self.scope = scope
        self.turn_id = turn_id

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def _attribution(self):
        # Resolve the hook on each call so a retired observer is never reused.
        attribute = getattr(self.provider, "_series_attributed_call_v1", None)
        if callable(attribute):
            return attribute("chat_dynamics.reply", scope=self.scope, operation_id=self.turn_id)
        return nullcontext()

    async def text_chat(self, *args, **kwargs):
        with self._attribution():
            return await self.provider.text_chat(*args, **kwargs)

    async def text_chat_stream(self, *args, **kwargs):
        with self._attribution():
            stream = aiter(self.provider.text_chat_stream(*args, **kwargs))
        finished = object()
        try:
            while True:
                with self._attribution():
                    response = await anext(stream, finished)
                if response is finished:
                    break
                # Attribution ends before yielding to hooks or tool dispatch.
                yield response
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                with self._attribution():
                    await close()


def bind_reply_attribution(runner, scope, *turn_ids):
    """Bind existing turn identity for the primary and any configured fallbacks."""
    turn_id = next((value for value in turn_ids if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)), "")
    if not (isinstance(scope, str) and scope and turn_id):
        return

    def bind(provider):
        if callable(getattr(provider, "_series_attributed_call_v1", None)):
            return ReplyAttributionProvider(provider, scope, turn_id)
        return provider

    runner.provider = bind(runner.provider)
    runner.fallback_providers = [bind(provider) for provider in getattr(runner, "fallback_providers", ())]
