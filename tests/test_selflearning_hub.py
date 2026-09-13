"""Public Hub contract tests using only an ephemeral loopback HTTP server."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from astrbot_plugin_chat_dynamics.core.integrations.selflearning import SelfLearningHubClient


@asynccontextmanager
async def server(override=None):
    calls = []
    async def handler(request):
        calls.append((request.method, request.path, request.headers.get("Authorization")))
        if override:
            response = await override(request)
            if response is not None:
                return response
        if request.path.endswith("manifest"):
            data = {"version": "v1", "endpoints": [{"method": "POST", "path": "/api/hub/v1/context"}]}
        elif request.path.endswith("status"):
            data = {"healthy": True, "capabilities": {"social_context": True}}
        else:
            payload = await request.json()
            assert payload["include"] == {"social": True, "jargon": True, "few_shots": True, "v2": False}
            assert payload["top_k"] <= 4
            assert payload["group_id"] == "group"
            assert payload["user_id"] == "user"
            data = {"group_id": "group", "user_id": "user", "parts": [
                {"type": "social", "content": "friend"}, {"type": "v2", "content": "secret memory"}],
                "v2": {"private": "memory"}}
        return web.json_response({"success": True, "data": data})
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", calls
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_happy_public_contract():
    async with server() as (url, calls):
        client = SelfLearningHubClient(url + "/api/hub/v1/", "secret")
        assert await client.discover()
        assert await client.context(group_id="group", user_id="user", query="hello") == {"social": "friend"}
        assert [c[1] for c in calls] == ["/api/hub/v1/manifest", "/api/hub/v1/status", "/api/hub/v1/context"]
        assert all(c[2] == "Bearer secret" for c in calls)
        assert "secret" not in str(client.snapshot()) and url not in str(client.snapshot())
        await client.close()


@pytest.mark.parametrize("data,code", [
    ({"version": "v2", "endpoints": []}, "unsupported_contract"),
    ({"version": "v1", "endpoints": [{"method": "POST", "path": "https://other/context"}]}, "unsupported_contract"),
])
@pytest.mark.asyncio
async def test_manifest_rejected(data, code):
    async def override(request):
        return web.json_response({"success": True, "data": data})
    async with server(override) as (url, calls):
        client = SelfLearningHubClient(url)
        assert not await client.discover()
        assert client.snapshot()["error_code"] == code
        assert len(calls) == 1


@pytest.mark.parametrize("mode,code", [("auth", "unauthorized"), ("redirect", "redirect_rejected"),
    ("large", "response_too_large"), ("envelope", "invalid_envelope"),
    ("unhealthy", "unhealthy"), ("capabilities", "context_unavailable")])
@pytest.mark.asyncio
async def test_safe_failures(mode, code):
    async def override(request):
        if mode == "auth":
            return web.Response(status=401, text="secret error")
        if mode == "redirect":
            return web.Response(status=302, headers={"Location": "/evil"})
        if mode == "large":
            return web.Response(body=b" " * (256 * 1024 + 1))
        if mode == "envelope":
            return web.json_response({"success": 1, "data": {}})
        if request.path.endswith("status"):
            return web.json_response({"success": True, "data": {"healthy": mode != "unhealthy", "capabilities": {}}})
    async with server(override) as (url, calls):
        client = SelfLearningHubClient(url)
        assert not await client.discover()
        assert client.snapshot()["error_code"] == code
        assert "secret error" not in str(client.snapshot())
        assert all(c[1] != "/evil" for c in calls)


@pytest.mark.asyncio
async def test_total_timeout_includes_both_discovery_requests():
    async def override(request):
        await asyncio.sleep(.06)
    async with server(override) as (url, _):
        client = SelfLearningHubClient(url, timeout=.1)
        assert not await client.discover()
        assert client.snapshot()["error_code"] == "timeout"


@pytest.mark.parametrize("action", ["close", "configure", "cancel"])
@pytest.mark.asyncio
async def test_cancellation_and_hot_update(action):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def override(request):
        entered.set()
        await release.wait()
    async with server(override) as (url, _):
        client = SelfLearningHubClient(url)
        task = asyncio.create_task(client.discover())
        await entered.wait()
        if action == "close":
            await client.close()
        elif action == "configure":
            client.configure("", "new-secret")
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert not client.snapshot()["available"]
        assert not client._tasks


@pytest.mark.parametrize("mismatch", [True, False])
@pytest.mark.asyncio
async def test_context_scope_and_bounds(mismatch):
    async def override(request):
        if request.path.endswith("context"):
            return web.json_response({"success": True, "data": {
                "group_id": "other" if mismatch else "group", "user_id": "user",
                "parts": [{"type": "social", "content": "x" * 10000}],
                "few_shots": ["y" * 10000] * 10, "context_text": "never expose"}})
    async with server(override) as (url, calls):
        client = SelfLearningHubClient(url)
        result = await client.context(group_id="group", user_id="user", query="hello")
        if mismatch:
            assert result == {}
            assert client.snapshot()["error_code"] == "scope_mismatch"
        else:
            assert set(result) == {"social", "few_shots"}
            assert all(len(value) <= 4096 for value in result.values())
        assert len(calls) == 3


@pytest.mark.parametrize("url", ["", "http://user:pass@localhost", "http://localhost?key=secret",
    "http://localhost/#secret", "http://localhost/other", "file:///tmp/a"])
@pytest.mark.asyncio
async def test_disabled_or_invalid_url_never_networks(url):
    client = SelfLearningHubClient(url)
    assert not await client.discover()
    assert await client.context(group_id="group", user_id="user", query="hi") == {}
    assert not client.snapshot()["configured"]


@pytest.mark.asyncio
async def test_identical_configure_preserves_inflight_and_discovery():
    entered, release = asyncio.Event(), asyncio.Event()
    async def override(request):
        if request.path.endswith("manifest"):
            entered.set()
            await release.wait()
    async with server(override) as (url, _):
        client = SelfLearningHubClient(url, "key")
        task = asyncio.create_task(client.discover())
        await entered.wait()
        generation = client._generation
        client.configure(url, "key")
        assert client._generation == generation
        release.set()
        assert await task
        snapshot = client.snapshot()
        client.configure(url, "key")
        assert client.snapshot() == snapshot
        assert client.snapshot()["available"]
        client.configure(url, "different-key")
        assert not client.snapshot()["available"]
        assert client._generation == generation + 1
        await client.close()
