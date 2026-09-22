"""Laya transport: one typed decision call per turn, against a local Laya service.

Laya is a self-hosted typed-decision model. Its wire format is the same published
contract TypeSafe's System One serves — `choice`/`score`/`noul` answers with the
same fields and the same closed vocabulary — so the request/response validators in
`core/integrations/typesafe.py` are reused verbatim rather than reimplemented. Only
two things differ:

* the endpoint is `POST {base}/predict` rather than `/v1/systemone`, and
* the request envelope carries `state` and `questions` but **no** `model`, because a
  local deployment serves one model per port rather than a model catalogue.

Laya also returns a `confidence` on `noul` answers where System One does not. That
field is deliberately dropped by the shared validator: `core/jev_decision.py` gates
the whole decision on the weakest `choice`/`score` confidence, and a second,
differently-calibrated number on one answer type would make that floor mean two
things.

Everything said in `typesafe.py` about not turning a remote decision model into a
remote control applies here too. One extra hazard is added by self-hosting: this
client sends the conversation itself, so the payload — not just the credential —
has to stay off the public cleartext internet. See `_cleartext_allowed`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .typesafe import (
    MAX_BODY,
    MAX_STATE_CHARS,
    MIN_TIMEOUT,
    MAX_TIMEOUT,
    _payload_size_ok,
    _question_specs,
    _SystemOneError,
    _timeout_seconds,
    _validated_answers,
)

ENDPOINT_SUFFIX = "/predict"
_BASE_PATHS = frozenset({"", "/predict"})
DEFAULT_BASE_URL = "http://127.0.0.1:8900"


def _endpoint_path(path: str) -> str:
    """The full predict path for a documented base URL, never doubled."""
    if path.endswith(ENDPOINT_SUFFIX):
        return path
    return path + ENDPOINT_SUFFIX


# The networks a self-hosted pair of ends may talk over in cleartext, listed
# explicitly. `ipaddress.is_private` is deliberately not used: its semantics are
# "not globally routable", which also swallows documentation and benchmarking
# ranges, and this policy is specifically about where a conversation may travel
# on a network the operator runs both ends of.
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",     # IPv4 loopback
        "10.0.0.0/8",      # RFC 1918
        "172.16.0.0/12",   # RFC 1918
        "192.168.0.0/16",  # RFC 1918
        "169.254.0.0/16",  # IPv4 link-local
        "100.64.0.0/10",   # carrier-grade NAT, RFC 6598
        "::1/128",         # IPv6 loopback
        "fc00::/7",        # unique local, RFC 4193
        "fe80::/10",       # IPv6 link-local
    )
)


def _cleartext_allowed(hostname: str) -> bool:
    """Whether the conversation may travel in cleartext HTTP to this host.

    There is no credential to steal here — what leaves the process is the group
    chat itself. Loopback never leaves the machine and RFC 1918 / link-local /
    unique-local addresses never leave the network the operator runs both ends on,
    so plain HTTP is an acceptable self-hosting default there. A public address is
    not: that would publish private conversation to every hop in between. Use
    https for anything routed over the internet.
    """
    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # Not a literal address: a name can resolve to anything, so it is not
        # assumed to sit on a private network.
        return False
    return any(address in network for network in _PRIVATE_NETWORKS)


class LayaClient:
    """Bounded client for one Laya decision call per turn.

    The duck-typed contract the persona turn calls is `evaluate` / `configure` /
    `snapshot` / `close`; `SystemOneClient` implements the same four. Keeping them
    interchangeable is what lets `core/persona_engine.py` pick a backend without
    knowing which wire protocol it is about to speak.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        base_url: str = "",
        timeout: float = 0.5,
    ) -> None:
        self.timeout = _timeout_seconds(timeout, 0.5)
        self.calls = 0
        self.failures = 0
        self.last_request_id = ""
        self._generation = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._url = ""
        self._host = ""
        self._status = "missing"
        self._detail = "not_configured"
        self._configured_input: tuple | None = None
        self.configure(enabled=enabled, base_url=base_url, timeout=timeout)

    # ---- configuration -------------------------------------------------

    def configure(
        self,
        *,
        enabled: bool = True,
        base_url: str | None = None,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> None:
        """Apply configuration; unchanged input keeps the current connection state.

        `**_ignored` absorbs the `api_key` / `model` keywords `SystemOneClient`
        takes so the caller can configure either backend through one code path.
        Laya is a single local model with no credential, and accepting those
        keywords only to discard them is cheaper than teaching the caller which
        backend is which.
        """
        if self._closed:
            return
        if timeout is not None:
            self.timeout = _timeout_seconds(timeout, self.timeout)
        url = str(base_url or "").strip()
        desired = (bool(enabled), url)
        if self._configured_input == desired and (self._url or not enabled):
            return
        self._configured_input = desired
        self._invalidate()
        if not enabled:
            self._status, self._detail = "disabled", "decision_backend_not_laya"
            return
        if not url:
            self._status, self._detail = "missing", "not_configured"
            return
        try:
            parts = urlsplit(url)
            path = parts.path.rstrip("/")
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or path not in _BASE_PATHS
            ):
                raise ValueError
            _ = parts.port
            if parts.scheme == "http" and not _cleartext_allowed(parts.hostname):
                self._status, self._detail = "degraded", "insecure_cleartext"
                return
            self._url = urlunsplit((parts.scheme, parts.netloc, _endpoint_path(path), "", ""))
            self._host = str(parts.hostname or "")
        except (ValueError, TypeError):
            self._status, self._detail = "degraded", "invalid_url"
            return
        self._status, self._detail = "configured", "not_called"

    def _invalidate(self) -> None:
        self._generation += 1
        for task in tuple(self._tasks):
            task.cancel()
        self._url = ""
        self._host = ""
        self._status = "missing"
        self._detail = "not_configured"

    def snapshot(self) -> dict[str, Any]:
        """Panel-safe state: no full URL, no message content."""
        return {
            "configured": bool(self._url),
            "available": self._status == "available",
            "status": self._status,
            "detail": self._detail,
            "error_code": self._detail if self._status == "degraded" else "",
            "endpoint_host": self._host,
            "calls": self.calls,
            "failures": self.failures,
            "request_id": self.last_request_id,
        }

    # ---- requests ------------------------------------------------------

    async def evaluate(
        self,
        *,
        state: Any,
        questions: Any,
        timeout: float | None = None,
    ) -> dict[str, dict] | None:
        """Ask one Laya call; return validated answers or None.

        None is the only failure signal and it always leaves a code in
        `snapshot()["detail"]`; callers must treat it as "no decision available"
        rather than as a negative decision.
        """
        specs = _question_specs(questions)
        if specs is None:
            self._detail = "invalid_questions"
            return None
        payload = _payload(state, specs)
        if payload is None:
            self._detail = "invalid_state"
            return None
        if not self._url or self._closed:
            return None
        generation = self._generation
        deadline = self.timeout if timeout is None else _timeout_seconds(timeout, self.timeout)

        async def exchange():
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.post(self._url, json=payload, allow_redirects=False) as response:
                    if response.status in {401, 403}:
                        raise _SystemOneError("unauthorized")
                    if response.status == 422:
                        raise _SystemOneError("request_rejected")
                    if response.status == 429:
                        raise _SystemOneError("rate_limited")
                    if 300 <= response.status < 400:
                        raise _SystemOneError("redirect_rejected")
                    if response.status != 200:
                        raise _SystemOneError(f"http_{int(response.status)}")
                    request_id = str(response.headers.get("x-request-id") or "")[:64]
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            raise _SystemOneError("response_too_large")
            try:
                envelope = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise _SystemOneError("invalid_json") from None
            answers = _validated_answers(envelope, specs)
            if answers is None:
                raise _SystemOneError("invalid_answers")
            return answers, request_id

        async def perform():
            self.calls += 1
            try:
                answers, request_id = await asyncio.wait_for(exchange(), timeout=deadline)
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                return self._failed(generation, "timeout")
            except _SystemOneError as exc:
                return self._failed(generation, exc.code)
            except (aiohttp.ClientError, OSError, ValueError):
                return self._failed(generation, "transport_error")
            if generation != self._generation or self._closed:
                return None
            self._status, self._detail = "available", "ready"
            self.last_request_id = request_id
            return answers

        task = asyncio.create_task(perform())
        self._tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            # Reconfiguring or switching the decision layer off cancels this request
            # on purpose, and the honest answer is "no decision available" — a
            # `CancelledError` escaping would abort a turn that was merely
            # mid-decision. A cancellation aimed at the caller (reset, stop,
            # shutdown) is not ours to swallow, so it is re-raised.
            if task.cancelled() and (self._closed or generation != self._generation):
                return None
            task.cancel()
            raise
        finally:
            self._tasks.discard(task)

    def _failed(self, generation: int, code: str) -> None:
        if generation == self._generation and not self._closed:
            self._status, self._detail = "degraded", code
            self.failures += 1
        return None

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
        self._url = ""
        self._host = ""
        self._status, self._detail = "missing", "closed"


def _payload(state: Any, specs: dict[str, dict]) -> dict | None:
    """The Laya request envelope: `model` is absent on purpose (see module docstring)."""
    if isinstance(state, str):
        if not state.strip():
            return None
        body: Any = state[:MAX_STATE_CHARS]
    elif isinstance(state, (dict, list)):
        if not state:
            return None
        body = state
    else:
        return None
    if not _payload_size_ok(body):
        return None
    return {"state": body, "questions": specs}
