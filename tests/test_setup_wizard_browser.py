"""Browser end-to-end tests for the Chat Dynamics 4-step Setup Wizard and auto-entry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .test_ui_theme_browser import browser as browser, page_server as page_server

ROOT = Path(__file__).resolve().parents[1]


def _install_wizard_bridge(context, state: dict):
    """Install a controllable AstrBotPluginPage mock exposing recorded calls back to Python."""
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def handle_api(_source, method: str, endpoint: str, payload: dict):
        state.setdefault("calls", []).append((method, endpoint, payload))
        if method == "get":
            if endpoint == "ui_preferences":
                if state.get("prefs_fail"):
                    return {"ok": False, "error": "UI preference unavailable"}
                return {
                    "ok": True,
                    "data": {
                        "ui": state.get("ui", "day"),
                        "setup_dismissed": state.get("setup_dismissed", False),
                    },
                }
            if endpoint == "config":
                if state.get("config_fail"):
                    return {"ok": False, "error": "config unavailable"}
                return {
                    "ok": True,
                    "data": {
                        "schema": schema,
                        "stored": dict(state.get("stored", {})),
                        "effective": dict(state.get("effective", {})),
                        "mismatches": [],
                        "warnings": [],
                    },
                }
            if endpoint == "providers":
                return {
                    "ok": True,
                    "data": state.get(
                        "providers",
                        {
                            "chat": [
                                {"id": "openai/gpt-4o-mini", "model": "gpt-4o-mini"},
                                {"id": "anthropic/claude", "model": "claude-3.5"},
                            ],
                            "decision": [],
                            "systemone": [],
                            "embedding": [],
                            "rerank": [{"id": "bge-reranker", "model": "bge-reranker-v2"}],
                            "systemone_router_ready": True,
                        },
                    ),
                }
            if endpoint == "setup/groups":
                return {
                    "ok": True,
                    "data": state.get(
                        "setup_groups",
                        {
                            "groups": [
                                {
                                    "group_id": "10001",
                                    "group_name": "测试大群",
                                    "platform_id": "qq-main",
                                    "platform_type": "aiocqhttp",
                                    "umo": "qq-main:GroupMessage:10001",
                                    "member_count": 42,
                                    "sources": ["platform", "session"],
                                    "last_activity": 1732000000.0,
                                    "in_takeover": False,
                                    "in_exclude": False,
                                },
                                {
                                    "group_id": "10002",
                                    "group_name": "摸鱼茶话会",
                                    "platform_id": "qq-main",
                                    "platform_type": "aiocqhttp",
                                    "umo": "qq-main:GroupMessage:10002",
                                    "member_count": 18,
                                    "sources": ["platform"],
                                    "last_activity": 0,
                                    "in_takeover": False,
                                    "in_exclude": False,
                                },
                            ],
                            "platforms": [
                                {
                                    "platform_id": "qq-main",
                                    "platform_type": "aiocqhttp",
                                    "status": "ok",
                                    "group_count": 2,
                                    "error": "",
                                }
                            ],
                            "discovered_count": 2,
                            "session_count": 1,
                            "partial_failure": False,
                        },
                    ),
                }
            if endpoint == "series_presence":
                return {
                    "ok": True,
                    "data": {
                        "schema_version": 1,
                        "current": "astrbot_plugin_chat_dynamics",
                        "plugins": [
                            {
                                "id": "astrbot_plugin_chat_dynamics",
                                "label": "Chat Dynamics",
                                "page": "app",
                                "role": "对话调度与回复",
                                "state": "ready",
                                "can_open": False,
                                "current": True,
                            },
                            {
                                "id": "astrbot_plugin_dynamics_world",
                                "label": "群间世界",
                                "page": "world",
                                "role": "角色与生活状态",
                                "state": "ready",
                                "version": "v1.4.0",
                                "can_open": True,
                                "current": False,
                            },
                            {
                                "id": "astrbot_plugin_dynamics_memory",
                                "label": "群间记忆",
                                "page": "dashboard",
                                "role": "记忆与证据",
                                "state": "missing",
                                "can_open": False,
                                "current": False,
                            },
                        ],
                        "legacy_conflict": False,
                        "legacy_conflicts": [],
                        "series_defaults": {
                            "takeover_groups": state.get("effective", {}).get("takeover_groups", []),
                            "exclude_groups": state.get("effective", {}).get("exclude_groups", []),
                            "bot_names": state.get("effective", {}).get("bot_names", []),
                            "reply_provider": state.get("effective", {}).get("reply_provider", ""),
                            "decision_provider": state.get("effective", {}).get("decision_provider", ""),
                            "rhythm_timezone": "Asia/Shanghai",
                        },
                    },
                }
            if endpoint == "integrations":
                return {
                    "ok": True,
                    "data": {
                        "schema_version": 1,
                        "companions": [
                            {
                                "id": "astrbot_plugin_dynamics_world",
                                "name": "群间世界",
                                "role": "角色与生活状态",
                                "state": "ready",
                                "version": "v1.4.0",
                            }
                        ],
                        "blocking": [],
                    },
                }
            if endpoint == "page_nav":
                target = str((payload or {}).get("page") or "console")
                return {
                    "ok": True,
                    "data": {"content_path": f"/{target}/index.html?asset_token=fresh"},
                }
            return {"ok": True, "data": {}}

        if method == "post":
            if endpoint == "ui_preferences":
                body = payload or {}
                out = {"saved": True}
                if "ui" in body:
                    state["ui"] = body["ui"]
                    out["ui"] = body["ui"]
                if "setup_dismissed" in body:
                    state["setup_dismissed"] = body["setup_dismissed"]
                    out["setup_dismissed"] = body["setup_dismissed"]
                return {"ok": True, "data": out}
            if endpoint == "setup/probe":
                state.setdefault("probe_payloads", []).append(payload)
                if state.get("probe_slot_error"):
                    return {
                        "ok": True,
                        "data": {
                            "ok": False,
                            "results": {
                                "decision": {
                                    "status": "error",
                                    "code": "timeout",
                                    "backend": (payload or {}).get("participation_backend", "jev"),
                                    "provider_id": (payload or {}).get("decision_provider", "sys1/jev"),
                                    "model": "jev-1.13",
                                    "using_default": False,
                                    "latency_ms": 6001,
                                    "message": "模型响应超时（6秒）",
                                },
                                "reply": {
                                    "status": "ok",
                                    "code": "ok",
                                    "provider_id": "openai/gpt-4o-mini",
                                    "model": "gpt-4o-mini",
                                    "using_default": True,
                                    "latency_ms": 310,
                                    "message": "连接正常",
                                },
                                "discussion": None,
                            },
                        },
                    }
                return {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "results": {
                            "decision": {
                                "status": "ok",
                                "code": "ok",
                                "backend": (payload or {}).get("participation_backend", "llm"),
                                "provider_id": (payload or {}).get("decision_provider")
                                or (payload or {}).get("decision_llm_provider")
                                or "openai/gpt-4o-mini",
                                "model": "gpt-4o-mini",
                                "using_default": not bool((payload or {}).get("decision_provider")),
                                "latency_ms": 412,
                                "message": "连接正常",
                            },
                            "reply": {
                                "status": "ok",
                                "code": "ok",
                                "provider_id": (payload or {}).get("reply_provider")
                                or "openai/gpt-4o-mini",
                                "model": "gpt-4o-mini",
                                "using_default": not bool((payload or {}).get("reply_provider")),
                                "latency_ms": 625,
                                "message": "连接正常",
                            },
                            "discussion": None,
                        },
                    },
                }
            if endpoint == "config":
                body = payload or {}
                state.setdefault("saved_configs", []).append(body)
                updates = body.get("config", {})
                state.setdefault("stored", {}).update(updates)
                state.setdefault("effective", {}).update(updates)
                return {
                    "ok": True,
                    "data": {
                        "schema": schema,
                        "stored": dict(state["stored"]),
                        "effective": dict(state["effective"]),
                        "mismatches": [],
                        "warnings": [],
                    },
                }
            return {"ok": True, "data": {}}
        return {"ok": True, "data": {}}

    context.expose_binding("__wizardApi", handle_api)
    context.add_init_script(
        """
        window.AstrBotPluginPage = {
          ready: async () => {},
          t: (_key, fallback) => fallback,
          apiGet: (endpoint, params = {}) => window.__wizardApi('get', endpoint, params),
          apiPost: (endpoint, body = {}) => window.__wizardApi('post', endpoint, body),
        };
        """
    )


@pytest.mark.parametrize(
    ("effective", "prefs", "local_dismissed", "config_fail", "expected_segment"),
    [
        (
            {"takeover_all": False, "takeover_groups": [], "private_chat_enabled": False},
            {"setup_dismissed": False},
            False,
            False,
            "/setup/index.html",
        ),
        (
            {"takeover_all": False, "takeover_groups": ["10001"], "private_chat_enabled": False},
            {"setup_dismissed": False},
            False,
            False,
            "/console/index.html",
        ),
        (
            {"takeover_all": True, "takeover_groups": [], "private_chat_enabled": False},
            {"setup_dismissed": False},
            False,
            False,
            "/console/index.html",
        ),
        (
            {"takeover_all": False, "takeover_groups": [], "private_chat_enabled": True},
            {"setup_dismissed": False},
            False,
            False,
            "/console/index.html",
        ),
        (
            {"takeover_all": False, "takeover_groups": [], "private_chat_enabled": False},
            {"setup_dismissed": True},
            False,
            False,
            "/console/index.html",
        ),
        (
            {"takeover_all": False, "takeover_groups": [], "private_chat_enabled": False},
            {"prefs_fail": True},
            True,
            False,
            "/console/index.html",
        ),
        (
            {"takeover_all": False, "takeover_groups": [], "private_chat_enabled": False},
            {"setup_dismissed": False},
            False,
            True,
            "/console/index.html",
        ),
    ],
)
def test_entry_js_auto_redirects_and_preserves_params(
    browser,
    page_server,
    effective,
    prefs,
    local_dismissed,
    config_fail,
    expected_segment,
):
    state = {
        "effective": effective,
        "setup_dismissed": prefs.get("setup_dismissed", False),
        "prefs_fail": prefs.get("prefs_fail", False),
        "config_fail": config_fail,
    }
    with browser.new_context() as context:
        _install_wizard_bridge(context, state)
        init_js = "sessionStorage.setItem('dynamics_shared_umo', 'qq-main:GroupMessage:10001');"
        if local_dismissed:
            init_js += "localStorage.setItem('chat_dynamics_setup_dismissed', '1');"
        context.add_init_script(init_js)
        page = context.new_page()
        page.goto(f"{page_server}/index.html?asset_token=signed123&ui=night#panel")
        page.wait_for_url(f"**{expected_segment}**")
        assert "asset_token=signed123" in page.url
        assert "ui=night" in page.url
        assert "umo=qq-main%3AGroupMessage%3A10001" in page.url
        assert page.url.endswith("#panel")


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    [
        (1440, 960, "day"),
        (375, 812, "night"),
    ],
)
def test_setup_wizard_full_flow_responsive_and_partial_save(
    browser, page_server, width, height, theme
):
    state = {
        "ui": theme,
        "stored": {
            "enable": True,
            "takeover_all": False,
            "takeover_groups": [],
            "private_chat_enabled": False,
            "participation_backend": "jev",
            "decision_provider": "",
            "rhythm_timezone": "",
            "reply_prompt": "keep-this-untouched-prompt",
            "jev_timeout": 9.5,
        },
        "effective": {
            "enable": True,
            "takeover_all": False,
            "takeover_groups": [],
            "exclude_groups": [],
            "bot_names": [],
            "participation_backend": "jev",
            "decision_provider": "",
            "decision_llm_provider": "",
            "reply_provider": "",
            "discussion_judgment_mode": "rerank",
            "discussion_reranker_provider": "",
            "discussion_llm_provider": "",
            "shadow_mode": False,
            "presence_knob": "sensible",
            "reply_probability_threshold": 70,
            "debounce_base_cooldown": 0.25,
            "debounce_extended_cooldown": 1.0,
            "debounce_max_cap": 4.0,
            "deep_cooling_minutes": 15.0,
            "private_chat_enabled": False,
            "daily_rhythm_enabled": True,
            "media_understand_reply_enabled": False,
            "rhythm_timezone": "",
            "reply_prompt": "keep-this-untouched-prompt",
            "jev_timeout": 9.5,
        },
    }
    with browser.new_context(viewport={"width": width, "height": height}) as context:
        _install_wizard_bridge(context, state)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))

        page.goto(f"{page_server}/setup/index.html?ui={theme}")
        page.locator('[data-group-check="10001"]').wait_for()

        # Never auto-probe on load
        assert not state.get("probe_payloads")

        # Step 1 responsive + group search + checkbox + batch paste + bot_names
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.locator("#groupSearch").fill("测试大群")
        assert page.locator(".group-row").count() == 1
        page.locator('[data-group-check="10001"]').check()
        page.locator("#groupSearch").fill("")
        assert page.locator(".group-row").count() == 2
        page.locator("#takeoverGroupsInput").fill("10001, 20002")
        page.locator("#botNamesInput").fill("小安, 安安")
        assert page.locator(".name-chip").count() == 2

        # Step 2: 0 decision providers -> auto-recommended llm (even with schema default stored="jev"), manual probe works
        page.locator("#btnNextStep").click()
        page.locator("#stepPanel2").wait_for(state="visible")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert page.locator("#backendLlm").is_checked()
        assert "participation_backend" in page.locator("#stepPanel2 legend").inner_text()
        assert not state.get("probe_payloads")

        page.locator("#btnProbeModels").click()
        page.locator(".probe-card").first.wait_for()
        assert len(state.get("probe_payloads", [])) == 1
        probe_text = page.locator("#probeResults").inner_text()
        assert "412 ms" in probe_text and "625 ms" in probe_text

        # Step 3: Scenario card (observe) + 3 toggles
        page.locator("#btnNextStep").click()
        page.locator("#stepPanel3").wait_for(state="visible")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.locator('[data-style-id="observe"]').click()
        assert page.locator('[data-style-id="observe"]').get_attribute("aria-checked") == "true"
        page.locator("#togglePrivateChat").check()
        page.locator("#toggleMediaUnderstand").check()

        # Free-jump test: jump back to Step 1 then directly to Step 4
        page.locator("#stepBtn1").click()
        page.locator("#stepPanel1").wait_for(state="visible")
        page.locator("#stepBtn4").click()
        page.locator("#stepPanel4").wait_for(state="visible")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        # Verify shared defaults and companion cards rendered
        shared_text = page.locator("#sharedDefaultsBox").inner_text()
        assert "takeover_groups" in shared_text
        assert "rhythm_timezone" in shared_text
        assert "Asia/Shanghai" in shared_text
        assert page.locator('[data-companion-id="astrbot_plugin_dynamics_world"]').is_visible()

        # Verify companion jump calls window.DynamicsSeriesNavigation.open(plugin.id)
        page.evaluate(
            """() => {
              window.__openedCompanion = null;
              window.DynamicsSeriesNavigation = {
                open: async (id) => { window.__openedCompanion = id; return true; }
              };
            }"""
        )
        page.locator('[data-open-companion="astrbot_plugin_dynamics_world"]').click()
        page.wait_for_function("window.__openedCompanion === 'astrbot_plugin_dynamics_world'")

        # Save partial diff and verify ONLY changed keys + baseline sent
        page.locator("#btnSaveSetup").click()
        page.locator("#saveSuccessBanner").wait_for(state="visible")
        assert len(state.get("saved_configs", [])) == 1
        saved_req = state["saved_configs"][0]
        cfg = saved_req["config"]
        baseline = saved_req["baseline"]

        assert cfg["takeover_groups"] == ["10001", "20002"]
        assert cfg["bot_names"] == ["小安", "安安"]
        assert cfg["participation_backend"] == "llm"
        assert cfg["shadow_mode"] is True
        assert cfg["private_chat_enabled"] is True
        assert cfg["media_understand_reply_enabled"] is True
        # Untouched advanced keys must NEVER be overwritten or included
        assert "reply_prompt" not in cfg
        assert "jev_timeout" not in cfg
        assert "daily_rhythm_enabled" not in cfg
        assert set(baseline.keys()) == set(cfg.keys())
        assert state["setup_dismissed"] is True
        assert not errors


def test_reentry_keeps_custom_tuning_and_jev_recommendation_and_dismiss(
    browser, page_server
):
    state = {
        "stored": {
            "takeover_groups": ["10001"],
            "participation_backend": "jev",
            "decision_provider": "sys1/jev",
            "shadow_mode": False,
            "presence_knob": "sensible",
            "reply_probability_threshold": 63,  # custom non-preset value
            "debounce_base_cooldown": 0.3,
            "debounce_extended_cooldown": 1.1,
            "debounce_max_cap": 4.5,
            "deep_cooling_minutes": 18.0,
        },
        "effective": {
            "enable": True,
            "takeover_all": False,
            "takeover_groups": ["10001"],
            "exclude_groups": [],
            "bot_names": ["小安"],
            "participation_backend": "jev",
            "decision_provider": "sys1/jev",
            "decision_llm_provider": "",
            "reply_provider": "",
            "discussion_judgment_mode": "rerank",
            "discussion_reranker_provider": "",
            "discussion_llm_provider": "",
            "shadow_mode": False,
            "presence_knob": "sensible",
            "reply_probability_threshold": 63,
            "debounce_base_cooldown": 0.3,
            "debounce_extended_cooldown": 1.1,
            "debounce_max_cap": 4.5,
            "deep_cooling_minutes": 18.0,
            "private_chat_enabled": False,
            "daily_rhythm_enabled": True,
            "media_understand_reply_enabled": False,
        },
        "providers": {
            "chat": [{"id": "openai/gpt-4o-mini", "model": "gpt-4o-mini"}],
            "decision": [{"id": "sys1/jev", "model": "jev-1.13"}],
            "systemone": [{"id": "sys1/jev", "model": "jev-1.13"}],
            "embedding": [],
            "rerank": [],
            "systemone_router_ready": True,
        },
    }
    with browser.new_context(viewport={"width": 1440, "height": 960}) as context:
        _install_wizard_bridge(context, state)
        page = context.new_page()
        page.goto(f"{page_server}/setup/index.html")
        page.locator("#setupReentryBanner").wait_for(state="visible")

        # Step 3 defaults to keep_custom so existing tuning is never clobbered
        page.locator("#stepBtn3").click()
        keep_card = page.locator('[data-style-id="keep_custom"]')
        keep_card.wait_for(state="visible")
        assert keep_card.get_attribute("aria-checked") == "true"

        # Change only private_chat_enabled and save -> none of the 7 style keys sent!
        page.locator("#togglePrivateChat").check()
        page.locator("#stepBtn4").click()
        page.locator("#btnSaveSetup").click()
        page.locator("#saveSuccessBanner").wait_for(state="visible")

        saved_cfg = state["saved_configs"][0]["config"]
        assert saved_cfg == {"private_chat_enabled": True}

        # Blocking warning when Jev has empty decision_provider
        page.locator("#stepBtn2").click()
        page.locator("#decisionProviderSelect").select_option("")
        page.locator("#stepBtn4").click()
        page.locator("#reviewWarnings.has-blocking").wait_for(state="visible")
        page.locator("#btnSaveSetup").click()
        assert len(state["saved_configs"]) == 1  # blocked, not submitted

        # Dismiss button saves setup_dismissed=true and navigates to console
        page.locator("#btnDismissSetup").click()
        page.wait_for_url("**/console/index.html?**")
        assert state["setup_dismissed"] is True
        assert page.evaluate("localStorage.getItem('chat_dynamics_setup_dismissed')") == "1"


def test_config_page_open_setup_wizard_button(browser, page_server):
    state = {"effective": {"enable": True, "takeover_all": True}}
    with browser.new_context() as context:
        _install_wizard_bridge(context, state)
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        btn = page.locator("#btnOpenSetupWizard")
        btn.wait_for(state="visible")
        btn.click()
        page.wait_for_url("**/setup/index.html?**")


def test_fresh_install_with_decision_provider_and_style_presets(browser, page_server):
    state = {
        "stored": {
            "enable": True,
            "takeover_all": False,
            "takeover_groups": [],
            "private_chat_enabled": False,
            "participation_backend": "jev",
            "decision_provider": "",
        },
        "effective": {
            "enable": True,
            "takeover_all": False,
            "takeover_groups": [],
            "exclude_groups": [],
            "bot_names": [],
            "participation_backend": "jev",
            "decision_provider": "",
            "decision_llm_provider": "",
            "reply_provider": "",
            "discussion_judgment_mode": "rerank",
            "discussion_reranker_provider": "",
            "discussion_llm_provider": "",
            "shadow_mode": False,
            "presence_knob": "sensible",
            "reply_probability_threshold": 70,
            "debounce_base_cooldown": 0.25,
            "debounce_extended_cooldown": 1.0,
            "debounce_max_cap": 4.0,
            "deep_cooling_minutes": 15.0,
            "private_chat_enabled": False,
            "daily_rhythm_enabled": True,
            "media_understand_reply_enabled": False,
        },
        "providers": {
            "chat": [{"id": "openai/gpt-4o-mini", "model": "gpt-4o-mini"}],
            "decision": [{"id": "sys1/jev-primary", "model": "jev-1.13"}],
            "systemone": [{"id": "sys1/jev-primary", "model": "jev-1.13"}],
            "embedding": [],
            "rerank": [],
            "systemone_router_ready": True,
        },
    }
    with browser.new_context(viewport={"width": 1440, "height": 960}) as context:
        _install_wizard_bridge(context, state)
        page = context.new_page()
        page.goto(f"{page_server}/setup/index.html")
        page.locator('[data-group-check="10001"]').wait_for()
        page.locator('[data-group-check="10001"]').check()

        # Step 2 auto-recommends Jev and preselects first decision provider
        page.locator("#stepBtn2").click()
        assert page.locator("#backendJev").is_checked()
        assert page.locator("#decisionProviderSelect").input_value() == "sys1/jev-primary"

        # Step 3: pick lively preset and verify exact 7 keys saved
        page.locator("#stepBtn3").click()
        page.locator('[data-style-id="lively"]').click()
        page.locator("#stepBtn4").click()
        page.locator("#btnSaveSetup").click()
        page.locator("#saveSuccessBanner").wait_for(state="visible")

        cfg = state["saved_configs"][0]["config"]
        assert cfg["decision_provider"] == "sys1/jev-primary"
        assert cfg["presence_knob"] == "lively"
        assert cfg["reply_probability_threshold"] == 55
        assert cfg["debounce_base_cooldown"] == 0.15
        assert cfg["debounce_extended_cooldown"] == 0.75
        assert cfg["debounce_max_cap"] == 3.0
        assert cfg["deep_cooling_minutes"] == 10.0

        # Probe with per-slot error still renders friendly error card alongside healthy reply card
        state["probe_slot_error"] = True
        page.locator("#stepBtn2").click()
        page.locator("#btnProbeModels").click()
        page.locator(".probe-card.is-error").wait_for(state="visible")
        assert "模型响应超时" in page.locator(".probe-card.is-error").inner_text()
        assert page.locator(".probe-card.is-ok").count() == 1

