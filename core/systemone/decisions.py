"""OpenAI Decisions transport adapted to Chat Dynamics' closed question contract.

API contract: https://developers.openai.com/api/docs/guides/decisions
This provider never uses Chat Completions or generates a conversational reply.
"""

import json
import math
from urllib.parse import urlsplit

from astrbot.core.config.default import CONFIG_METADATA_2
from astrbot.core.provider.entities import ProviderType, TokenUsage
from astrbot.core.provider.register import (
    provider_cls_map,
    provider_registry,
    register_provider_adapter,
)

from .client import SystemOneError, endpoint_url, request_json, validate_answers
from .provider import DEFAULT_CONFIG as SYSTEMONE_CONFIG
from .provider import SystemOneProvider

PROVIDER_TYPE = "openai_decisions"
DEFAULT_CONFIG = {
    **SYSTEMONE_CONFIG,
    "type": PROVIDER_TYPE,
    "provider": "openai_decisions",
    "id": "openai-decisions",
    "api_base": "https://api.openai.com/v1",
    "model": "gpt-6-luna",
    "decision_path": "/v1/decisions",
}


def _invalid():
    return SystemOneError(
        "OpenAI Decisions 返回的答案不符合本轮问题。", code="invalid_decision_answers"
    )


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def convert_questions(questions):
    """Preserve question IDs, candidate values, order and all rubric instructions."""
    converted = []
    for qid, spec in questions.items():
        kind, criteria = spec["type"], spec.get("criteria")
        question = {"name": qid, "instructions": spec["instructions"]}
        if kind == "noul":
            question["type"] = "predicate"
            if criteria:
                question["instructions"] += "\nCondition rubric: " + json.dumps(
                    criteria, ensure_ascii=False
                )
        elif kind == "choice":
            if (
                not isinstance(criteria, dict)
                or not 1 <= len(criteria) <= 255
                or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in criteria.items()
                )
            ):
                raise SystemOneError("OpenAI choice 候选无效。", code="invalid_questions")
            if len(criteria) == 1:
                # /decisions requires >=2 choices. A predicate judges the sole
                # offered candidate without inventing a second option or certainty.
                question["type"] = "predicate"
                question["instructions"] += (
                    "\nThere is only one candidate: "
                    + json.dumps(criteria, ensure_ascii=False)
                    + ". Is this candidate an appropriate answer to the question?"
                )
            else:
                question.update(
                    type="choice",
                    choices=[
                        {"value": key, "description": value} for key, value in criteria.items()
                    ],
                )
        elif kind == "score":
            if (
                not isinstance(criteria, list)
                or not 2 <= len(criteria) <= 10
                or any(not isinstance(value, str) for value in criteria)
            ):
                raise SystemOneError(
                    "OpenAI score 需要 2～10 个文字等级。", code="invalid_questions"
                )
            question.update(type="score", levels=[{"label": value} for value in criteria])
        else:
            raise SystemOneError("不支持该决策问题类型。", code="invalid_questions")
        converted.append(question)
    return converted


def convert_answers(envelope, questions, sent):
    """Check ordered names/types and map reported values without deriving confidence."""
    rows = envelope.get("answers") if isinstance(envelope, dict) else None
    if not isinstance(rows, list) or len(rows) != len(sent):
        raise _invalid()
    answers = {}
    for (qid, spec), request, row in zip(questions.items(), sent, rows):
        if not isinstance(row, dict) or row.get("name") != qid:
            raise _invalid()
        if row.get("type") == "refusal":
            # The shared contract requires complete evidence. A refusal cannot
            # become a guessed probability or permission to take part.
            raise SystemOneError("OpenAI Decisions 拒绝了本轮问题。", code="decision_refused")
        if row.get("type") != request["type"]:
            raise _invalid()
        kind = spec["type"]
        if request["type"] == "predicate":
            probability = row.get("probability")
            if not _probability(probability):
                raise _invalid()
            if kind == "noul":
                answers[qid] = {"type": "noul", "noul": probability}
            else:
                value = next(iter(spec["criteria"]))
                answers[qid] = {
                    "type": "choice",
                    "choice": value,
                    "confidence": probability,
                    "probabilities": {value: probability},
                }
            continue
        if not _probability(row.get("confidence")):
            raise _invalid()
        probabilities = row.get("probabilities")
        expected = (
            list(spec["criteria"]) if kind == "choice" else list(range(len(spec["criteria"])))
        )
        if not isinstance(probabilities, list) or len(probabilities) != len(expected):
            raise _invalid()
        distribution = {}
        for item in probabilities:
            if not isinstance(item, dict) or not _probability(item.get("probability")):
                raise _invalid()
            value = item.get("value")
            if (
                type(value) is not (str if kind == "choice" else int)
                or value not in expected
                or value in distribution
            ):
                raise _invalid()
            if kind == "score" and item.get("label") != spec["criteria"][value]:
                raise _invalid()
            distribution[value] = item["probability"]
        if not math.isclose(sum(distribution.values()), 1, abs_tol=0.02):
            raise _invalid()
        if kind == "choice":
            value = row.get("choice")
            if not isinstance(value, str) or value not in expected:
                raise _invalid()
        else:
            value = row.get("score")
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= len(expected) - 1
            ):
                raise _invalid()
        answers[qid] = {
            "type": kind,
            kind: value,
            "confidence": row["confidence"],
            "probabilities": {str(k): v for k, v in distribution.items()},
        }
    return validate_answers({"answers": answers}, questions)


def reported_usage(envelope):
    """Cache reads/writes are already part of input_tokens; retain them separately."""
    value = envelope.get("usage") if isinstance(envelope, dict) else None
    if not isinstance(value, dict):
        return {"reported": False}
    result = {"reported": True}
    for source, names in (
        (value, ("input_tokens", "output_tokens", "total_tokens")),
        (value.get("input_tokens_details"), ("cached_tokens", "cache_write_tokens")),
        (value.get("output_tokens_details"), ("reasoning_tokens",)),
    ):
        for name in names:
            count = source.get(name) if isinstance(source, dict) else None
            if type(count) is int and count >= 0:
                result[name] = count
    return result


class OpenAIDecisionsProvider(SystemOneProvider):
    decision_protocol = "openai_decisions"

    def __init__(self, provider_config, provider_settings):
        super().__init__({**DEFAULT_CONFIG, **provider_config}, provider_settings)
        self.last_decision_usage = None

    async def systemone_count_tokens(self, *, state, questions, timeout=3):
        # This endpoint has no System One tokenizer capability. Never probe the
        # unrelated /systemone/count_tokens route or guess a token window.
        return None

    async def get_models(self):
        models = await super().get_models()
        if urlsplit(self.provider_config.get("api_base", "")).hostname == "api.openai.com":
            return [name for name in models if name == "gpt-6-luna"]
        return models

    async def text_chat(self, prompt=None, contexts=None, model=None, **kwargs):
        response = await super().text_chat(prompt=prompt, contexts=contexts, model=model, **kwargs)
        usage = self.last_decision_usage or {}
        input_tokens, cached, output = (usage.get(name) for name in
                                       ("input_tokens", "cached_tokens", "output_tokens"))
        if (all(type(value) is int for value in (input_tokens, cached, output))
                and 0 <= cached <= input_tokens and output >= 0):
            response.usage = TokenUsage(input_other=input_tokens-cached, input_cached=cached,
                                        output=output)
        return response

    async def systemone_evaluate(self, *, state, questions, timeout=None, model=None):
        self.last_decision_usage = None
        normalized = self._validated_questions(state=state, questions=questions)
        sent = convert_questions(normalized)
        json_data = {"state": state} if isinstance(state, list) else state
        payload = {
            "model": model or self.get_model(),
            "input": state
            if isinstance(state, str)
            else [{"role": "user", "content": [{"type": "input_json", "data": json_data}]}],
            "questions": sent,
        }
        try:
            size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
        except (ValueError, TypeError):
            raise SystemOneError(
                "OpenAI Decisions 输入不是有效 JSON。", code="invalid_questions"
            ) from None
        if size > 256 * 1024:
            raise SystemOneError("OpenAI Decisions 请求过大。", code="request_budget_exceeded")
        limit = self.timeout if timeout is None else min(self.timeout, float(timeout))
        if not math.isfinite(limit) or limit <= 0:
            raise SystemOneError("决策超时无效。", code="timeout")
        envelope = await request_json(
            "POST",
            endpoint_url(
                self.provider_config.get("api_base", ""),
                self.provider_config.get("decision_path", "/v1/decisions"),
            ),
            key=self.get_current_key(),
            headers=self.request_headers,
            timeout=limit,
            payload=payload,
            max_body=256 * 1024,
            client=self._http,
        )
        self.last_decision_usage = reported_usage(envelope)
        return convert_answers(envelope, normalized, sent)


def register_adapter():
    old = provider_cls_map.get(PROVIDER_TYPE)
    if old is not None and old.cls_type is not OpenAIDecisionsProvider:
        if not getattr(old.cls_type, "is_chat_dynamics_builtin", False):
            raise SystemOneError("OpenAI Decisions 模型服务类型冲突。", code="adapter_conflict")
        provider_registry[:] = [item for item in provider_registry if item.type != PROVIDER_TYPE]
        del provider_cls_map[PROVIDER_TYPE]
    if PROVIDER_TYPE not in provider_cls_map:
        register_provider_adapter(
            PROVIDER_TYPE,
            "OpenAI Decisions 决策模型",
            provider_type=ProviderType.DECISION,
            default_config_tmpl=None,
            provider_display_name="OpenAI Decisions",
        )(OpenAIDecisionsProvider)
    CONFIG_METADATA_2["provider_group"]["metadata"]["provider"]["config_template"][
        "OpenAI Decisions"
    ] = dict(DEFAULT_CONFIG)


register_adapter()
