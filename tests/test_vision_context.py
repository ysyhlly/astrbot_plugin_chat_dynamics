from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core import vision_context as vision


def test_caption_settings_preserve_host_config():
    original = {"image_caption_prompt": "原有要求", "other": 1}
    result = vision.caption_settings(original)
    assert result["image_caption_prompt"].startswith("原有要求")
    assert vision.VISION_REQUIREMENTS in result["image_caption_prompt"]
    assert original == {"image_caption_prompt": "原有要求", "other": 1}


@pytest.mark.asyncio
async def test_native_caption_refinement_uses_configured_provider(monkeypatch):
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="初音未来，依据：双马尾与服饰")))
    context = SimpleNamespace(
        get_config=lambda **kwargs: {"provider_settings": {"default_image_caption_provider_id": "vision"}},
        get_provider_by_id=lambda pid: provider if pid == "vision" else None)
    monkeypatch.setattr(vision, "collect_media_urls", AsyncMock(return_value=(["image-a"], [])))
    request = SimpleNamespace(extra_user_content_parts=[SimpleNamespace(text="<image_caption>卡通人物</image_caption>")])
    result = await vision.refine_host_caption(context, SimpleNamespace(unified_msg_origin="room"), request)
    assert "初音未来" in result
    assert provider.text_chat.await_args.kwargs["image_urls"] == ["image-a"]
    assert vision.VISION_REQUIREMENTS in provider.text_chat.await_args.kwargs["prompt"]
    assert request.extra_user_content_parts[0].text == "<image_caption>卡通人物</image_caption>"


@pytest.mark.asyncio
async def test_no_caption_does_not_call_another_model():
    assert await vision.refine_host_caption(None, None, SimpleNamespace()) == ""
