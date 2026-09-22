"""Real socket checks for service metadata, admin credentials and reconfiguration."""
import asyncio

import pytest
from aiohttp import web

from astrbot_plugin_chat_dynamics.core.integrations.laya import LayaClient


@pytest.mark.asyncio
async def test_service_json_operations_keep_response_identity_and_cancel_on_reconfigure():
    seen = []
    pending = asyncio.Event()
    release = asyncio.Event()

    async def endpoint(request):
        seen.append((request.path, request.headers.get('Authorization')))
        if request.path == '/wait':
            pending.set()
            await release.wait()
        if request.path == '/redirect':
            raise web.HTTPFound('/metadata')
        if request.path == '/bad':
            return web.Response(text='not json')
        return web.json_response({'model_version': 'model-A', 'tasks': ['join']})

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = LayaClient(base_url=f'http://127.0.0.1:{port}')
    try:
        assert (await client.request_json('/metadata', method='GET'))['model_version'] == 'model-A'
        assert (await client.request_json('/admin/status', method='GET', token='token-A'))['tasks'] == ['join']
        assert seen[-1] == ('/admin/status', 'Bearer token-A')
        diagnostics = {}
        assert await client.request_json('/redirect', diagnostics=diagnostics) is None
        assert diagnostics == {'status': 'http_302'}
        assert await client.request_json('/bad', diagnostics=diagnostics) is None
        assert diagnostics == {'status': 'transport_or_json_error'}
        assert await client.request_json('/wait', timeout=.01, diagnostics=diagnostics) is None
        assert diagnostics == {'status': 'timeout'}
        task = asyncio.create_task(client.request_json('/wait', timeout=1))
        await pending.wait()
        client.configure(enabled=False)
        assert await task is None
        assert not client._tasks
    finally:
        release.set()
        await client.close()
        await runner.cleanup()
