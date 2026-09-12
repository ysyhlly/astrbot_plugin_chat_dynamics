"""Parameter categories preserve every schema field and unsaved edits."""
import json

import pytest

from .test_ui_theme_browser import ROOT, browser as browser, page_server as page_server, setup


@pytest.mark.parametrize("width,theme", [(1366, "day"), (1366, "night"), (375, "day"), (375, "night")])
def test_config_categories_navigation_and_save(browser, page_server, width, theme):
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    with browser.new_context(viewport={"width": width, "height": 900}) as context:
        setup(context, {"ui": theme})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/config/index.html?ui={theme}")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("[data-config-key]")
        keys = page.locator("[data-config-key]").evaluate_all("nodes => nodes.map(n => n.dataset.configKey)")
        assert len(keys) == len(set(keys)) == len(schema)
        assert set(keys) == set(schema)
        assert page.locator('[data-group-id="other"]').count() == 0
        page.locator("#btnCollapseGroups").click()
        page.locator("#configCategory").select_option("routing")
        group = page.locator('[data-group-id="routing"]')
        assert group.get_attribute("open") is not None
        assert group.locator("summary").evaluate("n => n === document.activeElement")
        control = page.locator('[data-config-key="topic_window_seconds"]')
        control.fill("240")
        page.locator("#configSearch").fill("记忆与联动")
        assert page.locator('.config-field:visible').count() == 4
        page.locator("#configCategory").select_option("routing")
        assert control.input_value() == "240"
        assert not page.locator("#btnConfigSave").is_disabled()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        (ROOT / "output").mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / "output" / f"config-categories-{width}-{theme}.png"), full_page=True)
        page.evaluate("""() => {
          window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
            window.__savedConfig = body.config;
            return {ok:true,data:{schema:window.__schema,stored:body.config,effective:body.config}};
          };
        }""")
        page.locator("#btnConfigSave").click()
        page.wait_for_function("window.__savedConfig?.topic_window_seconds === 240")
        assert set(page.evaluate("Object.keys(window.__savedConfig)")) == set(schema)
        assert not errors


def test_config_directory_dirty_revert_and_empty_search(browser, page_server):
    with browser.new_context(viewport={"width": 1440, "height": 1000}) as context:
        setup(context, {"ui": "day"})
        page = context.new_page()
        page.goto(f"{page_server}/config/index.html")
        page.wait_for_selector("[data-config-key]")
        page.locator('[data-category="routing"]').click()
        control = page.locator('[data-config-key="topic_window_seconds"]')
        initial = control.input_value()
        control.fill("241")
        assert "1 项未保存" in page.locator("#actionTitle").inner_text()
        assert control.locator("..").get_attribute("data-dirty") == "true"
        control.fill(initial)
        assert page.locator("#btnConfigSave").is_disabled()
        page.locator("#configSearch").fill("no-such-config-xyz")
        assert page.locator("#configEmpty").is_visible()
        page.locator("#btnClearSearch").click()
        assert page.locator("#configEmpty").is_hidden()
        assert page.locator("#configSearch").input_value() == ""
        assert page.locator("#configSearch").evaluate("node => node === document.activeElement")
