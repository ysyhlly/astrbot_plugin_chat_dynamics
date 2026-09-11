from . import test_ui_theme_browser as harness

browser = harness.browser
page_server = harness.page_server


def test_annotation_save_export_and_escape(browser, page_server):
    with browser.new_context(viewport={"width": 390, "height": 844}, accept_downloads=True) as context:
        harness.setup(context, {"ui": "day"})
        context.add_init_script("""
          const original = window.AstrBotPluginPage.apiGet;
          window.annotationWrites = [];
          window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
            if (endpoint === 'replay') return {ok:true,data:{sessions:[],topic_blocks:[{
              session_id:'room',topic_id:'t1',topic_title:'Topic',start_ts:1,end_ts:2,events:[],
              messages:[{msg_id:'m',text:'<script>unsafe</script>',topic_id:'t1',confidence:.6,candidates:[[.6,'t1']],decision_trace:{routing_schema_version:2,recipient:{ids:['<img src=x onerror=alert(1)>'],bot_targeted:true,ambiguous:false},topic:{ambiguous:true},participation:{level:'STRONG',should_reply:null}}}]
            }]}};
            if (endpoint === 'topic_annotations') return {ok:true,data:{records:window.annotationWrites,metrics:{total:window.annotationWrites.length,error_counts:{},sample_note:'selected sample'}}};
            return original(endpoint, params);
          };
          const post = window.AstrBotPluginPage.apiPost;
          window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
            if (endpoint === 'topic_annotations') {window.annotationWrites = [body]; return {ok:true,data:{saved:true}};}
            return post(endpoint, body);
          };
        """)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/replay/index.html")
        page.locator(".replay-block").click()
        assert page.locator("#annotationMessages").inner_text().find("<script>unsafe</script>") >= 0
        assert page.locator("#annotationMessages script").count() == 0
        assert "收件人歧义：否" in page.locator("[data-trace-summary]").inner_text()
        assert "应回复：待决" in page.locator("[data-trace-summary]").inner_text()
        assert page.locator("#annotationMessages img").count() == 0
        page.get_by_text("查看决策记录", exact=True).click()
        assert '"should_reply": null' in page.locator(".decision-trace").inner_text()
        page.locator("[data-target]").select_option("UNKNOWN")
        page.locator("[data-annotate]").click()
        page.wait_for_function("document.querySelector('#annotationStatus').textContent === '已保存标注。'")
        assert page.evaluate("window.annotationWrites[0].expected_topic") == "UNKNOWN"
        assert page.evaluate("window.annotationWrites[0].session_key") == "room"
        page.locator(".recipient-editor summary").click()
        page.locator('[data-recipient="bot_targeted"]').select_option("false")
        page.locator('[data-recipient="expected_reply"]').select_option("false")
        page.locator('[data-recipient="recipient_correct"]').select_option("false")
        page.locator('[data-recipient="recipient_ids"]').fill("alice, bob, alice")
        page.locator('[data-recipient="subject_ids"]').fill("[]")
        page.locator('[data-recipient="recipient_error_type"]').select_option("false_bot")
        page.locator("[data-annotate]").click()
        page.wait_for_function("window.annotationWrites[0].recipient_error_type === 'false_bot'")
        page.wait_for_function("document.querySelector('[data-saved-annotation]').textContent.includes('alice, bob')")
        body = page.evaluate("window.annotationWrites[0]")
        assert body["bot_targeted"] is False and body["expected_reply"] is False
        assert body["recipient_ids"] == ["alice", "bob"]
        assert body["subject_ids"] == []
        page.locator("#btnCloseDetail").click()
        page.locator(".replay-block").click()
        page.wait_for_function("document.querySelector('[data-recipient=bot_targeted]').value === 'false'")
        page.locator(".recipient-editor summary").click()
        assert page.locator('[data-recipient="recipient_ids"]').input_value() == "alice, bob"
        assert page.locator('[data-recipient="subject_ids"]').input_value() == "[]"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        with page.expect_download() as download:
            page.locator("#btnExportAnnotations").click()
        assert download.value.suggested_filename == "topic-annotations.json"
        assert not errors
