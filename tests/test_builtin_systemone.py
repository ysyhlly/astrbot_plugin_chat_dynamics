import asyncio
import copy
import importlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from astrbot_plugin_chat_dynamics.core.systemone.client import (
    SystemOneError,
    endpoint_url,
    parse_models,
)
from astrbot_plugin_chat_dynamics.core.systemone.service import SystemOneService
from astrbot_plugin_chat_dynamics.core.native_decision import NativeDecisionUnavailable
from astrbot_plugin_chat_dynamics.core.systemone.provider import (
    DEFAULT_CONFIG,
    PROVIDER_TYPE,
    SystemOneProvider,
)
from astrbot_plugin_chat_dynamics.core.systemone.routing import RoutedSystemOneClient

from astrbot.core.provider.register import provider_cls_map, provider_registry
from astrbot.dashboard.services.config_service import ProviderConfigService

QUESTIONS = {
    "join": {"type": "noul", "instructions": "Join this synthetic conversation?"},
    "route": {"type": "choice", "instructions": "Choose a route", "criteria": {"reply": "Reply", "silent": "Remain silent"}},
    "priority": {"type": "score", "instructions": "Rate priority", "criteria": ["Low", "High"]},
}
ANSWERS = {
    "join": {"type": "noul", "noul": 0.9},
    "route": {"type": "choice", "choice": "reply", "probabilities": {"reply": 0.8, "silent": 0.2}, "confidence": 0.8},
    "priority": {"type": "score", "score": 0.7, "probabilities": {"0": 0.3, "1": 0.7}, "confidence": 0.7},
}


class ParseTests(unittest.TestCase):
    def test_formats_and_full_model_ids(self):
        for payload in (["model", "model"], {"models": [{"name": "model"}]},
                        {"data": [{"id": "model"}]}, {"data": {"models": [{"model_id": "model"}]}}):
            self.assertEqual(parse_models(payload), ["model"])
        self.assertEqual(parse_models(["x" * 100]), ["x" * 100])
        for payload in ({"answers": {}}, {"models": [{}]}, [True], ["bad\nmodel"]):
            with self.assertRaises(SystemOneError):
                parse_models(payload)

    def test_urls(self):
        for base in ("https://service.example/api", "https://service.example/api/v1", "https://service.example/api/v1/systemone"):
            self.assertEqual(endpoint_url(base, "/v1/systemone"), "https://service.example/api/v1/systemone")
        self.assertEqual(endpoint_url("https://service.example/api/v1", "/v1/models"), "https://service.example/api/v1/models")
        for base in ("https://user:secret@service.example", "https://service.example?q=secret", "file:///path"):
            with self.assertRaises(SystemOneError):
                endpoint_url(base, "/v1/systemone")

    def test_adapter_registration_reload_and_native_template(self):
        self.assertTrue(provider_cls_map[PROVIDER_TYPE].cls_type.is_systemone_provider)
        module = importlib.import_module("astrbot_plugin_chat_dynamics.core.systemone.provider")
        module.register_adapter()
        module.register_adapter()
        self.assertEqual(sum(metadata.type == PROVIDER_TYPE for metadata in provider_registry), 1)
        lifecycle = SimpleNamespace(astrbot_config={"provider": [], "provider_sources": []}, provider_manager=None)
        schema = ProviderConfigService(lifecycle).get_provider_schema()
        template = schema["config_schema"]["provider"]["config_template"]["Jev / System One"]
        self.assertEqual(template["type"], PROVIDER_TYPE)
        self.assertIn("api_base", template)
        self.assertIn("key", template)
        self.assertNotIn("services", template)


class NativeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.mode = "ok"
        self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            body = await request.json() if request.method == "POST" else None
            self.requests.append((request.method, request.path, request.headers.get("Authorization"), body))
            if self.mode == "error":
                return web.Response(status=401, text="secret upstream body")
            if self.mode == "redirect":
                raise web.HTTPFound("/leak")
            if self.mode == "malformed":
                return web.Response(text="not json")
            if self.mode == "oversize":
                return web.Response(body=b"x" * (1024 * 1024 + 1))
            if request.method == "GET":
                return web.json_response({"models": [{"name": "jev-latest"}, {"name": "custom-model"}]})
            if self.mode == "delay":
                self.entered.set()
                await self.release.wait()
            answers = copy.deepcopy(ANSWERS)
            if self.mode == "full_turn":
                answers = {}
                for name, spec in body["questions"].items():
                    if spec["type"] == "noul":
                        answers[name] = {"type": "noul", "noul": 0.9}
                    elif spec["type"] == "choice":
                        choices = list(spec["criteria"])
                        answers[name] = {"type": "choice", "choice": choices[0],
                                         "probabilities": {key: float(key == choices[0]) for key in choices},
                                         "confidence": 1.0}
                    else:
                        answers[name] = {"type": "score", "score": 0.7, "confidence": 0.7,
                                         "probabilities": {"0": 0.3, "1": 0.7}}
            if self.mode == "bad_answers":
                answers["join"]["noul"] = True
            if self.mode == "missing_answer":
                del answers["route"]
            return web.json_response({"model": body["model"], "answers": answers})

        app = web.Application()
        app.router.add_route("*", "/api/v1/systemone", handler)
        app.router.add_route("GET", "/api/v1/models", handler)
        app.router.add_route("*", "/leak", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.source = {**DEFAULT_CONFIG, "id": "native-source", "api_base": f"http://127.0.0.1:{port}/api/v1", "key": ["dummy-key"]}
        self.provider = SystemOneProvider({**self.source, "id": "selected-model", "model": "custom-model"}, {})
        self.manager = SimpleNamespace(inst_map={"selected-model": self.provider},
                                       providers_config=[], dynamic_import_provider=lambda name: None)
        lifecycle = SimpleNamespace(astrbot_config={"provider": [], "provider_sources": [self.source]}, provider_manager=self.manager)
        self.service = ProviderConfigService(lifecycle)
        config_module = importlib.import_module("astrbot_plugin_chat_dynamics.core.config")
        self.panel_cls = importlib.import_module("astrbot_plugin_chat_dynamics.core.config_panel").ConfigPanel
        config, _ = config_module.parse_runtime_config({"decision_backend": "jev", "decision_mode": "persona_model",
                                                       "decision_provider": "selected-model", "decision_learning_mode": "off"})
        self.context = SimpleNamespace(provider_manager=self.manager,
            get_provider_by_id=lambda provider_id: self.manager.inst_map.get(provider_id),
            get_all_providers=lambda: list(self.manager.inst_map.values()),
            get_all_embedding_providers=lambda: [])
        self.fallback = NativeDecisionUnavailable()
        self.host = SimpleNamespace(context=self.context, _runtime_config=config, jev=self.fallback)
        self.host.jev = RoutedSystemOneClient(self.host, self.fallback)
        self.plugin = SystemOneService(self.host)

    async def asyncTearDown(self):
        self.release.set()
        await self.runner.cleanup()

    async def test_native_model_list_service_endpoint_auth_and_dropdown(self):
        result = await self.service.list_provider_source_models("native-source")
        self.assertEqual(result["models"], ["jev-latest", "custom-model"])
        self.assertEqual(self.requests[-1][:3], ("GET", "/api/v1/models", "Bearer dummy-key"))
        available = self.panel_cls(self.host).list_available_providers()
        self.assertEqual(available["chat"][0]["id"], "selected-model")
        self.assertEqual(available["chat"][0]["model"], "custom-model")

    async def test_source_environment_key_and_alternative_models_path(self):
        self.source["key"] = ["${JEV_TEST_KEY}"]
        self.source["models_path"] = "/v1/models"
        with patch.dict(os.environ, {"JEV_TEST_KEY": "environment-dummy-key"}):
            await self.service.list_provider_source_models("native-source")
        self.assertEqual(self.requests[-1][:3], ("GET", "/api/v1/models", "Bearer environment-dummy-key"))

    async def test_zen_catalog_uses_models_route_and_filters_chat_models(self):
        provider = SystemOneProvider({**DEFAULT_CONFIG, "api_base": "https://opencode.ai/zen/v1",
                                      "models_path": "/v1/systemone", "key": ["dummy-key"]}, {})
        request = AsyncMock(return_value={"data": [{"id": "gpt-model"}, {"id": "jev-1.13"}, {"id": "jev-1.13-free"}]})
        with patch("astrbot_plugin_chat_dynamics.core.systemone.provider.request_json", request):
            self.assertEqual(await provider.get_models(), ["jev-1.13", "jev-1.13-free"])
        self.assertEqual(request.call_args.args, ("GET", "https://opencode.ai/zen/v1/models"))
        self.assertEqual(provider.provider_config["models_path"], "/v1/systemone")

    async def test_custom_catalog_path_remains_configurable(self):
        provider = SystemOneProvider({**self.source, "models_path": "/v1/systemone"}, {})
        self.assertEqual(await provider.get_models(), ["jev-latest", "custom-model"])
        self.assertEqual(self.requests[-1][1], "/api/v1/systemone")

    async def test_selected_provider_decision_uses_native_key_address_and_model(self):
        await self.plugin.refresh_providers()
        self.assertIsInstance(self.host.jev, RoutedSystemOneClient)
        answers = await self.host.jev.evaluate(state={"message": "synthetic test only"}, questions=QUESTIONS, timeout=2)
        self.assertEqual(answers, ANSWERS)
        method, path, key, payload = self.requests[-1]
        self.assertEqual((method, path, key), ("POST", "/api/v1/systemone", "Bearer dummy-key"))
        self.assertEqual(payload["model"], "custom-model")
        self.assertTrue(self.host.jev.snapshot()["available"])
        self.provider.set_model("jev-latest")
        self.assertFalse(self.host.jev.snapshot()["available"])
        await self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS)
        self.assertEqual(self.requests[-1][3]["model"], "jev-latest")

    async def test_native_connection_test_uses_three_typed_questions_and_selected_model(self):
        self.provider.set_model("typesafe/jev")
        answers = {
            "is_urgent": {"type": "noul", "noul": 0.96},
            "department": {"type": "choice", "choice": "billing", "confidence": 0.89,
                           "probabilities": {"billing": 0.89, "shipping": 0.05, "returns": 0.06}},
            "frustration": {"type": "score", "score": 1.41, "confidence": 0.5,
                            "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
                            "probabilities": {"0": 0.09, "1": 0.41, "2": 0.5}},
        }
        request = AsyncMock(return_value={"model": "typesafe/jev", "answers": answers,
                                         "usage": {"input_tokens": 84, "output_tokens": 3}})
        with patch("astrbot_plugin_chat_dynamics.core.systemone.provider.request_json", request):
            self.assertIsNone(await self.provider.test(timeout=12))
            self.assertEqual(request.call_args.args, ("POST", self.source["api_base"] + "/systemone"))
            payload = request.call_args.kwargs["payload"]
            self.assertEqual(payload["model"], "typesafe/jev")
            self.assertEqual(payload["state"], "My payments have failed for three days and I am losing sales. Please help now.")
            self.assertEqual({key: value["type"] for key, value in payload["questions"].items()},
                             {"is_urgent": "noul", "department": "choice", "frustration": "score"})
            self.assertEqual(payload["questions"]["frustration"]["criteria"], ["Calm", "Frustrated", "Very angry"])
            self.assertEqual(request.call_args.kwargs["key"], "dummy-key")
            self.assertEqual(request.call_args.kwargs["timeout"], 12)
            del answers["department"]
            with self.assertRaises(SystemOneError):
                await self.provider.test()

    async def test_http_failures_are_safe_and_redirects_are_not_followed(self):
        for mode in ("error", "redirect", "malformed", "oversize"):
            self.mode = mode
            before = len(self.requests)
            with self.subTest(mode=mode), self.assertRaises(SystemOneError) as caught:
                await self.provider.get_models()
            self.assertEqual(len(self.requests), before + 1)
            self.assertNotIn("secret upstream body", str(caught.exception))

    async def test_real_chat_dynamics_questions_are_normalized_without_mutation(self):
        from astrbot_plugin_chat_dynamics.core.jev_decision import (
            build_questions,
            build_state,
        )
        from astrbot_plugin_chat_dynamics.core.turn_decision import (
            MessageSnapshot,
            TurnContext,
        )
        turn = TurnContext(session_key="synthetic-test", author="test-user", text="请帮我检查连接问题",
                           messages=(MessageSnapshot("m1", "test-user", "测试消息"),), background=(),
                           epoch=0, revision=0, started_at=0, explicit=True)
        questions = build_questions(turn)
        before = copy.deepcopy(questions)
        state = build_state(turn, previous_state="observing", observations={}, presence="sensible",
                            persona_prompt="简洁地帮助处理问题")
        self.mode = "full_turn"
        await self.plugin.refresh_providers()
        answers = await self.host.jev.evaluate(state=state, questions=questions, timeout=2)
        self.assertEqual(set(answers), set(questions))
        self.assertEqual(questions, before)
        payload = self.requests[-1][3]
        self.assertEqual(payload["model"], "custom-model")
        self.assertEqual(payload["state"], json.loads(json.dumps(state)))
        for name, spec in questions.items():
            self.assertEqual(json.loads(payload["questions"][name]["instructions"]), spec["instructions"])
            self.assertEqual(payload["questions"][name].get("criteria"), spec.get("criteria"))
        self.assertTrue(self.host.jev.snapshot()["available"])

    async def test_request_budget_covers_state_questions_and_selected_model_before_http(self):
        await self.plugin.refresh_providers()
        for oversized_part in ("state", "questions", "model"):
            with self.subTest(part=oversized_part):
                self.provider.set_model("custom-model")
                state = {"message": "synthetic state"}
                questions = copy.deepcopy(QUESTIONS)
                if oversized_part == "state":
                    state["message"] = "x" * 18_000
                elif oversized_part == "questions":
                    questions["join"]["instructions"] = {"question": "x" * 18_000}
                else:
                    self.provider.set_model("x" * 18_000)
                self.assertIsNone(await self.host.jev.evaluate(state=state, questions=questions))
                self.assertEqual(self.host.jev.snapshot()["detail"], "request_budget_exceeded")
                self.assertEqual(self.requests, [])

    async def test_router_distinguishes_timeout_from_http_failure(self):
        await self.plugin.refresh_providers()
        self.mode = "delay"
        self.assertIsNone(await self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS, timeout=0.05))
        status = self.host.jev.snapshot()
        self.assertEqual(status["detail"], "timeout")
        self.assertGreater(status["last_latency_ms"], 0)
        self.release.set()
        self.mode = "error"
        self.assertIsNone(await self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS, timeout=2))
        status = self.host.jev.snapshot()
        self.assertEqual(status["detail"], "http_401")
        self.assertEqual(status["failures"], 2)
        self.assertNotIn("secret upstream body", str(status))

    async def test_invalid_instructions_fail_before_sending_request(self):
        await self.plugin.refresh_providers()
        for instructions in (3, {}, {"question": 3}, ""):
            questions = copy.deepcopy(QUESTIONS)
            questions["join"]["instructions"] = instructions
            self.assertIsNone(await self.host.jev.evaluate(state="synthetic state", questions=questions))
            self.assertEqual(self.host.jev.snapshot()["detail"], "invalid_questions")
        self.assertEqual(self.requests, [])

    async def test_invalid_answers_rejected_and_router_reports_failure(self):
        await self.plugin.refresh_providers()
        for mode in ("bad_answers", "missing_answer"):
            self.mode = mode
            self.assertIsNone(await self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS))
            self.assertEqual(self.host.jev.snapshot()["status"], "degraded")

    async def test_model_switch_discards_in_flight_answers(self):
        await self.plugin.refresh_providers()
        self.mode = "delay"
        task = asyncio.create_task(self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS))
        await asyncio.wait_for(self.entered.wait(), timeout=2)
        self.provider.set_model("changed-model")
        self.release.set()
        self.assertIsNone(await task)

    async def test_unload_discards_in_flight_without_restoring_an_external_client(self):
        await self.plugin.refresh_providers()
        router = self.host.jev
        self.mode = "delay"
        task = asyncio.create_task(router.evaluate(state="synthetic state", questions=QUESTIONS))
        await asyncio.wait_for(self.entered.wait(), timeout=2)
        await self.host.jev.close()
        self.assertIs(self.host.jev, router)
        self.release.set()
        self.assertIsNone(await task)

    async def test_invalid_selected_provider_never_falls_back_to_another_service(self):
        await self.plugin.refresh_providers()
        self.manager.inst_map.clear()
        self.assertIsNone(await self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS))
        self.assertEqual(self.requests, [])
        self.assertFalse(self.host.jev.snapshot()["configured"])

    async def test_typed_llm_interface_and_plain_reply_rejection(self):
        result = await self.provider.text_chat(prompt=json.dumps({"state": "synthetic state", "questions": QUESTIONS}))
        self.assertEqual(json.loads(result.completion_text)["answers"], ANSWERS)
        before = len(self.requests)
        with self.assertRaises(SystemOneError):
            await self.provider.text_chat(prompt="write a reply")
        self.assertEqual(len(self.requests), before)

    async def test_builtin_router_exists_without_a_companion_and_refresh_is_idempotent(self):
        router = self.host.jev
        await self.plugin.refresh_providers()
        await self.plugin.refresh_providers()
        self.assertIs(self.host.jev, router)
        self.assertTrue(router.is_systemone_router)
        await router.close()
        self.assertIsNone(await router.evaluate(state="synthetic", questions=QUESTIONS))

    async def test_plugin_update_refreshes_old_native_instances_and_merges_sources(self):
        old = SimpleNamespace(is_systemone_provider=True)
        self.manager.inst_map["selected-model"] = old
        self.manager.providers_config = [{"id": "selected-model", "provider_source_id": "native-source", "enable": True}]
        self.manager.get_merged_provider_config = lambda config: {**self.source, **config}
        self.manager.reload = AsyncMock()
        self.manager.load_provider = AsyncMock()
        await self.plugin.refresh_providers()
        self.manager.reload.assert_awaited_once_with(self.manager.providers_config[0])
        self.manager.load_provider.assert_not_awaited()
        self.manager.inst_map["selected-model"] = self.provider
        await self.plugin.refresh_providers()
        self.assertEqual(self.manager.reload.await_count, 1)
        self.manager.inst_map.clear()
        await self.plugin.refresh_providers()
        self.manager.load_provider.assert_awaited_once_with(self.manager.providers_config[0])

    async def test_no_automatic_configuration_changes(self):
        before = copy.deepcopy(self.host._runtime_config)
        source_before = copy.deepcopy(self.source)
        await self.plugin.refresh_providers()
        await self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS)
        await self.host.jev.close()
        self.assertEqual(self.host._runtime_config, before)
        self.assertEqual(self.source, source_before)

    async def test_blank_provider_is_quiet_without_any_legacy_endpoint(self):
        from dataclasses import replace
        self.host._runtime_config = replace(self.host._runtime_config, decision_provider_id="")
        await self.plugin.refresh_providers()
        self.assertIsNone(await self.host.jev.evaluate(state="synthetic state", questions=QUESTIONS))
        self.assertEqual(self.requests, [])

    async def test_one_invalid_provider_does_not_block_other_models(self):
        broken = {"id": "broken"}
        good = {"id": "new", "type": PROVIDER_TYPE, "enable": True}
        self.manager.providers_config = [broken, good]
        def merged(config):
            if config is broken:
                raise ValueError("invalid config")
            return config
        self.manager.get_merged_provider_config = merged
        self.manager.load_provider = AsyncMock()
        await self.plugin.refresh_providers()
        self.manager.load_provider.assert_awaited_once_with(good)

    async def test_real_main_owns_native_router_and_soft_wake_without_companion(self):
        from .test_plugin_lifecycle import MockEvent, _plugin
        from .test_persona_model import BridgeDouble, drain, flush
        with patch("astrbot_plugin_chat_dynamics.core.agent_bridge.AstrBotAgentBridge.check", return_value=True):
            p = _plugin({"decision_provider": "selected-model", "debounce_base_cooldown": 10,
                         "base_thinking_delay": 0, "daily_rhythm_enabled": False})
        p.context.provider_manager = self.manager
        p.context.get_provider_by_id = self.context.get_provider_by_id
        p.persona_engine.bridge = BridgeDouble()
        self.mode = "full_turn"
        event = MockEvent("小助手帮我检查连接")
        try:
            self.assertIsInstance(p.jev, RoutedSystemOneClient)
            await p.on_astrbot_loaded()
            await p.on_group_message(event)
            await flush(p, event)
            await drain(p)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(self.requests[0][1], "/api/v1/systemone")
            self.assertIn("recipient", self.requests[0][3]["questions"])
            self.assertTrue(event.replies_sent)
            self.assertTrue(p.jev.snapshot()["available"])
        finally:
            await p.terminate()


if __name__ == "__main__":
    unittest.main()
