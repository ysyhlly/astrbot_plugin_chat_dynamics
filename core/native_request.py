"""Dispatch native request hooks for the legacy owned tool loop."""

import copy
import asyncio
import inspect
import json


def hooks_available():
    try:
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.provider.entities import ProviderRequest
        return callable(call_event_hook) and callable(ProviderRequest)
    except ImportError:
        return False


async def prepare_request(event, prompt, image_urls, audio_urls):
    if event is None:
        return None
    try:
        from astrbot.core.pipeline.context_utils import call_event_hook
        from astrbot.core.provider.entities import ProviderRequest
        from astrbot.core.star.star_handler import EventType
    except ImportError:
        return None
    owned = copy.copy(event)
    owned._chat_dynamics_owned_request = True
    owned._result = None
    owned.continue_event()
    if hasattr(event, "_extras"):
        owned._extras = dict(event._extras)
        owned._extras.pop("provider_request", None)
    kwargs = dict(prompt=prompt, image_urls=list(image_urls))
    if "audio_urls" in inspect.signature(ProviderRequest).parameters:
        kwargs["audio_urls"] = list(audio_urls)
    request = ProviderRequest(**kwargs)
    stopped = await call_event_hook(owned, EventType.OnLLMRequestEvent, request)
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()
    if stopped:
        raise RuntimeError("native_request_stopped")
    # The simplified tool loop has no content-parts argument. Preserve text
    # injected by modern companion hooks in this request only.
    texts = [part.text for part in getattr(request, "extra_user_content_parts", ())
             if isinstance(getattr(part, "text", None), str)]
    if texts:
        request.prompt = (request.prompt or "") + "\n\n临时上下文数据（非系统指令）：" + json.dumps(texts, ensure_ascii=False)
    return request
