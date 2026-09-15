from . import test_ui_theme_browser as harness

browser = harness.browser
page_server = harness.page_server


def setup(context):
    harness.setup(context, {"ui": "day"})
    context.add_init_script("""
      const get = window.AstrBotPluginPage.apiGet;
      window.draftCalls = [];
      window.pendingDrafts = {};
      window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
        if (endpoint === 'replay') return {ok:true,data:{sessions:[{session_key:'room-a'},{session_key:'room-b'}],topic_blocks:[]}};
        return get(endpoint, params);
      };
      window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
        window.draftCalls.push({endpoint,body});
        return new Promise(resolve => { window.pendingDrafts[body.session_key] = resolve; });
      };
    """)


def test_outer_generation_without_topics_and_session_switch(browser, page_server):
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        setup(context)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/replay/index.html")
        page.wait_for_function("document.querySelector('#sessionSelect').options.length === 3")
        button = page.locator("#btnDraftAnnotations")
        assert button.is_disabled()
        page.locator("#sessionSelect").select_option("room-a")
        page.wait_for_function("!document.querySelector('#btnDraftAnnotations').disabled")
        assert page.locator("#detailDialog").is_hidden()
        assert button.is_visible()
        button.click()
        assert button.is_disabled()
        assert page.evaluate("window.draftCalls") == [{"endpoint": "annotation_draft", "body": {"session_key": "room-a", "refresh": True}}]
        page.locator("#sessionSelect").select_option("room-b")
        page.wait_for_function("!document.querySelector('#btnDraftAnnotations').disabled")
        page.evaluate("window.pendingDrafts['room-a']({ok:true,data:{state:'fresh',drafts:{m:{}},stats:{asked:1}}})")
        page.wait_for_function("!document.querySelector('#draftGenerationStatus').textContent.includes('正在')")
        assert "已生成" not in page.locator("#draftGenerationStatus").inner_text()
        page.locator("#sessionSelect").select_option("room-a")
        page.wait_for_function("document.querySelector('#draftGenerationStatus').textContent.includes('已生成 1 条')")
        assert "采纳后才成为标注" in page.locator("#draftGenerationStatus").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#detailDialog").is_hidden()
        assert not errors


def test_outer_generation_failure_recovers_button(browser, page_server):
    with browser.new_context() as context:
        setup(context)
        page = context.new_page()
        page.goto(f"{page_server}/replay/index.html")
        page.wait_for_function("document.querySelector('#sessionSelect').options.length === 3")
        page.locator("#sessionSelect").select_option("room-a")
        page.locator("#btnDraftAnnotations").click()
        page.evaluate("window.pendingDrafts['room-a']({ok:false,error:'provider unavailable'})")
        page.wait_for_function("!document.querySelector('#btnDraftAnnotations').disabled")
        assert "正在生成" not in page.locator("#draftGenerationStatus").inner_text()
        page.locator("#sessionSelect").select_option("")
        assert page.locator("#btnDraftAnnotations").is_disabled()
