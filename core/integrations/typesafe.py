"""TypeSafe System One (Jev) transport: one typed decision call per turn.

Only transport and contract validation live here. `core/jev_decision.py` owns which
questions are asked, how the answers become a turn decision, and where the confidence
floors sit. Nothing in this module knows what a group chat is.

Three properties keep a remote decision model from becoming a remote control:

* The credential is read from the process environment by name and never enters
  configuration, snapshots or logs.
* A response that breaks the published contract is discarded whole. A decision layer
  that guesses at a malformed answer is worse than one that admits it could not
  decide: the caller then falls back to its own conservative policy.
* Reconfiguration — including switching the backend off — invalidates in-flight
  requests, so a decision taken against the old endpoint can never be delivered
  under the new one.

The endpoint contract is TypeSafe's System One API (`POST {base}/v1/systemone`),
which OpenRouter (`https://openrouter.ai/api`), the Vercel AI Gateway
(`https://ai-gateway.vercel.sh/typesafe`) and AI/ML API (`/v1/decisions`, already a
complete path) all serve. Requests carry `model`, `state` and `questions`;
responses carry `model`, `answers` and `usage`. Choice answers return
`choice`/`probabilities`/`confidence`, score answers return
`score`/`legend`/`probabilities`/`confidence`, and noul answers return a single
probability `noul` with **no** confidence of its own.

There are no retries. The SDK retries 408/429/5xx with backoff, but this client sits
behind a per-turn decision deadline: a retry that lands after the turn has moved on
is a wasted call, and the caller already has a conservative plan to fall back to.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .net_policy import is_loopback_host

# The three published question types. An answer of any other type is not a
# decision this plugin can act on.
QUESTION_TYPES = frozenset({"choice", "score", "noul"})
ENDPOINT_SUFFIX = "/v1/systemone"
# Paths the SDKs are documented to use as a base URL. The SDK appends
# `/v1/systemone` to each of these; a path that already names an endpoint is used
# as-is, which is how AI/ML API's `/v1/decisions` stays reachable.
_BASE_PATHS = frozenset({"", "/api", "/v1", "/api/v1", "/typesafe"})
_ENDPOINT_PATHS = (ENDPOINT_SUFFIX, "/v1/decisions")
MAX_QUESTIONS = 10
MAX_BODY = 64 * 1024
MAX_STATE_CHARS = 24000
MAX_MODEL_CHARS = 64
MIN_TIMEOUT, MAX_TIMEOUT = 0.05, 30.0


def _payload_size_ok(body: Any) -> bool:
    """Whether a request body fits the documented bound once encoded.

    Shared with `core/integrations/laya.py`, which enforces the same bound on the
    same kind of payload.
    """
    try:
        encoded = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return len(encoded) <= MAX_STATE_CHARS


class _SystemOneError(Exception):
    """A contract or transport failure carrying its diagnostic code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _timeout_seconds(value: Any, default: float = 6.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        parsed = default
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, parsed))


def _unit(value: Any) -> float | None:
    """A probability in [0, 1], or None when the value is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if parsed != parsed or not 0.0 <= parsed <= 1.0:
        return None
    return parsed


def _probabilities(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        parsed = _unit(item)
        if isinstance(key, str) and parsed is not None:
            result[key] = parsed
    return result


def _endpoint_path(path: str) -> str:
    """The full path for a documented base URL, without doubling its own version."""
    if path.endswith(_ENDPOINT_PATHS):
        return path
    if path.endswith("/v1"):
        return path + "/systemone"
    return path + ENDPOINT_SUFFIX


def _model_id(value: Any) -> str:
    model = str(value or "").strip() or "jev-latest"
    return model[:MAX_MODEL_CHARS]


def _question_specs(questions: Any) -> dict[str, dict] | None:
    """Validate the request side once, before anything leaves the process."""
    if not isinstance(questions, dict) or not questions or len(questions) > MAX_QUESTIONS:
        return None
    specs: dict[str, dict] = {}
    for key, spec in questions.items():
        if not isinstance(key, str) or not key or len(key) > 64 or not isinstance(spec, dict):
            return None
        kind = spec.get("type")
        if kind not in QUESTION_TYPES:
            return None
        if "instructions" not in spec:
            return None
        criteria = spec.get("criteria")
        if kind == "choice" and (not isinstance(criteria, dict) or not criteria):
            return None
        if kind == "score" and (not isinstance(criteria, list) or len(criteria) < 2):
            return None
        if kind == "noul" and criteria is not None and not isinstance(criteria, dict):
            return None
        specs[key] = spec
    return specs


def _payload(model: str, state: Any, specs: dict[str, dict]) -> dict | None:
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
    return {"model": model, "state": body, "questions": specs}


def _validated_answers(envelope: Any, specs: dict[str, dict]) -> dict[str, dict] | None:
    """Typed answers for every requested question, or None when the reply is unusable.

    Only the documented shapes pass. An option outside the criteria this call sent,
    a probability outside [0, 1], a missing confidence or a score beyond the levels
    all reject the whole response rather than being coerced into a decision.
    """
    if not isinstance(envelope, dict):
        return None
    raw = envelope.get("answers")
    if not isinstance(raw, dict) or not raw:
        return None
    answers: dict[str, dict] = {}
    for key, spec in specs.items():
        item = raw.get(key)
        if not isinstance(item, dict):
            return None
        kind = item.get("type") or spec.get("type")
        if kind != spec.get("type"):
            return None
        criteria = spec.get("criteria")
        if kind == "choice":
            choice = item.get("choice")
            if not isinstance(choice, str) or not choice:
                return None
            if isinstance(criteria, dict) and choice not in criteria:
                return None
            probabilities = _probabilities(item.get("probabilities"))
            confidence = _unit(item.get("confidence"))
            if confidence is None:
                return None
            answers[key] = {"type": "choice", "choice": choice,
                            "probabilities": probabilities, "confidence": confidence}
        elif kind == "score":
            score = item.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                return None
            top = len(criteria) - 1 if isinstance(criteria, list) and criteria else None
            value = float(score)
            if value != value or value < 0.0 or (top is not None and value > top + 1e-9):
                return None
            confidence = _unit(item.get("confidence"))
            if confidence is None:
                return None
            answers[key] = {"type": "score", "score": value,
                            "probabilities": _probabilities(item.get("probabilities")),
                            "confidence": confidence}
        else:
            probability = _unit(item.get("noul"))
            if probability is None:
                return None
            answers[key] = {"type": "noul", "noul": probability}
    return answers


def _cancel_request(task: asyncio.Task) -> None:
    """Cancel once: a second cancel could interrupt the transport's cleanup."""
    if not task.done() and not getattr(task, "_client_cancel_requested", False):
        setattr(task, "_client_cancel_requested", True)
        task.cancel()


class SystemOneClient:
    """Bounded client for one System One decision call per turn."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        base_url: str = "",
        api_key: str = "",
        model: str = "jev-latest",
        timeout: float = 6.0,
    ) -> None:
        self.timeout = _timeout_seconds(timeout)
        self.model = _model_id(model)
        self.calls = 0
        self.failures = 0
        # Set from a successful response: the versioned model that actually served
        # the call (`jev-latest` is an alias that moves), and the provider's request
        # ID, which is the only handle an operator can quote to the API vendor.
        self.served_model = ""
        self.last_request_id = ""
        self._generation = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._url = ""
        self._key = ""
        self._host = ""
        self._status = "missing"
        self._detail = "not_configured"
        self._configured_input: tuple | None = None
        self.configure(enabled=enabled, base_url=base_url, api_key=api_key, model=model, timeout=timeout)

    # ---- configuration -------------------------------------------------

    def configure(
        self,
        *,
        enabled: bool = True,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Apply configuration; unchanged input keeps the current connection state."""
        if self._closed:
            return
        if model is not None:
            self.model = _model_id(model)
        if timeout is not None:
            self.timeout = _timeout_seconds(timeout)
        url = str(base_url or "").strip()
        key = str(api_key or "")
        # Model changes invalidate in-flight answers just like endpoint changes.
        desired = (bool(enabled), url, key, self.model)
        if self._configured_input == desired and (self._url or not enabled):
            return
        self._configured_input = desired
        self._invalidate()
        if not enabled:
            self._status, self._detail = "disabled", "decision_backend_not_jev"
            return
        self._key = key
        if not url:
            self._status, self._detail = "missing", "not_configured"
            return
        try:
            parts = urlsplit(url)
            path = parts.path.rstrip("/")
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or parts.query or parts.fragment
                    or not (path in _BASE_PATHS or path.endswith(_ENDPOINT_PATHS))):
                raise ValueError
            _ = parts.port
            if key and parts.scheme == "http" and not is_loopback_host(parts.hostname or ""):
                # The key would travel in clear text to a remote host. Loopback
                # endpoints keep working, and a keyless remote endpoint stays usable.
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
            _cancel_request(task)
        self._url = ""
        self._key = ""
        self._host = ""
        self._status = "missing"
        self._detail = "not_configured"

    def snapshot(self) -> dict[str, Any]:
        """Panel-safe state: no credential, no full URL, no message content."""
        return {
            "configured": bool(self._url),
            "available": self._status == "available",
            "status": self._status,
            "detail": self._detail,
            "error_code": self._detail if self._status == "degraded" else "",
            "model": self.model,
            "served_model": self.served_model,
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
        """Ask one System One call; return validated answers or None.

        None is the only failure signal and it always leaves a code in
        `snapshot()["detail"]`; callers must treat it as "no decision available"
        rather than as a negative decision.
        """
        specs = _question_specs(questions)
        if specs is None:
            self._detail = "invalid_questions"
            return None
        payload = _payload(self.model, state, specs)
        if payload is None:
            self._detail = "invalid_state"
            return None
        if not self._url or self._closed:
            return None
        generation = self._generation
        deadline = self.timeout if timeout is None else _timeout_seconds(timeout, self.timeout)

        async def exchange():
            headers = {"Authorization": "Bearer " + self._key} if self._key else {}
            async with aiohttp.ClientSession(headers=headers, trust_env=False) as session:
                async with session.post(self._url, json=payload, allow_redirects=False) as response:
                    # 403 is a missing or malformed Authorization header and 401 an
                    # invalid key; both mean "this credential cannot decide anything".
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
                    request_id = str(response.headers.get("x-typesafe-request-id") or "")[:64]
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
            return answers, str(envelope.get("model") or "")[:MAX_MODEL_CHARS], request_id

        async def perform():
            self.calls += 1
            try:
                answers, served, request_id = await asyncio.wait_for(exchange(), timeout=deadline)
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
            self.served_model = served or self.model
            self.last_request_id = request_id
            return answers

        task = asyncio.create_task(perform())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            # A cancelled child is an internal fallback; cancellation of this
            # wait belongs to the caller and must propagate even during reload.
            # asyncio.wait preserves that distinction on Python 3.10 as well.
            await asyncio.wait({task})
            return None if task.cancelled() else task.result()
        except asyncio.CancelledError:
            _cancel_request(task)
            raise
        finally:
            if task.done():
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
            _cancel_request(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
        self._url = ""
        self._key = ""
        self._host = ""
        self._status, self._detail = "missing", "closed"
