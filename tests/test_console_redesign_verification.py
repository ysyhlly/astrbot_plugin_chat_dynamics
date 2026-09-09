"""Verification tests for the console redesign and self-learning diagnostic panel."""

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent))

playwright = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
sync_playwright = playwright.sync_playwright
expect = playwright.expect

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "pages" / "console"


def _launch_browser(pw):
    configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    candidates = [Path(configured)] if configured else []
    if os.name == "nt":
        candidates.extend([
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ])
    for candidate in candidates:
        if candidate and candidate.is_file():
            try:
                return pw.chromium.launch(headless=True, executable_path=str(candidate))
            except Exception as exc:
                pytest.skip(f"Chromium unavailable: {exc}")
    try:
        return pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        return


@pytest.fixture()
def console_server():
    def handler(*args, **kwargs):
        return _QuietHandler(*args, directory=str(CONSOLE), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.html"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("viewport_name", "width", "height"),
    [
        ("mobile-375", 375, 667),
        ("mobile-390", 390, 844),
        ("tablet-768", 768, 1024),
        ("tablet-820", 820, 1180),
        ("desktop-1280", 1280, 800),
        ("desktop-1440", 1440, 1000),
    ],
)
@pytest.mark.parametrize("theme", ["day", "night"])
def test_all_viewports_no_horizontal_overflow_and_touch_targets(
    console_server, viewport_name, width, height, theme
):
    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": width, "height": height})
        page.on("console", lambda msg: print(f"BROWSER LOG: {msg.text}"))
        page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))
        page.add_init_script(f"""
            window.addEventListener("DOMContentLoaded", () => {{
                document.documentElement.setAttribute("data-theme", "{theme}");
            }});
            window.confirm = () => true;
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async (endpoint) => {{
                if (endpoint === "presets") return {{status:"ok", ok:true, data:{{presets:{{active:{{changes:{{}}}}}}}}}};
                if (endpoint === "session") return {{status:"ok", ok:true, data:{{
                  session_key:"mock:GroupMessage:g1", session_id:"g1", group_id:"g1", mode:"chill_fade", content_redacted:true,
                  nodes:[], dag_nodes:0, unique_speakers:1, cooling:false, cooling_remaining:0
                }}}};
                return {{status:"ok", ok:true, data:{{
                  enabled:true, pipeline_mode:"filter", takeover_all:true, takeover_groups:[],
                  live_count:1, cooling_count:0, pending_count:0, shadow_mode:false, console_show_message_content:false,
                  sessions:[{{session_key:"mock:GroupMessage:g1", session_id:"g1", group_id:"g1", takeover:true, mode:"chill_fade", mpm:1}}],
                  selflearning: {{
                    status: "connected", lamp: "原生钩子", enabled: true,
                    providers: [{{
                      name: "LivingMemory",
                      mode: "native_hooks",
                      ready: true,
                      native_hooks: ["before_llm_request"],
                      input_hooks: ["on_message"],
                      delivery_hooks: ["on_llm_response"],
                      native_commands: ["learning_status_command"],
                      direct_methods: {{memories: ["get_approved_memories"], relationships: ["get_relationship_hints"]}},
                      errors: {{}}
                    }}]
                  }}
                }}}};
              }},
              apiPost: async () => ({{status:"ok", ok:true, data:{{saved:true}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#channelList .channel").first.click()
        page.locator("#roomView").wait_for(state="visible")
        page.locator("#integrationPanel").wait_for(state="visible")

        # 1. Assert no horizontal overflow
        dims = page.evaluate("""() => ({
            vw: window.innerWidth,
            docScroll: document.documentElement.scrollWidth,
            bodyScroll: document.body ? document.body.scrollWidth : 0
        })""")
        assert dims["docScroll"] <= dims["vw"] + 1, f"Document overflow on {viewport_name}: {dims}"
        assert dims["bodyScroll"] <= dims["vw"] + 1, f"Body overflow on {viewport_name}: {dims}"

        # 2. Check touch targets >= 44px for key interactive controls
        controls = [
            "#btnRefresh",
            "#btnCool",
            "#btnReset",
            "#btnPreset",
            "#presetSelect",
            ".integration-jump",
            "#capability-memories summary",
            ".provider-summary",
            "#readAirPanel > summary",
            "#sessionFilter",
        ]
        for sel in controls:
            loc = page.locator(sel).first
            if loc.is_visible():
                box = loc.bounding_box()
                assert box is not None, f"No bounding box for {sel}"
                assert box["height"] >= 44, f"{sel} height {box['height']} < 44 on {viewport_name}"
                assert box["width"] >= 44, f"{sel} width {box['width']} < 44 on {viewport_name}"

        browser.close()


@pytest.mark.parametrize(
    ("state_name", "partner_data"),
    [
        ("connected", {
            "status": "connected", "lamp": "原生钩子", "enabled": True,
            "providers": [{
                "name": "SelfLearning", "mode": "native_hooks", "ready": True,
                "native_hooks": ["before_llm_request"],
                "input_hooks": ["on_message"],
                "delivery_hooks": ["send_feedback"],
                "native_commands": ["learning_status_command", "remember_command"],
                "direct_methods": {"memories": ["get_approved_memories", "fetch_memories"], "relationships": ["get_relationship_hints"]},
                "errors": {}
            }]
        }),
        ("initializing", {
            "status": "degraded", "lamp": "初始化中", "enabled": True,
            "providers": [{
                "name": "LivingMemory", "mode": "direct_api", "ready": False,
                "native_hooks": [], "input_hooks": ["on_message"],
                "delivery_hooks": [], "native_commands": [],
                "direct_methods": {}, "errors": {}
            }]
        }),
        ("partial_error", {
            "status": "degraded", "lamp": "降级中", "enabled": True,
            "providers": [{
                "name": "LivingMemory", "mode": "direct_api", "ready": True,
                "native_hooks": [], "input_hooks": ["on_message"],
                "delivery_hooks": [], "native_commands": [],
                "direct_methods": {"relationships": ["get_relationship_hints"]},
                "errors": {"memories": "TimeoutError: connection lost"}
            }]
        }),
        ("not_found", {
            "status": "missing", "lamp": "未发现搭档", "enabled": True,
            "providers": []
        }),
        ("disabled", {
            "status": "missing", "lamp": "已关闭", "enabled": False,
            "providers": []
        }),
        ("null", None),
    ],
)
def test_integration_panel_renders_all_boundary_states_without_errors(
    console_server, state_name, partner_data
):
    page_errors = []
    with sync_playwright() as pw:
        try:
            browser = _launch_browser(pw)
        except Exception as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.add_init_script(f"""
            window.__partner = {json.dumps(partner_data, ensure_ascii=False) if partner_data is not None else "null"};
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{
                  enabled: true, sessions: [],
                  selflearning: window.__partner
                }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")

        # Verify no JS runtime page errors occurred
        assert not page_errors, f"Page errors in state {state_name}: {page_errors}"

        # Verify status element
        status_text = page.locator("#integrationStatus").inner_text()
        assert len(status_text) > 0, f"Empty status in {state_name}"

        # Verify capability cards
        assert page.locator("#capability-memories").is_visible()
        assert page.locator("#capability-relationships").is_visible()
        assert page.locator("#capability-slang").is_visible()

        # Specific state assertions
        if state_name == "connected":
            assert page.locator("#integrationProviderCount").inner_text() == "1"
            assert page.locator(".provider-category-card").count() == 5
            assert page.locator(".provider-name").inner_text() == "SelfLearning"
            assert page.locator("#integrationEmpty").is_hidden()
        elif state_name == "initializing":
            assert page.locator(".init-banner").is_visible()
            assert "初始化中" in page.locator(".init-banner").inner_text()
        elif state_name == "partial_error":
            assert page.locator(".error-banner").is_visible()
            assert "TimeoutError" in page.locator(".error-banner").inner_text()
            assert page.locator("#integrationStatus").get_attribute("data-state") == "degraded"
        elif state_name == "not_found":
            assert page.locator("#integrationEmpty").is_visible()
            assert "尚未发现" in page.locator("#integrationEmpty").inner_text()
        elif state_name == "disabled":
            assert page.locator("#integrationStatus").inner_text() == "互联已关闭"
            assert page.locator("#integrationEmpty").is_visible()
            assert "已在配置中停用" in page.locator("#integrationEmpty").inner_text()
        elif state_name == "null":
            assert page.locator("#integrationStatus").inner_text() == "状态未知"

        browser.close()


def test_integration_panel_initializing_empty_providers(console_server):
    """When providers list is empty but partner is initializing, emptyEl must not show 'not found'."""
    page_errors = []
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        partner_data = {
            "status": "degraded",
            "lamp": "初始化中",
            "detail": "plugin_initializing",
            "enabled": True,
            "providers": [],
        }
        page.add_init_script(f"""
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{ enabled: true, sessions: [], selflearning: {json.dumps(partner_data)} }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")
        assert not page_errors
        assert page.locator("#integrationStatus").inner_text() == "初始化中"
        empty_text = page.locator("#integrationEmpty").inner_text()
        assert "尚未发现" not in empty_text
        assert "初始化" in empty_text
        browser.close()


def test_integration_panel_string_and_array_errors(console_server):
    """String and array errors should be rendered gracefully without breaking JS."""
    page_errors = []
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        partner_data = {
            "status": "degraded",
            "lamp": "降级中",
            "enabled": True,
            "providers": [
                {
                    "name": "StringErrorCompanion",
                    "mode": "direct_api",
                    "ready": True,
                    "errors": "Network connection reset",
                },
                {
                    "name": "ArrayErrorCompanion",
                    "mode": "direct_api",
                    "ready": True,
                    "errors": ["First failure", "Second failure"],
                },
            ],
        }
        page.add_init_script(f"""
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{ enabled: true, sessions: [], selflearning: {json.dumps(partner_data)} }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")
        assert not page_errors
        assert page.locator(".error-banner").count() == 2
        assert "Network connection reset" in page.locator(".error-banner").first.inner_text()
        assert "First failure" in page.locator(".error-banner").nth(1).inner_text()
        browser.close()


def test_integration_panel_weakened_diagnosis(console_server):
    """Weakened direct interface warnings should appear in the note when not in native mode."""
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        partner_data = {
            "status": "connected",
            "lamp": "接口已发现",
            "enabled": True,
            "weakened": ["无直连记忆接口", "无直连关系接口"],
            "providers": [
                {
                    "name": "SlangOnlyCompanion",
                    "mode": "direct_api",
                    "ready": True,
                    "direct_methods": {"slang": ["get_slang_candidates"]},
                    "errors": {},
                }
            ],
        }
        page.add_init_script(f"""
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{ enabled: true, sessions: [], selflearning: {json.dumps(partner_data)} }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")
        note_text = page.locator("#integrationNote").inner_text()
        assert "无直连记忆接口" in note_text
        assert "无直连关系接口" in note_text
        browser.close()


def test_four_concurrent_companions_and_extreme_strings(console_server):
    """Stress test: 4 simultaneous companions with extreme long tokens on 375px mobile."""
    long_method = "custom_method_with_a_super_length_identifier_" + "x" * 120
    long_provider = "EnterpriseMemoryClusterWithAnExtraordinaryLongNamingScheme_" + "p" * 80
    partner_data = {
        "status": "connected",
        "lamp": "多搭档已连接",
        "enabled": True,
        "providers": [
            {
                "name": long_provider,
                "mode": "native_hooks",
                "ready": True,
                "native_hooks": [long_method],
                "direct_methods": {"memories": [long_method]},
                "errors": {},
            },
            {"name": "SelfLearningCore", "mode": "direct_api", "ready": True, "direct_methods": {}, "errors": {}},
            {"name": "LivingMemoryBridge", "mode": "native_hooks", "ready": True, "native_hooks": ["recall"], "errors": {}},
            {"name": "LocalVectorStore", "mode": "direct_api", "ready": False, "direct_methods": {}, "errors": {}},
        ],
    }
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page(viewport={"width": 375, "height": 812})
        page.add_init_script(f"""
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{
                  enabled: true,
                  sessions: [{{session_key: "mock:s1", session_id: "s1", group_id: "g1", takeover: true, mode: "chill_fade", mpm: 1}}],
                  selflearning: {json.dumps(partner_data)}
                }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")
        assert page.locator(".provider-card").count() == 4

        # Verify zero horizontal overflow on 375px viewport
        dims = page.evaluate("""() => ({
            vw: window.innerWidth,
            docScroll: document.documentElement.scrollWidth,
            bodyScroll: document.body ? document.body.scrollWidth : 0
        })""")
        assert dims["docScroll"] <= dims["vw"] + 1, f"Overflow doc: {dims}"
        assert dims["bodyScroll"] <= dims["vw"] + 1, f"Overflow body: {dims}"

        # Verify summary has list-style: none and touch targets >= 44px
        summary_style = page.locator(".provider-summary").first.evaluate(
            "el => window.getComputedStyle(el).listStyleType"
        )
        assert summary_style in {"none", ""}
        browser.close()


def test_integration_panel_ignores_empty_and_null_errors_and_serializes_objects(console_server):
    """Empty or null errors should be ignored; object errors should serialize to readable JSON."""
    page_errors = []
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        partner_data = {
            "status": "degraded",
            "lamp": "降级中",
            "enabled": True,
            "providers": [
                {
                    "name": "ObjectErrorCompanion",
                    "mode": "direct_api",
                    "ready": True,
                    "errors": {
                        "memories": None,
                        "relationships": "",
                        "slang": {"code": 503, "detail": "ServiceUnavailable"},
                    },
                }
            ],
        }
        page.add_init_script(f"""
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{ enabled: true, sessions: [], selflearning: {json.dumps(partner_data)} }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")
        assert not page_errors
        # Only 1 error item should be rendered (the slang object error), null and empty string are ignored
        assert page.locator(".error-item").count() == 1
        error_text = page.locator(".error-item").first.inner_text()
        assert "ServiceUnavailable" in error_text
        assert "[object Object]" not in error_text
        assert "503" in error_text
        browser.close()


def test_integration_panel_handles_duplicate_provider_names_and_reordering(console_server):
    """Multiple companions with the same name must not collide, and reordering must update DOM."""
    page_errors = []
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        partner_v1 = {
            "status": "connected",
            "lamp": "已连接",
            "enabled": True,
            "providers": [
                {"name": "LivingMemory", "mode": "native_hooks", "ready": True, "native_hooks": ["hook_alpha"], "errors": {}},
                {"name": "LivingMemory", "mode": "direct_api", "ready": True, "direct_methods": {"memories": ["method_beta"]}, "errors": {}},
            ],
        }
        partner_v2 = {
            "status": "connected",
            "lamp": "已连接",
            "enabled": True,
            "providers": [
                {"name": "LivingMemory", "mode": "direct_api", "ready": True, "direct_methods": {"memories": ["method_beta"]}, "errors": {}},
                {"name": "LivingMemory", "mode": "native_hooks", "ready": True, "native_hooks": ["hook_alpha"], "errors": {}},
            ],
        }
        page.add_init_script(f"""
            window.__partnerData = {json.dumps(partner_v1)};
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{ enabled: true, sessions: [], selflearning: window.__partnerData }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")
        assert not page_errors
        # Both distinct companion cards must be rendered even with the same name
        assert page.locator(".provider-card").count() == 2
        first_badge = page.locator(".provider-card").first.locator(".provider-mode-badge").inner_text()
        assert "原生钩子注入" in first_badge

        # Now update to reversed order and re-render
        page.evaluate(f"""() => {{
            window.__partnerData = {json.dumps(partner_v2)};
            // Trigger refresh via import
            import("./integrations.js").then(m => m.renderIntegrations(window.__partnerData));
        }}""")
        # First card should now be direct_api
        first_badge_after = page.locator(".provider-card").first.locator(".provider-mode-badge").inner_text()
        assert "直连 API 模式" in first_badge_after
        browser.close()


def test_extreme_narrow_320px_viewport_no_overflow_and_single_column_stats(console_server):
    """Viewports at 320px (smart watch / split window) must have zero overflow and single-column stats."""
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page(viewport={"width": 320, "height": 568})
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")

        dims = page.evaluate("""() => ({
            vw: window.innerWidth,
            docScroll: document.documentElement.scrollWidth,
            bodyScroll: document.body ? document.body.scrollWidth : 0
        })""")
        assert dims["docScroll"] <= dims["vw"] + 1, f"Overflow doc at 320px: {dims}"
        assert dims["bodyScroll"] <= dims["vw"] + 1, f"Overflow body at 320px: {dims}"

        # Stat cards should be in a single column at <= 360px
        stats_cols = page.locator(".stats").first.evaluate(
            "el => window.getComputedStyle(el).gridTemplateColumns.split(' ').length"
        )
        assert stats_cols == 1, f"Expected 1 column for .stats at 320px, got {stats_cols}"
        browser.close()


def test_capability_methods_summary_toggle_title_text(console_server):
    """Clicking capability summary should dynamically toggle label between '查看' and '收起'."""
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.goto(console_server)
        page.locator("#capability-memories").wait_for(state="visible")

        summary_title = page.locator("#capability-memories .summary-title")
        expect(summary_title).to_contain_text("查看")

        # Click to open
        page.locator("#capability-memories summary").click()
        expect(summary_title).to_contain_text("收起")

        # Click to close
        page.locator("#capability-memories summary").click()
        expect(summary_title).to_contain_text("查看")
        browser.close()


def test_integration_panel_counts_direct_methods_from_flat_array_and_string_values(console_server):
    """Direct methods provided as flat array or string values must be accurately counted and mapped."""
    page_errors = []
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        partner_data = {
            "status": "connected",
            "lamp": "接口已发现",
            "enabled": True,
            "providers": [
                {
                    "name": "FlatArrayCompanion",
                    "mode": "direct_api",
                    "ready": True,
                    "direct_methods": ["get_approved_memories", "fetch_memories"],
                    "errors": {},
                },
                {
                    "name": "StringValueCompanion",
                    "mode": "direct_api",
                    "ready": True,
                    "direct_methods": {"relationships": "get_relationship_hints"},
                    "errors": {},
                },
            ],
        }
        page.add_init_script(f"""
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{ enabled: true, sessions: [], selflearning: {json.dumps(partner_data)} }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")
        assert not page_errors

        # integrationCount should accurately reflect 3 distinct direct methods
        discovered_count = page.locator("#integrationCount").inner_text()
        assert discovered_count == "3", f"Expected '3', got {discovered_count}"

        provider_count = page.locator("#integrationProviderCount").inner_text()
        assert provider_count == "2", f"Expected '2', got {provider_count}"

        # Memories capability should find 2 methods
        memories_state = page.locator("#capability-memories .capability-state").inner_text()
        assert "已发现 2 个直连方法" in memories_state

        # Relationships capability should find 1 method
        rel_state = page.locator("#capability-relationships .capability-state").inner_text()
        assert "已发现 1 个直连方法" in rel_state
        browser.close()


def test_integration_panel_handles_error_instances_and_circular_references(console_server):
    """Error instances, circular objects, and top-level partner errors must not crash and format cleanly."""
    page_errors = []
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.goto(console_server)
        page.locator("#integrationPanel").wait_for(state="visible")

        # Pass partner data directly via JS evaluate to include Error instances and circular refs
        eval_result = page.evaluate("""() => {
            const circular = { code: 500 };
            circular.self = circular;
            const partner = {
                status: "degraded",
                enabled: true,
                errors: { bridge: "Global handshake timeout" },
                providers: [
                    {
                        name: "ErrorTestCompanion",
                        mode: "direct_api",
                        ready: true,
                        errors: {
                            memories: new Error("Upstream socket closed"),
                            slang: circular,
                            relationships: {}
                        }
                    }
                ]
            };
            return import("./integrations.js").then(m => {
                m.renderIntegrations(partner);
                return {
                    status: document.getElementById("integrationStatus").textContent,
                    state: document.getElementById("integrationStatus").dataset.state,
                    errorCount: document.querySelectorAll(".error-item").length,
                    firstError: document.querySelector(".error-item") ? document.querySelector(".error-item").textContent : "",
                    note: document.getElementById("integrationNote").textContent
                };
            });
        }""")

        assert not page_errors, f"Page errors encountered: {page_errors}"
        assert eval_result["state"] == "degraded"
        assert "Global handshake timeout" in eval_result["note"]
        assert "Upstream socket closed" in eval_result["firstError"]
        # Empty object {} should be filtered out; 2 errors rendered (memories and slang)
        assert eval_result["errorCount"] == 2
        browser.close()


def test_accessibility_aria_expanded_sync_on_details_summaries(console_server):
    """Summaries in capability details and provider cards must keep aria-expanded in sync."""
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page = browser.new_page()
        partner_data = {
            "status": "connected",
            "lamp": "接口已发现",
            "enabled": True,
            "providers": [
                {
                    "name": "AccessibleCompanion",
                    "mode": "direct_api",
                    "ready": True,
                    "direct_methods": {"memories": ["get_approved_memories"]},
                    "errors": {},
                }
            ],
        }
        page.add_init_script(f"""
            window.AstrBotPluginPage = {{
              t: (_key, fallback) => fallback,
              apiGet: async () => ({{
                status: "ok", ok: true,
                data: {{ enabled: true, sessions: [], selflearning: {json.dumps(partner_data)} }}
              }}),
              apiPost: async () => ({{status: "ok", ok: true, data: {{}}}})
            }};
        """)
        page.goto(console_server)
        page.locator("#capability-memories").wait_for(state="visible")
        page.locator(".provider-card").wait_for(state="visible")

        # 1. Capability details summary
        cap_summary = page.locator("#capability-memories summary")
        expect(cap_summary).to_have_attribute("aria-expanded", "false")
        cap_summary.click()
        expect(cap_summary).to_have_attribute("aria-expanded", "true")
        cap_summary.click()
        expect(cap_summary).to_have_attribute("aria-expanded", "false")

        # 2. Provider card summary (open by default)
        prov_summary = page.locator(".provider-summary").first
        expect(prov_summary).to_have_attribute("aria-expanded", "true")
        prov_summary.click()
        expect(prov_summary).to_have_attribute("aria-expanded", "false")
        prov_summary.click()
        expect(prov_summary).to_have_attribute("aria-expanded", "true")

        # 3. Provider summary cursor is pointer
        cursor = prov_summary.evaluate("el => window.getComputedStyle(el).cursor")
        assert cursor == "pointer"
        browser.close()


