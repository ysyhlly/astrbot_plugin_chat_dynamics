"""Adapter for the open-source AgentJev decision.v1 service.

This protocol is independent of the TypeSafe Jev and Laya protocols.
"""
from __future__ import annotations

import math
import json

from .laya import LayaClient
from ..agentjev_state import pack_state, path_fits, target_option

API_VERSION = "agentjev.decision.v1"
CMDCODE_INPUT_FORMAT = "cmdcode_full_input_soft_v1"
CMDCODE_QUESTION = "Should a naturally conversational group assistant speak now in this hypothetical situation?"
CMDCODE_OPTIONS = {"false": "否：本轮不参与。", "true": "是：本轮参与。"}
OBSERVER_INPUT_FORMAT = "agentjev_observer_v1"
MAX_SHADOW_QUESTIONS = 32
MAX_SHADOW_OPTIONS = 16


def _question_text(instructions):
    if isinstance(instructions, str):
        return instructions.strip()
    if isinstance(instructions, dict) and instructions:
        if any(not isinstance(value, str) or not value.strip() for value in instructions.values()):
            return ""
        return " ".join(instructions.values()).strip()
    return ""


def build_all_tasks_shadow_request(state, questions):
    """Observe each original typed question, preserving independent target booleans."""
    if (not isinstance(state, dict) or not state or not isinstance(questions, dict)
            or not 0 < len(questions) <= MAX_SHADOW_QUESTIONS):
        return None
    full_turn = build_cmdcode_request(state, {"join": questions["join"]}) if "join" in questions else None
    if full_turn is not None:
        packed = full_turn[0]["state"]
        state_format = CMDCODE_INPUT_FORMAT
    else:
        try:
            packed = json.dumps(state, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return None
        state_format = OBSERVER_INPUT_FORMAT
    request = []
    for qid, spec in questions.items():
        if (not isinstance(qid, str) or not qid or len(qid) > 64
                or not isinstance(spec, dict)):
            return None
        kind = spec.get("type")
        prompt = _question_text(spec.get("instructions"))
        if qid == "join" and full_turn is not None:
            prompt = CMDCODE_QUESTION
        if not prompt:
            return None
        if kind == "noul":
            options = CMDCODE_OPTIONS.copy()
        elif kind == "choice":
            criteria = spec.get("criteria")
            if (not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_SHADOW_OPTIONS
                    or any(not isinstance(key, str) or not key or not isinstance(value, str)
                           or not value.strip() for key, value in criteria.items())):
                return None
            options = {key: f"{key}: {value}" for key, value in criteria.items()}
        elif kind == "score":
            criteria = spec.get("criteria")
            if (not isinstance(criteria, list) or not 2 <= len(criteria) <= MAX_SHADOW_OPTIONS
                    or any(not isinstance(value, str) or not value.strip() for value in criteria)):
                return None
            options = {str(index): f"{index}: {value}" for index, value in enumerate(criteria)}
        else:
            return None
        request.append({"id": qid, "type": "choice", "question": prompt, "options": options})
    return {"all_tasks_shadow": True, "state_format": state_format,
            "state": packed, "questions": request}, {}


def parse_all_tasks_shadow_response(response, questions):
    if not isinstance(response, dict) or response.get("api_version") != API_VERSION:
        raise ValueError("invalid_api_version")
    results = response.get("results")
    if (not isinstance(results, list) or len(results) != 1 or
            not isinstance(results[0], dict) or not isinstance(results[0].get("answers"), list)):
        raise ValueError("invalid_results")
    rows = results[0]["answers"]
    if (len(rows) != len(questions) or any(not isinstance(row, dict) or
            not isinstance(row.get("id"), str) for row in rows)):
        raise ValueError("invalid_answers")
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != set(questions):
        raise ValueError("unexpected_answers")
    answers = {}
    for qid, spec in questions.items():
        item = by_id[qid]
        kind = spec["type"]
        criteria = spec.get("criteria")
        keys = (("false", "true") if kind == "noul" else
                tuple(criteria) if kind == "choice" else
                tuple(str(index) for index in range(len(criteria))))
        probs = _distribution(item, keys)
        selected = item.get("value")
        if (item.get("type") != "choice" or selected not in probs or
                probs[selected] < max(probs.values()) - 1e-6):
            raise ValueError("invalid_choice")
        if kind == "noul":
            answers[qid] = {"type": "noul", "noul": probs["true"]}
        elif kind == "choice":
            answers[qid] = {"type": "choice", "choice": selected,
                            "probabilities": probs, "confidence": max(probs.values())}
        else:
            answers[qid] = {"type": "score",
                            "score": sum(index * probs[str(index)] for index in range(len(keys))),
                            "probabilities": probs, "confidence": max(probs.values())}
    return answers


def build_cmdcode_request(state, questions):
    """Recreate the complete Command Code input used by the soft-label export."""
    if not isinstance(state, dict) or not isinstance(questions, dict) or "join" not in questions:
        return None
    conversation = state.get("conversation")
    persona = state.get("persona")
    bot_id = state.get("bot_speaker_id")
    environment = state.get("environment")
    # An explicitly empty persona is a valid AstrBot setting. Preserve it in the
    # Command Code wire format instead of falling back to the much larger observer state.
    if (not isinstance(conversation, dict) or not isinstance(persona, str)
            or not isinstance(bot_id, str) or not bot_id.strip()
            or not isinstance(environment, dict) or environment.get("schema_version") != 1):
        return None
    current = conversation.get("messages")
    background = conversation.get("background")
    if not isinstance(current, list) or not current or not isinstance(background, list):
        return None
    raw_chat = background + current
    if not all(isinstance(message, dict) for message in raw_chat):
        return None
    chat = []
    for message in raw_chat[-8:]:
        message_id, speaker, raw_text = (message.get("message_id"), message.get("author"),
                                         message.get("text"))
        timestamp = message.get("timestamp")
        if (not isinstance(message_id, str) or not message_id or
                not isinstance(speaker, str) or not speaker or
                not isinstance(raw_text, str) or not raw_text or
                message.get("text_missing") is True or
                type(timestamp) not in (int, float) or not math.isfinite(timestamp) or
                timestamp < 0):
            return None
        reply = message.get("reply_to")
        if reply is not None and not isinstance(reply, str):
            return None
        semantics = message.get("semantics")
        if semantics is not None and not isinstance(semantics, dict):
            return None
        quoted_author = semantics.get("quoted_author_id") if semantics else None
        if quoted_author is not None and not isinstance(quoted_author, str):
            return None
        mentioned = message.get("mentioned_users")
        if not isinstance(mentioned, (list, tuple)) or any(
                not isinstance(user, str) or not user for user in mentioned):
            return None
        chat.append({"message_id": message_id, "timestamp": int(timestamp),
                     "speaker": speaker, "text": raw_text, "reply_to": reply or None,
                     "reply_to_user": quoted_author or None, "mentions": list(mentioned)})
    context_id = current[-1].get("message_id")
    if chat[-1]["message_id"] != context_id or len({row["message_id"] for row in chat}) != len(chat):
        return None
    full_input = {"context_id": context_id, "persona": persona, "background": {},
                  "bot_speaker_id": bot_id, "chat": chat, "environment": environment}
    try:
        packed = json.dumps(full_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                            allow_nan=False)
    except (TypeError, ValueError):
        return None
    return {"state_format": CMDCODE_INPUT_FORMAT, "state": packed,
            "questions": [{"id": "join", "type": "choice", "question": CMDCODE_QUESTION,
                           "options": CMDCODE_OPTIONS.copy()}]}, {}


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
            choices[str(message_id)] = target_option(str(message_id), messages[str(message_id)])
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
        if isinstance(prompt, dict):
            prompt = " ".join(str(value) for value in prompt.values())
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
    try:
        packed, _ = pack_state(state)
    except (KeyError, TypeError, ValueError):
        return None
    if any(not path_fits(packed, item["question"], list(item["options"].values()))
           for item in request):
        return None
    return {"state": packed, "questions": request}, mapping


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
    def __init__(self, *, enabled=True, base_url="", timeout=1.5, internal_hosts=(),
                 input_format="legacy", all_tasks_shadow=False):
        self.transport = LayaClient(enabled=enabled, base_url=base_url, timeout=timeout,
                                    internal_hosts=internal_hosts)
        self.input_format = input_format
        self.all_tasks_shadow = bool(all_tasks_shadow)
        self.calls = 0
        self.failures = 0
        self._available = False
        self._detail = "not_called"

    def configure(self, **kwargs):
        self.input_format = kwargs.pop("input_format", self.input_format)
        self.all_tasks_shadow = bool(kwargs.pop("all_tasks_shadow", self.all_tasks_shadow))
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

    async def evaluate(self, *, state, questions, timeout=None, diagnostics=None):
        def status(code):
            if diagnostics is not None:
                diagnostics["status"] = code

        cmdcode = self.input_format == CMDCODE_INPUT_FORMAT
        all_shadow = cmdcode and self.all_tasks_shadow
        built = (build_all_tasks_shadow_request(state, questions) if all_shadow else
                 build_cmdcode_request(state, questions) if cmdcode else
                 build_request(state, questions))
        if built is None:
            self._detail = "invalid_input"
            status("invalid_input")
            return None
        self.calls += 1
        payload, mapping = built
        if diagnostics is not None:
            diagnostics["state_format"] = payload.get("state_format", "legacy")
            diagnostics["state_bytes"] = len(payload["state"].encode("utf-8"))
            diagnostics["question_count"] = len(payload["questions"])
        response = await self.transport.request_json("/api/evaluate", payload,
            timeout=self.transport.timeout if timeout is None else timeout,
            diagnostics=diagnostics)
        if response is None:
            self.failures += 1
            self._available = False
            self._detail = "request_failed"
            if diagnostics is not None and diagnostics.get("status") == "answered":
                status("invalid_response")
            return None
        try:
            answers = (parse_all_tasks_shadow_response(response, questions) if all_shadow else
                       parse_response(response, {"join": questions["join"]} if cmdcode else questions,
                                      mapping))
        except (KeyError, TypeError, ValueError):
            self.failures += 1
            self._available = False
            self._detail = "invalid_response"
            status("invalid_response")
            return None
        self._available = True
        self._detail = "ready"
        status("answered")
        return {"answers": answers, "model_version": str(response.get("model", "")),
                "checkpoint_sha256": response.get("checkpoint_sha256", ""),
                "usage": response.get("usage", {})}
