"""Adapter for the open-source AgentJev decision.v1 service.

This protocol is independent of the TypeSafe Jev and Laya protocols.
"""
from __future__ import annotations

import json
import math

from .laya import LayaClient

API_VERSION = "agentjev.decision.v1"


def _unit(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid_probability")
    return float(value)


def _distribution(item, keys):
    values = item.get("distribution")
    if not isinstance(values, dict) or set(values) != set(keys):
        raise ValueError("invalid_distribution")
    probs = {key: _unit(values[key]) for key in keys}
    if abs(sum(probs.values()) - 1) > 1e-4:
        raise ValueError("invalid_distribution_sum")
    return probs


def _message_options(state):
    conversation = state.get("conversation", {})
    messages = {}
    for item in (conversation.get("background") or []) + (conversation.get("messages") or []):
        if isinstance(item, dict) and item.get("message_id") and isinstance(item.get("text"), str) and item["text"].strip():
            messages[str(item["message_id"])] = f"{item.get('author', 'unknown')}: {item['text'].strip()}"
    for item in (state.get("target_candidates") or {}).values():
        if isinstance(item, dict) and item.get("text_missing") is False and item.get("message_id") and isinstance(item.get("text"), str) and item["text"].strip():
            messages[str(item["message_id"])] = f"{item.get('author', 'unknown')}: {item['text'].strip()}"
    return messages


def build_request(state, questions):
    """Group target booleans into one exclusive choice, matching training cases."""
    if not isinstance(state, dict) or not isinstance(questions, dict):
        return None
    request = []
    mapping = {}
    targets = sorted((key for key in questions if key.startswith("target.")),
                     key=lambda key: int(key.split(".")[1]) if key.split(".")[1].isdigit() else 999)
    if targets:
        messages = _message_options(state)
        choices = {}
        seen = set()
        for key in targets:
            candidate = (state.get("target_candidates") or {}).get(key)
            message_id = candidate.get("message_id") if isinstance(candidate, dict) else None
            if not message_id or str(message_id) not in messages or message_id in seen:
                return None
            seen.add(message_id)
            choices[str(message_id)] = f"{message_id}: {messages[str(message_id)]}"
            mapping[str(message_id)] = key
        choices["none"] = "none: 以上候选都不是主要回应对象。"
        request.append({"id": "target", "type": "choice", "question": "选择本轮的主要消息回应对象；无法确定时选 none。", "options": choices})
    for key in ("join", "recipient_choice", "action", "reply_length"):
        spec = questions.get(key)
        if not isinstance(spec, dict):
            continue
        qid = "recipient" if key == "recipient_choice" else key
        kind = spec.get("type")
        prompt = spec.get("instructions")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        if kind == "noul" and key == "join":
            request.append({"id": qid, "type": "choice", "question": prompt,
                            "options": {"false": "否：本轮不参与。", "true": "是：本轮参与。"}})
        elif kind == "choice" and isinstance(spec.get("criteria"), dict):
            options = {str(k): f"{k}: {v}" for k, v in spec["criteria"].items()}
            if len(options) < 2:
                return None
            request.append({"id": qid, "type": "choice", "question": prompt, "options": options})
        else:
            return None
    if not request:
        return None
    # The service does not truncate input. Keep the current message and target
    # evidence at the front, bounded to fit the model's path token budget.
    conversation = state.get("conversation", {})
    current = {key: conversation[key] for key in ("text", "author", "explicit") if key in conversation}
    history = [{key: item[key] for key in ("message_id", "author", "text", "reply_to") if key in item}
               for item in (conversation.get("messages") or [])[-5:] if isinstance(item, dict)]
    compact = {"current": current, "messages": history,
               "target_candidates": {key: {field: val for field, val in value.items()
                                             if field in ("message_id", "author", "text")}
                                     for key, value in (state.get("target_candidates") or {}).items()
                                     if isinstance(value, dict)}}
    return {"state": json.dumps(compact, ensure_ascii=False, separators=(",", ":")),
            "questions": request}, mapping


def parse_response(response, questions, target_mapping):
    if not isinstance(response, dict) or response.get("api_version") != API_VERSION:
        raise ValueError("invalid_api_version")
    results = response.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("invalid_results")
    rows = results[0].get("answers")
    if not isinstance(rows, list) or len({row.get("id") for row in rows if isinstance(row, dict)}) != len(rows):
        raise ValueError("invalid_answers")
    by_id = {row["id"]: row for row in rows if isinstance(row, dict)}
    expected_ids = {"recipient" if key == "recipient_choice" else key
                    for key in ("join", "recipient_choice", "action", "reply_length") if key in questions}
    if target_mapping:
        expected_ids.add("target")
    if set(by_id) != expected_ids:
        raise ValueError("unexpected_answers")
    answers = {}
    for key in ("join", "recipient_choice", "action", "reply_length"):
        spec = questions.get(key)
        if spec is None:
            continue
        qid = "recipient" if key == "recipient_choice" else key
        item = by_id.get(qid)
        if not isinstance(item, dict):
            raise ValueError("missing_answer")
        if key == "join":
            probs = _distribution(item, ("false", "true"))
            if item.get("type") != "choice" or item.get("value") not in probs or probs[item["value"]] < max(probs.values()) - 1e-6:
                raise ValueError("invalid_join")
            answers[key] = {"type": "noul", "noul": probs["true"]}
        else:
            keys = tuple(spec["criteria"])
            probs = _distribution(item, keys)
            choice = item.get("value")
            if item.get("type") != "choice" or choice not in probs or probs[choice] < max(probs.values()) - 1e-6:
                raise ValueError("invalid_choice")
            answers[key] = {"type": "choice", "choice": choice, "probabilities": probs,
                            "confidence": max(probs.values())}
    if target_mapping:
        item = by_id.get("target")
        if not isinstance(item, dict):
            raise ValueError("missing_target")
        probs = _distribution(item, (*target_mapping, "none"))
        choice = item.get("value")
        if item.get("type") != "choice" or choice not in probs or probs[choice] < max(probs.values()) - 1e-6:
            raise ValueError("invalid_target")
        for message_id, key in target_mapping.items():
            answers[key] = {"type": "noul", "noul": probs[message_id]}
    return answers


class AgentJevClient:
    def __init__(self, *, enabled=True, base_url="", timeout=1.5, internal_hosts=()):
        self.transport = LayaClient(enabled=enabled, base_url=base_url, timeout=timeout,
                                    internal_hosts=internal_hosts)
        self.calls = 0
        self.failures = 0
        self._available = False
        self._detail = "not_called"

    def configure(self, **kwargs):
        self.transport.configure(**kwargs)
        self._available = False
        self._detail = "not_called"

    def snapshot(self):
        result = self.transport.snapshot()
        result.update(available=self._available, status="available" if self._available else result["status"],
                      detail=self._detail, calls=self.calls, failures=self.failures)
        return result

    async def close(self):
        await self.transport.close()

    async def evaluate(self, *, state, questions, timeout=None):
        built = build_request(state, questions)
        if built is None:
            self._detail = "invalid_input"
            return None
        self.calls += 1
        payload, mapping = built
        response = await self.transport.request_json("/api/evaluate", payload,
            timeout=self.transport.timeout if timeout is None else timeout)
        if response is None:
            self.failures += 1
            self._available = False
            self._detail = "request_failed"
            return None
        try:
            answers = parse_response(response, questions, mapping)
        except (KeyError, TypeError, ValueError):
            self.failures += 1
            self._available = False
            self._detail = "invalid_response"
            return None
        self._available = True
        self._detail = "ready"
        return {"answers": answers, "model_version": str(response.get("model", "")),
                "usage": response.get("usage", {})}
