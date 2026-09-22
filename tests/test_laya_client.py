"""Laya transport contract tests using only an ephemeral loopback server.

`core/integrations/laya.py` shares `typesafe`'s validators outright, so what is
asserted here is what genuinely differs from `test_jev_client.py`: the endpoint
and envelope, the absent credential, Laya's extra `confidence` on a `noul`, and
the cleartext policy that covers the conversation itself.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from astrbot_plugin_chat_dynamics.core.integrations.laya import (
    LayaClient,
    _cleartext_allowed,
)

QUESTIONS = {
    "action": {"type": "choice", "instructions": "How?", "criteria": {"reply": "answer", "ignore": "stay out"}},
    "join": {"type": "noul", "instructions": "Speak?"},
}
# Laya answers a noul with its own confidence where System One does not. The
# shared validator drops it on purpose; the test pins that the drop happens
# instead of rejecting the answer or leaking the number downstream.
ANSWERS = {
    "action": {"type": "choice", "choice": "reply", "confidence": 0.9,
               "probabilities": {"reply": 0.9, "ignore": 0.1}},
    "join": {"type": "noul", "noul": 0.83, "confidence": 0.83},
}


@asynccontextmanager
async def server(override=None, answers=None):
    calls = []

    async def handler(request):
        calls.append({
            "method": request.method,
            "path": request.path,
            "authorization": request.headers.get("Authorization"),
            "body": await request.json(),
        })
        if override:
            response = await override(request)
            if response is not None:
                return response
        return web.json_response({
            "model": "laya-rl-agent",
            "answers": ANSWERS if answers is None else answers,
            "usage": {"input_tokens": 120, "output_tokens": 0},
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


def always(response):
    """An override handler that ignores the request and returns `response`."""

    async def _override(request):
        return response

    return _override


@pytest.mark.asyncio
async def test_request_hits_predict_without_model_or_credential():
    async with server() as (url, calls):
        client = LayaClient(base_url=url)
        answers = await client.evaluate(state={"conversation": {"text": "hi"}}, questions=QUESTIONS)
        assert answers == {
            "action": {"type": "choice", "choice": "reply", "confidence": 0.9,
                       "probabilities": {"reply": 0.9, "ignore": 0.1}},
            # The extra confidence is dropped, never passed upward.
            "join": {"type": "noul", "noul": 0.83},
        }
        assert len(calls) == 1
        assert calls[0]["path"] == "/predict"
        assert calls[0]["authorization"] is None
        assert "model" not in calls[0]["body"]
        assert set(calls[0]["body"]) == {"state", "questions"}
        assert client.snapshot()["status"] == "available"


@pytest.mark.asyncio
async def test_a_base_url_that_names_the_endpoint_is_used_as_is():
    async with server() as (url, calls):
        client = LayaClient(base_url=url + "/predict")
        await client.evaluate(state="hello", questions=QUESTIONS)
        assert calls[0]["path"] == "/predict"


@pytest.mark.parametrize("code,expected", [
    (401, "unauthorized"),
    (403, "unauthorized"),
    (422, "request_rejected"),
    (429, "rate_limited"),
    (302, "redirect_rejected"),
    (503, "http_503"),
])
@pytest.mark.asyncio
async def test_http_failures_report_a_code_and_no_decision(code, expected):
    async with server(override=always(web.Response(status=code))) as (url, _):
        client = LayaClient(base_url=url)
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == expected
        assert client.snapshot()["error_code"] == expected


@pytest.mark.asyncio
async def test_malformed_and_off_contract_replies_are_discarded_whole():
    cases = [
        (web.Response(text="not json"), "invalid_json"),
        (web.json_response({"answers": {}}), "invalid_answers"),
        # An option this call never offered is not a decision the plugin can act on.
        (web.json_response({"answers": {**ANSWERS, "action": {
            "type": "choice", "choice": "sideways", "confidence": 0.9, "probabilities": {}}}}),
         "invalid_answers"),
        (web.json_response({"answers": {**ANSWERS, "join": {"type": "noul", "noul": 4.0}}}),
         "invalid_answers"),
    ]
    for response, expected in cases:
        async with server(override=always(response)) as (url, _):
            client = LayaClient(base_url=url)
            assert await client.evaluate(state="hello", questions=QUESTIONS) is None
            assert client.snapshot()["detail"] == expected


@pytest.mark.asyncio
async def test_slow_replies_fall_out_of_the_call_budget():
    async def slow(request):
        await asyncio.sleep(1.0)
        return web.json_response({"answers": ANSWERS})

    async with server(override=slow) as (url, _):
        client = LayaClient(base_url=url, timeout=0.05)
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "timeout"
        assert client.snapshot()["failures"] == 1


@pytest.mark.parametrize("payload,detail", [
    ("", "invalid_questions"),
    (None, "invalid_questions"),
])
@pytest.mark.asyncio
async def test_an_unusable_request_never_leaves_the_process(payload, detail):
    async with server() as (url, calls):
        client = LayaClient(base_url=url)
        questions = payload if payload is not None else {"q": {"type": "guess", "instructions": "x"}}
        assert await client.evaluate(state="hello", questions=questions) is None
        assert client.snapshot()["detail"] == detail
        assert calls == []


@pytest.mark.parametrize("state", ["", None, 7, []])
@pytest.mark.asyncio
async def test_an_empty_or_unencodable_state_is_refused(state):
    async with server() as (url, calls):
        client = LayaClient(base_url=url)
        assert await client.evaluate(state=state, questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "invalid_state"
        assert calls == []


@pytest.mark.asyncio
async def test_reconfiguration_invalidates_the_request_in_flight():
    released = asyncio.Event()

    async def wait(request):
        await released.wait()
        return web.json_response({"answers": ANSWERS})

    async with server(override=wait) as (url, _):
        client = LayaClient(base_url=url)
        task = asyncio.ensure_future(client.evaluate(state="hello", questions=QUESTIONS))
        await asyncio.sleep(0)
        client.configure(enabled=False)
        released.set()
        assert await task is None
        assert client.snapshot()["status"] == "disabled"


@pytest.mark.asyncio
async def test_close_is_repeatable_and_refuses_further_calls():
    async with server() as (url, calls):
        client = LayaClient(base_url=url)
        await client.close()
        await client.close()
        assert await client.evaluate(state="hello", questions=QUESTIONS) is None
        assert client.snapshot()["detail"] == "closed"
        assert calls == []


@pytest.mark.parametrize("url,detail", [
    ("api.typesafe.ai", "invalid_url"),
    ("ftp://host", "invalid_url"),
    ("http://user:pass@host", "invalid_url"),
    ("http://host/?q=1", "invalid_url"),
    ("http://host/other", "invalid_url"),
    ("", "not_configured"),
])
def test_only_documented_endpoints_are_configurable(url, detail):
    client = LayaClient(base_url=url)
    snapshot = client.snapshot()
    assert snapshot["configured"] is False
    assert snapshot["detail"] == detail


def test_the_conversation_may_not_travel_in_public_cleartext():
    """What leaves the process is the group chat, so the policy is about content.

    Loopback never leaves the machine and private ranges never leave the network
    the operator runs both ends on. A public address over plain http would publish
    the conversation to every hop in between, so that endpoint degrades instead of
    silently answering.
    """
    assert _cleartext_allowed("127.0.0.1")
    assert _cleartext_allowed("localhost")
    assert _cleartext_allowed("192.168.1.4")
    assert _cleartext_allowed("10.0.0.7")
    assert _cleartext_allowed("172.16.3.9")
    assert _cleartext_allowed("100.64.0.3")
    assert _cleartext_allowed("::1")
    assert _cleartext_allowed("fe80::1")
    # Not a literal address: it could resolve anywhere, so it gets no assumption.
    assert not _cleartext_allowed("laya.internal.example")
    # Documentation and benchmarking ranges are not routable either, but they are
    # not networks an operator runs both ends on, so they get no exception.
    assert not _cleartext_allowed("203.0.113.10")
    assert not _cleartext_allowed("198.51.100.7")
    assert not _cleartext_allowed("8.8.8.8")

    degraded = LayaClient(base_url="http://203.0.113.10:8900")
    assert degraded.snapshot()["status"] == "degraded"
    assert degraded.snapshot()["error_code"] == "insecure_cleartext"

    secured = LayaClient(base_url="https://203.0.113.10:8900")
    assert secured.snapshot()["configured"] is True


def test_configure_absorbs_the_other_backends_keywords():
    """`api_key` / `model` belong to System One; one caller configures either."""
    client = LayaClient(base_url="")
    client.configure(enabled=True, base_url="http://127.0.0.1:8900",
                     api_key="ignored", model="ignored", timeout=2.0)
    assert client.snapshot()["configured"] is True
    assert client.timeout == 2.0
