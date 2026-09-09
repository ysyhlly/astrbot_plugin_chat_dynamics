"""web_compat request parsing contracts: loader shapes and query fallbacks."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core import web_compat as wc


@pytest.fixture
def clean_request(monkeypatch):
    """Remove every JSON entry point so each test installs exactly one."""
    monkeypatch.setattr(wc, "request", NS())
    return wc


@pytest.mark.asyncio
async def test_request_json_awaits_async_loader_and_retries_legacy_shape(clean_request):
    wc.request.json = AsyncMock(return_value={"a": 1})
    assert await wc.request_json(default={}) == {"a": 1}

    calls = []

    def legacy_loader(*args, **kwargs):
        calls.append((args, kwargs))
        if kwargs:
            raise TypeError("legacy json() takes no default")
        return {"b": 2}

    wc.request.json = legacy_loader
    assert await wc.request_json(default={}) == {"b": 2}
    assert len(calls) == 2  # keyword form attempted first, then legacy retry


@pytest.mark.asyncio
async def test_request_json_returns_raw_property_value(clean_request):
    wc.request.json = {"c": 3}
    assert await wc.request_json(default={}) == {"c": 3}


@pytest.mark.asyncio
async def test_request_json_falls_back_to_get_json_and_default(clean_request):
    wc.request.get_json = lambda force, silent: {"d": 4}
    assert await wc.request_json(default={}) == {"d": 4}

    wc.request.get_json = lambda force, silent: None
    assert await wc.request_json(default={"fallback": True}) == {"fallback": True}


@pytest.mark.asyncio
async def test_request_json_default_when_no_loader(clean_request):
    assert await wc.request_json(default="empty") == "empty"


def test_query_value_prefers_query_and_falls_back_to_args(clean_request):
    wc.request.query = {"room": " 42 "}
    wc.request.args = {"room": "ignored"}
    assert wc.query_value("room") == "42"

    wc.request.query = None
    assert wc.query_value("room") == "ignored"

    wc.request.query = None
    wc.request.args = None
    assert wc.query_value("room") == ""

    wc.request.query = {"room": 7}
    assert wc.query_value("room") == "7"
    assert wc.query_value("missing") == ""
