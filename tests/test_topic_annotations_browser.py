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
              messages:[{msg_id:'m',text:'<script>unsafe</script>',topic_id:'t1',confidence:.6,candidates:[[.6,'t1']]}]
            }]}};
            if (endpoint === 'topic_annotations') return {ok:true,data:{records:window.annotationWrites,metrics:{total:window.annotationWrites.length,error_counts:{},sample_note:'selected sample'}}};
            return original(endpoint, params);
          };
          const post = window.AstrBotPluginPage.apiPost;
          window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
            if (endpoint === 'topic_annotations') {window.annotationWrites.push(body); return {ok:true,data:{saved:true}};}
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
        page.locator("[data-target]").select_option("UNKNOWN")
        page.locator("[data-annotate]").click()
        page.wait_for_function("document.querySelector('#annotationStatus').textContent === '已保存标注。'")
        assert page.evaluate("window.annotationWrites[0].expected_topic") == "UNKNOWN"
        assert page.evaluate("window.annotationWrites[0].session_key") == "room"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        with page.expect_download() as download:
            page.locator("#btnExportAnnotations").click()
        assert download.value.suggested_filename == "topic-annotations.json"
        assert not errors
