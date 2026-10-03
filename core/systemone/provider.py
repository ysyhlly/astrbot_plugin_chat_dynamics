"""An AstrBot-native provider with normal address, API key and model fields."""

import json
import math
import os
from urllib.parse import urlsplit

from astrbot.api.provider import Provider
from astrbot.core.config.default import CONFIG_METADATA_2
from astrbot.core.provider.entities import LLMResponse, ProviderType
from astrbot.core.provider.register import (
    provider_cls_map,
    provider_registry,
    register_provider_adapter,
)

from .client import (
    SystemOneError,
    SystemOneHTTPClient,
    endpoint_url,
    parse_models,
    request_json,
    validate_answers,
)

PROVIDER_TYPE = "systemone_catalog"
DEFAULT_CONFIG = {
    "type": PROVIDER_TYPE,
    "provider": "systemone",
    "provider_type": "chat_completion",
    "id": "systemone",
    "enable": False,
    "api_base": "",
    "key": [""],
    "model": "jev-latest",
    "timeout": 30,
    "models_path": "/v1/models",
    "decision_path": "/v1/systemone",
    "custom_headers": {},
}

class SystemOneProvider(Provider):
    is_systemone_provider = True
    is_chat_dynamics_builtin = True

    def __init__(self, provider_config, provider_settings):
        super().__init__(provider_config, provider_settings)
        self.set_model(provider_config.get("model") or "jev-latest")
        keys = self.get_keys()
        self._key = keys[0] if keys else ""
        timeout = float(provider_config.get("timeout", 30))
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise SystemOneError("超时应为 0～300 秒之间的有限数值。")
        self.timeout = timeout
        self._http = SystemOneHTTPClient()

    async def terminate(self):
        # AstrBot calls this on reload/unload and for temporary catalog providers.
        await self._http.close()

    def get_current_key(self):
        key = self._key or ""
        if key.startswith("$"):
            name = key[1:]
            if name.startswith("{") and name.endswith("}"):
                name = name[1:-1]
            if name not in os.environ:
                raise SystemOneError("模型服务引用的密钥环境变量尚未设置。")
            return os.environ[name]
        return key

    def set_key(self, key):
        self._key = key

    async def get_models(self):
        base_url = self.provider_config.get("api_base", "")
        models_path = self.provider_config.get("models_path", "/v1/models")
        zen = urlsplit(base_url).hostname == "opencode.ai" and urlsplit(
            base_url
        ).path.rstrip("/") in {"/zen", "/zen/v1", "/zen/v1/systemone", "/zen/v1/models"}
        # v0.2.0 stored the decision route as the list route; keep existing Zen sources working.
        if zen and models_path == "/v1/systemone":
            models_path = "/v1/models"
        url = endpoint_url(
            base_url,
            models_path,
        )
        payload = await request_json(
            "GET",
            url,
            key=self.get_current_key(),
            headers=self.request_headers,
            timeout=self.timeout,
            client=self._http,
        )
        models = parse_models(payload)
        if zen:
            # Zen's full catalog also contains chat models using other protocols.
            models = [model for model in models if model.startswith("jev-")]
        return models

    async def systemone_evaluate(self, *, state, questions, timeout=None, model=None):
        if not isinstance(state, (str, dict, list)) or not state:
            raise SystemOneError("System One 需要非空 state。")
        if not isinstance(questions, dict) or not 0 < len(questions) <= 32:
            raise SystemOneError("System One 需要 1～32 个 typed questions。")
        normalized_questions = {}
        for qid, spec in questions.items():
            if (
                not isinstance(qid, str)
                or not qid
                or not isinstance(spec, dict)
                or spec.get("type") not in {"choice", "score", "noul"}
            ):
                raise SystemOneError("System One 问题格式无效。", code="invalid_questions")
            instructions = spec.get("instructions")
            if isinstance(instructions, dict) and instructions and all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in instructions.items()
            ):
                # Chat Dynamics supplies question/focus separately. Preserve every
                # instruction while using the text form accepted by native gateways.
                instructions = json.dumps(instructions, ensure_ascii=False)
            if not isinstance(instructions, str) or not instructions.strip():
                raise SystemOneError("System One 问题说明无效。", code="invalid_questions")
            normalized_questions[qid] = {**spec, "instructions": instructions}
            if spec["type"] == "choice" and (
                not isinstance(spec.get("criteria"), dict) or not spec["criteria"]
            ):
                raise SystemOneError("choice 问题需要 criteria 对象。")
            if spec["type"] == "score" and (
                not isinstance(spec.get("criteria"), list) or len(spec["criteria"]) < 2
            ):
                raise SystemOneError("score 问题需要至少两个等级。")
        selected_model = model or self.get_model()
        payload = {"model": selected_model, "state": state, "questions": normalized_questions}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 256 * 1024:
            raise SystemOneError("System One 请求过大。")
        limit = self.timeout if timeout is None else min(self.timeout, float(timeout))
        if not math.isfinite(limit) or limit <= 0:
            raise SystemOneError("决策超时无效。")
        url = endpoint_url(
            self.provider_config.get("api_base", ""),
            self.provider_config.get("decision_path", "/v1/systemone"),
        )
        envelope = await request_json(
            "POST",
            url,
            key=self.get_current_key(),
            headers=self.request_headers,
            timeout=limit,
            payload=payload,
            max_body=256 * 1024,
            client=self._http,
        )
        return validate_answers(envelope, questions)

    async def text_chat(self, prompt=None, contexts=None, model=None, **kwargs):
        # Typed annotations can use the normal llm_generate interface.
        if (
            kwargs.get("image_urls")
            or kwargs.get("audio_urls")
            or kwargs.get("func_tool")
        ):
            raise SystemOneError(
                "System One 只支持文本决策，不支持图片、音频或工具调用。"
            )
        try:
            payload = json.loads(prompt) if isinstance(prompt, str) else prompt
            if (
                not isinstance(payload, dict)
                or "state" not in payload
                or "questions" not in payload
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise SystemOneError(
                "这是 System One 决策模型；请在 Chat Dynamics 的 Jev 决策后端选择它，回复模型继续使用普通 LLM。"
            ) from None
        answers = await self.systemone_evaluate(
            state=payload["state"], questions=payload["questions"], model=model
        )
        return LLMResponse(
            role="assistant",
            completion_text=json.dumps({"answers": answers}, ensure_ascii=False),
        )

    async def test(self, timeout=45.0):
        await self.systemone_evaluate(
            state="My payments have failed for three days and I am losing sales. Please help now.",
            questions={
                "is_urgent": {
                    "type": "noul",
                    "instructions": "Does this request need urgent attention?",
                },
                "department": {
                    "type": "choice",
                    "instructions": "Which team should handle this?",
                    "criteria": {
                        "billing": "Charges and payments",
                        "shipping": "Delivery",
                        "returns": "Refunds and exchanges",
                    },
                },
                "frustration": {
                    "type": "score",
                    "instructions": "How frustrated is the customer?",
                    "criteria": ["Calm", "Frustrated", "Very angry"],
                },
            },
            timeout=timeout,
        )


def register_adapter():
    """Keep the original provider type and templates so saved models still work."""
    old = provider_cls_map.get(PROVIDER_TYPE)
    if old is not None and old.cls_type is not SystemOneProvider:
        if not getattr(old.cls_type, "is_systemone_provider", False):
            raise SystemOneError("System One 模型服务类型冲突。", code="adapter_conflict")
        provider_registry[:] = [item for item in provider_registry if item.type != PROVIDER_TYPE]
        del provider_cls_map[PROVIDER_TYPE]
    if PROVIDER_TYPE not in provider_cls_map:
        register_provider_adapter(
            PROVIDER_TYPE, "Jev / System One 决策模型", provider_type=ProviderType.CHAT_COMPLETION,
            default_config_tmpl=None, provider_display_name="Jev / System One",
        )(SystemOneProvider)
    templates = CONFIG_METADATA_2["provider_group"]["metadata"]["provider"]["config_template"]
    templates["Jev / System One"] = dict(DEFAULT_CONFIG)
    templates["OpenCode Zen · Jev"] = {
        **DEFAULT_CONFIG, "id": "opencode-jev", "api_base": "https://opencode.ai/zen/v1", "model": "jev-1.13",
    }


register_adapter()
