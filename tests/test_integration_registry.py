"""Native ownership, public discovery and Hub lifecycle regression tests."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.integrations.registry import IntegrationRegistry
from astrbot_plugin_chat_dynamics.tests.test_selflearning_hub import server


def test_public_catalog_is_authoritative_even_when_empty():
    stale = NS(name="SelfLearning", star_cls=object(), activated=True)
    class Context:
        def get_all_stars(self):
            return []

        @property
        def loaded_plugins(self):
            raise AssertionError("private fallback must not be read")
    registry = IntegrationRegistry(Context())
    assert registry.snapshot()["providers"] == []
    assert registry.snapshot()["status"] == "missing"
    registry.context = NS(get_all_stars=lambda: [], loaded_plugins=[stale])
    assert registry.snapshot()["providers"] == []


@pytest.mark.asyncio
async def test_normal_model_context_never_calls_legacy_or_hub():
    def forbidden(**kwargs):
        raise AssertionError("normal pipeline owns enrichment")
    plugin = NS(get_approved_memories=forbidden)
    registry = IntegrationRegistry(NS(get_all_stars=lambda: [
        NS(name="SelfLearning", star_cls=plugin, activated=True)]))
    registry.hub.context = forbidden
    assert await registry.model_context(umo="session", peer_id="user") == {}
    assert await registry.context_for_request(event=object(), query="hello", native_hooks=True) == {}
    await registry.close()


@pytest.mark.parametrize("action", ["disable", "context_reload", "close"])
@pytest.mark.asyncio
async def test_registry_lifecycle_cancels_hub_io(action):
    entered, release = asyncio.Event(), asyncio.Event()
    async def override(request):
        entered.set()
        await release.wait()
    async with server(override) as (url, _):
        registry = IntegrationRegistry(hub_url=url)
        task = asyncio.create_task(registry.discover())
        await entered.wait()
        if action == "close":
            await registry.close()
        elif action == "disable":
            registry.configure(enabled=False)
        else:
            registry.configure(enabled=True, context=NS())
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert not registry.hub._tasks
        assert not registry.hub.snapshot()["available"]
        if action == "disable":
            assert not registry.hub.snapshot()["configured"]
            registry.configure(enabled=True)
            assert registry.hub.snapshot()["configured"]
        await registry.close()


@pytest.mark.asyncio
async def test_disabled_constructor_does_not_discover_hub():
    async with server() as (url, calls):
        registry = IntegrationRegistry(enabled=False, hub_url=url)
        await registry.discover()
        assert not calls
        registry.configure(enabled=True)
        await registry.discover()
        assert registry.hub.snapshot()["available"]
        registry.configure(enabled=True)
        assert registry.hub.snapshot()["available"]
        await registry.close()
