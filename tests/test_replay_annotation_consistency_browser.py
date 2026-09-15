"""Regression coverage for annotation identity and edits during requests."""
from . import test_ui_theme_browser as harness

browser = harness.browser
page_server = harness.page_server


def test_cancel_refresh_keeps_identity_and_slow_save_keeps_new_edits(browser, page_server):
    with browser.new_context() as context:
        harness.setup(context, {"ui": "day"})
        context.add_init_script("""
          window.replayRead = 0;
          window.savedAnnotations = [];
          window.AstrBotPluginPage.apiGet = async (endpoint) => {
            if (endpoint === 'replay') {
              const id = ++window.replayRead === 1 ? 'a' : 'b';
              return {ok:true,data:{sessions:[],topic_blocks:[{
                session_id:'room-'+id,topic_id:id,topic_title:id,start_ts:1,end_ts:2,
                messages:[{msg_id:'message-'+id,text:'Message '+id,topic_id:id}],events:[]
              }]}};
            }
            return {ok:true,data:{records:[],metrics:{total:0,error_counts:{},sample_note:''}}};
          };
          window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
            window.savedAnnotations.push(body);
            return new Promise(resolve => { window.finishAnnotation = () => resolve({ok:true,data:{}}); });
          };
        """)
        page = context.new_page()
        page.goto(f"{page_server}/replay/index.html")
        page.locator('.replay-block').click()
        page.locator('[data-target]').select_option('NEW')
        page.on('dialog', lambda dialog: dialog.dismiss())
        # Programmatic refresh represents a response arriving while the modal is open.
        page.evaluate("document.querySelector('#btnRefresh').click()")
        page.wait_for_function('window.replayRead === 2')
        assert page.locator('#detailTitle').inner_text() == 'a'
        assert page.locator('.replay-block').inner_text() == 'a'
        page.locator('[data-annotate]').click()
        page.wait_for_function('window.savedAnnotations.length === 1')
        assert page.evaluate('window.savedAnnotations[0].session_key') == 'room-a'
        assert page.evaluate('window.savedAnnotations[0].msg_id') == 'message-a'
        page.locator('[data-target]').select_option('UNKNOWN')
        page.evaluate('window.finishAnnotation()')
        page.wait_for_function("!document.querySelector('[data-annotate]').disabled")
        assert page.locator('[data-target]').input_value() == 'UNKNOWN'
        assert page.locator('.annotation-row').get_attribute('data-dirty') == 'true'
        assert '尚未保存' in page.locator('#annotationStatus').inner_text()
        page.locator('[data-annotate]').click()
        page.wait_for_function('window.savedAnnotations.length === 2')
        assert page.evaluate('window.savedAnnotations[1].expected_topic') == 'UNKNOWN'
        page.evaluate('window.finishAnnotation()')
        page.wait_for_function("!document.querySelector('[data-annotate]').disabled")
        assert page.locator('.annotation-row').get_attribute('data-dirty') is None
