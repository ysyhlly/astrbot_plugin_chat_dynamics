"""Kev's native System One endpoint as an independent decision backend."""

from __future__ import annotations

import asyncio
import math
import os
import time

from .laya import LayaClient
from ..decision_state import build_decision_snapshot, snapshot_json, SCHEMA_VERSION

MODEL_ALIAS = "kev-latest"
MAX_QUESTIONS = 32


def _probability(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid_probability")
    return float(value)


def _distribution(raw, keys):
    if not isinstance(raw, dict) or set(raw) != set(keys):
        raise ValueError("invalid_distribution_keys")
    values = {key: _probability(raw[key]) for key in keys}
    # Kev rounds each value to four decimals; allow the corresponding sum error.
    if abs(sum(values.values()) - 1) > max(.0002, len(keys) * .000051):
        raise ValueError("invalid_distribution_sum")
    return values


def validate_kev_answers(envelope, questions):
    """Reject the whole response if a task, candidate, or probability is invalid."""
    if not isinstance(envelope, dict) or envelope.get("model") != MODEL_ALIAS:
        raise ValueError("invalid_model_alias")
    raw = envelope.get("answers")
    if not isinstance(raw, dict) or set(raw) != set(questions):
        raise ValueError("invalid_answer_ids")
    answers = {}
    for qid, spec in questions.items():
        item = raw[qid]
        kind = spec.get("type")
        if not isinstance(item, dict) or item.get("type") != kind:
            raise ValueError("invalid_answer_type")
        if kind == "noul":
            answers[qid] = {"type": "noul", "noul": _probability(item.get("noul"))}
        elif kind == "choice":
            criteria = spec.get("criteria")
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
                raise ValueError("invalid_choice_criteria")
            probs = _distribution(item.get("probabilities"), tuple(criteria))
            choice = item.get("choice")
            if choice not in probs or probs[choice] < max(probs.values()) - .0001:
                raise ValueError("invalid_choice")
            answers[qid] = {"type": "choice", "choice": choice,
                            "probabilities": probs,
                            "confidence": _probability(item.get("confidence"))}
        elif kind == "score":
            criteria = spec.get("criteria")
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 255:
                raise ValueError("invalid_score_criteria")
            probs = _distribution(item.get("probabilities"),
                                  tuple(str(i) for i in range(len(criteria))))
            score = item.get("score")
            if type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError("invalid_score")
            expected = sum(i * probs[str(i)] for i in range(len(criteria)))
            if not 0 <= score <= len(criteria) - 1 or abs(score - expected) > .01:
                raise ValueError("invalid_score")
            answers[qid] = {"type": "score", "score": float(score),
                            "probabilities": probs,
                            "confidence": _probability(item.get("confidence"))}
        else:
            raise ValueError("invalid_question_type")
    return answers


class KevClient:
    """Uses the existing private-network transport but Kev's own wire contract."""

    def __init__(self, *, enabled=False, base_url="", timeout=1.5,
                 checkpoint_id="", internal_hosts=(), api_key_env="KEV_API_KEY"):
        self.transport = LayaClient(enabled=enabled, base_url=base_url,
                                    timeout=timeout, internal_hosts=internal_hosts)
        self.checkpoint_id = str(checkpoint_id or "").strip()
        self.api_key_env = str(api_key_env or "KEV_API_KEY")
        self.calls = 0
        self.failures = 0
        self._model_card = {}
        self._model_lock = asyncio.Lock()
        self._detail = "not_called"

    def configure(self, *, checkpoint_id=None, api_key_env=None, **kwargs):
        if checkpoint_id is not None:
            self.checkpoint_id = str(checkpoint_id or "").strip()
        if api_key_env is not None:
            self.api_key_env = str(api_key_env or "KEV_API_KEY")
        self.transport.configure(**kwargs)
        self._model_card = {}
        self._detail = "not_called"

    def snapshot(self):
        view = self.transport.snapshot()
        available = self._detail == "answered" and view["configured"]
        view.update(available=available,
                    status="available" if available else view["status"],
                    detail=self._detail,
                    calls=self.calls, failures=self.failures,
                    checkpoint_id=self.checkpoint_id,
                    device=self._model_card.get("device", ""),
                    dtype=self._model_card.get("dtype", ""))
        return view

    async def close(self):
        await self.transport.close()
        self._detail = "closed"

    async def _verify_model(self, *, timeout, token):
        if not self.checkpoint_id:
            self._detail = "checkpoint_not_pinned"
            return False
        async with self._model_lock:
            listing = await self.transport.request_json("/v1/models", method="GET",
                                                        token=token, timeout=timeout)
            cards = listing.get("models") if isinstance(listing, dict) else None
            card = next((item for item in cards if isinstance(item, dict)
                         and item.get("name") == MODEL_ALIAS), None) if isinstance(cards, list) else None
            if not card or card.get("run") != self.checkpoint_id or not card.get("device"):
                self._detail = "checkpoint_mismatch"
                return False
            if card.get("strict_context") is not True:
                self._detail = "unsafe_server_context"
                return False
            self._model_card = card
            return True

    async def evaluate(self, *, state, questions, timeout=None, diagnostics=None):
        def status(code):
            self._detail = code
            if diagnostics is not None:
                diagnostics["status"] = code

        if not isinstance(questions, dict) or not 0 < len(questions) <= MAX_QUESTIONS:
            status("invalid_questions")
            return None
        snapshot = build_decision_snapshot(state, questions)
        if snapshot is None:
            status("invalid_snapshot")
            return None
        if diagnostics is not None:
            diagnostics.update(state_format=SCHEMA_VERSION,
                               state_bytes=len(snapshot_json(snapshot).encode("utf-8")),
                               question_count=len(questions))
        limit = self.transport.timeout if timeout is None else max(.05, float(timeout))
        deadline = time.monotonic() + limit
        token = os.environ.get(self.api_key_env, "")
        identity_started = time.monotonic()
        if not await self._verify_model(timeout=limit, token=token):
            status(self._detail)
            return None
        if diagnostics is not None:
            diagnostics["identity_check_ms"] = (time.monotonic() - identity_started) * 1000
        remaining = deadline - time.monotonic()
        if remaining <= .01:
            status("timeout")
            return None
        self.calls += 1
        payload = {"model": MODEL_ALIAS, "state": snapshot, "questions": questions}
        envelope = await self.transport.request_json("/v1/systemone", payload,
                                                     timeout=remaining, token=token,
                                                     diagnostics=diagnostics)
        if envelope is None:
            self.failures += 1
            status((diagnostics or {}).get("status", "request_failed"))
            return None
        try:
            answers = validate_kev_answers(envelope, questions)
        except (TypeError, ValueError):
            self.failures += 1
            status("invalid_response")
            return None
        if diagnostics is not None:
            server_ms = envelope.get("latency_ms")
            if type(server_ms) in (int, float) and math.isfinite(server_ms) and server_ms >= 0:
                diagnostics["server_model_ms"] = float(server_ms)
            usage = envelope.get("usage")
            input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            if type(input_tokens) is int and input_tokens >= 0:
                diagnostics["input_tokens"] = input_tokens
        status("answered")
        return {"answers": answers, "model_version": self.checkpoint_id,
                "usage": envelope.get("usage", {}),
                "checkpoint_sha256": ""}
