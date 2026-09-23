"""System One transport contract tests using only an ephemeral loopback server."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from astrbot_plugin_chat_dynamics.core.integrations.typesafe import SystemOneClient

QUESTIONS = {
    "action": {"type": "choice", "instructions": "How?", "criteria": {"reply": "answer", "ignore": "stay out"}},
    "join": {"type": "noul", "instructions": "Speak?"},
}
ANSWERS = {
    "action": {"type": "choice", "choice": "reply", "confidence": 0.9,
               "probabilities": {"reply": 0.9, "ignore": 0.1}},
    "join": {"type": "noul", "noul": 0.83},
}


@pytest.mark.asyncio
async def test_model_change_invalidates_old_generation():
    client = SystemOneClient(base_url="http://127.0.0.1:8123", model="old")
    before = client._generation
    client.configure(base_url="http://127.0.0.1:8123", model="new")
    assert client._generation > before
    assert client.model == "new"
    await client.close()


@asynccontextmanager
async def server(override=None, answers=None, model="jev-1.13.0"):
    calls = []

    async def handler(request):
        calls.append((request.method, request.path, request.headers.get("Authorization")))
        if override:
            response = await override(request)
            if response is not None:
                return response
        body = await request.json()
        assert body["state"] and body["questions"]
        return web.json_response({
            "model": model,
            "answers": ANSWERS if answers is None else answers,
            "usage": {"input_tokens": 120, "output_tokens": 12},
        })

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
async def test_happy_path_returns_typed_answers():
    async with server() as (url, calls):
        client = SystemOneClient(base_url=url, api_key="secret", model="jev-latest")
        answers = await client.evaluate(state={"conversation": {"text": "hi"}}, questions=QUESTIONS)
        assert answers == {
            "action": {"type": "choice", "choice": "reply", "confidence": 0.9,
                       "probabilities": {"reply": 0.9, "ignore": 0.1}},
            "join": {"type": "noul", "noul": 0.83},
        }
        assert [call[1] for call in calls] == ["/v1/systemone"]
        assert calls[0][2] == "Bearer secret"
        snapshot = client.snapshot()
        assert snapshot["status"] == "available" and snapshot["detail"] == "ready"
        assert snapshot["served_model"] == "jev-1.13.0" and snapshot["calls"] == 1
        assert "secret" not in str(snapshot) and url not in str(snapshot)
        await client.close()


@pytest.mark.parametrize("suffix,path", [
    ("", "/v1/systemone"),
    ("/api", "/api/v1/systemone"),
    ("/api/v1", "/api/v1/systemone"),
    ("/typesafe", "/typesafe/v1/systemone"),
    ("/v1/systemone", "/v1/systemone"),
    ("/v1/decisions", "/v1/decisions"),
])
@pytest.mark.asyncio
async def test_documented_base_urls_resolve_to_their_endpoint(suffix, path):
    async with server() as (url, calls):
        client = SystemOneClient(base_url=url + suffix, api_key="secret")
        assert await client.evaluate(state="hello", questions=QUESTIONS)
        assert [call[1] for call in calls] == [path]
        await client.close()


@pytest.mark.parametrize("url", [
    "ftp://api.example.com",
    "https://user:pass@api.example.com",
    "https://api.example.com/?key=1",
    "https://api.example.com/other",
    "not a url",
])
@pytest.mark.asyncio
async def test_invalid_base_url_degrades_without_a_call(url):
    client = SystemOneClient(base_url=url, api_key="secret")
    assert client.snapshot()["error_code"] == "invalid_url"
    assert await client.evaluate(state="hello", questions=QUESTIONS) is None
    await client.close()


@pytest.mark.asyncio
async def test_cleartext_credentials_are_refused_off_loopback():
    client = SystemOneClient(base_url="http://api.typesafe.ai", api_key="secret")
    assert client.snapshot()["error_code"] == "insecure_cleartext"
    assert not client.snapshot()["configured"]
    # Without a key the same cleartext endpoint stays usable.
    client.configure(base_url="http://api.typesafe.ai", api_key="")
    assert client.snapshot()["configured"]
    await client.close()


@pytest.mark.asyncio
async def test_disabled_client_makes_no_call():
    async with server() as (url, calls):
        client = SystemOneClient(enabled=False, base_url=url, api_key="secret")
        assert client.snapshot()["status"] == "disabled"
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert not calls
        await client.close()


@pytest.mark.parametrize("status,code", [
    (401, "unauthorized"), (403, "unauthorized"), (422, "request_rejected"),
    (429, "rate_limited"), (500, "http_500"),
])
@pytest.mark.asyncio
async def test_http_failures_report_their_code(status, code):
    async def override(request):
        return web.Response(status=status, text="upstream detail")
    async with server(override) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret")
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        snapshot = client.snapshot()
        assert snapshot["detail"] == code and snapshot["error_code"] == code
        assert snapshot["failures"] == 1
        await client.close()


@pytest.mark.asyncio
async def test_redirect_and_oversize_and_invalid_json_are_rejected():
    async def redirect(request):
        return web.Response(status=302, headers={"Location": "/evil"})
    async with server(redirect) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret")
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "redirect_rejected"
        await client.close()

    async def oversize(request):
        return web.Response(body=b" " * (64 * 1024 + 1))
    async with server(oversize) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret")
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "response_too_large"
        await client.close()

    async def broken(request):
        return web.Response(body=b"{not json", content_type="application/json")
    async with server(broken) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret")
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "invalid_json"
        await client.close()


@pytest.mark.asyncio
async def test_timeout_is_a_failed_call_not_an_exception():
    async def override(request):
        await asyncio.sleep(0.5)
        return web.json_response({"model": "m", "answers": ANSWERS, "usage": {}})
    async with server(override) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret", timeout=0.05)
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "timeout"
        await client.close()


@pytest.mark.parametrize("answers", [
    # an option this call never offered
    {"action": {"type": "choice", "choice": "shout", "confidence": 0.9, "probabilities": {}},
     "join": {"type": "noul", "noul": 0.5}},
    # a choice without the confidence the contract promises
    {"action": {"type": "choice", "choice": "reply", "probabilities": {}},
     "join": {"type": "noul", "noul": 0.5}},
    # a probability outside [0, 1]
    {"action": {"type": "choice", "choice": "reply", "confidence": 0.9, "probabilities": {}},
     "join": {"type": "noul", "noul": 1.4}},
    # a required answer is missing
    {"action": {"type": "choice", "choice": "reply", "confidence": 0.9, "probabilities": {}}},
    # an unknown question type
    {"action": {"type": "boolean", "value": True}, "join": {"type": "noul", "noul": 0.5}},
])
@pytest.mark.asyncio
async def test_contract_breaks_reject_the_whole_response(answers):
    async with server(answers=answers) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret")
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "invalid_answers"
        await client.close()


@pytest.mark.parametrize("state,questions,code", [
    ("", QUESTIONS, "invalid_state"),
    (None, QUESTIONS, "invalid_state"),
    ("x" * 24001, QUESTIONS, "invalid_state"),
    ("hello", {}, "invalid_questions"),
    ("hello", {"bad": {"type": "choice", "instructions": "x"}}, "invalid_questions"),
    ("hello", {f"q{index}": {"type": "noul", "instructions": "x"} for index in range(11)}, "invalid_questions"),
])
@pytest.mark.asyncio
async def test_local_request_validation_never_reaches_the_network(state, questions, code):
    async with server() as (url, calls):
        client = SystemOneClient(base_url=url, api_key="secret", timeout=1.0)
        assert await client.evaluate(state=state, questions=questions) is None
        assert client.snapshot()["detail"] == code
        assert not calls
        await client.close()


@pytest.mark.asyncio
async def test_reconfiguration_cancels_in_flight_work_without_cancelling_the_caller():
    entered, release = asyncio.Event(), asyncio.Event()

    async def override(request):
        entered.set()
        await release.wait()
        return web.json_response({"model": "m", "answers": ANSWERS, "usage": {}})

    async with server(override) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret", timeout=5.0)
        task = asyncio.create_task(client.evaluate(state="hello", questions=QUESTIONS))
        await entered.wait()
        client.configure(enabled=False, base_url=url, api_key="secret")
        assert await asyncio.wait_for(task, timeout=2) is None
        release.set()
        assert not client._tasks
        await client.close()


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_swallowed():
    entered, release = asyncio.Event(), asyncio.Event()

    async def override(request):
        entered.set()
        await release.wait()
        return web.json_response({"model": "m", "answers": ANSWERS, "usage": {}})

    async with server(override) as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret", timeout=5.0)
        task = asyncio.create_task(client.evaluate(state="hello", questions=QUESTIONS))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await client.close()


@pytest.mark.asyncio
async def test_close_is_final():
    async with server() as (url, calls):
        client = SystemOneClient(base_url=url, api_key="secret")
        await client.close()
        client.configure(base_url=url, api_key="secret")
        assert client.snapshot()["detail"] == "closed"
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert not calls


@pytest.mark.asyncio
async def test_unchanged_configuration_keeps_the_connection_state():
    async with server() as (url, _calls):
        client = SystemOneClient(base_url=url, api_key="secret")
        assert await client.evaluate(state="hello", questions=QUESTIONS)
        client.configure(enabled=True, base_url=url, api_key="secret")
        assert client.snapshot()["status"] == "available"
        # A different endpoint on the same client invalidates what was learned.
        client.configure(enabled=True, base_url=url + "/v1", api_key="other")
        assert client.snapshot()["status"] == "configured"
        await client.close()


@pytest.mark.asyncio
async def test_state_is_sent_as_structured_json():
    seen = {}

    async def handler(request):
        seen.update(await request.json())
        return web.json_response({"model": "m", "answers": ANSWERS, "usage": {}})

    app = web.Application()
    app.router.add_post("/v1/systemone", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        client = SystemOneClient(base_url=f"http://127.0.0.1:{port}", model="~typesafe/jev-latest")
        assert await client.evaluate(state={"conversation": {"text": "你好"}}, questions=QUESTIONS)
        assert seen["model"] == "~typesafe/jev-latest"
        assert seen["state"] == {"conversation": {"text": "你好"}}
        assert json.dumps(seen["questions"], sort_keys=True) == json.dumps(QUESTIONS, sort_keys=True)
        await client.close()
    finally:
        await runner.cleanup()
