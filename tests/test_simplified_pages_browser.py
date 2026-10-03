"""Native status, read-only exports and the single participation control entry."""
import json

import pytest
from .test_ui_theme_browser import browser as browser, page_server as page_server, setup


@pytest.mark.parametrize("action_confidence", [0.93, None])
def test_console_reports_action_confidence_instead_of_old_style_summary(browser, page_server, action_confidence):
    with browser.new_context() as context:
        setup(context, {})
        context.add_init_script("window.__actionConfidence = " + json.dumps(action_confidence))
        context.add_init_script("""(() => {
          const api=window.AstrBotPluginPage, get=api.apiGet;
          const session={session_key:'mock:GroupMessage:g1',session_id:'g1',group_id:'g1',
            takeover:true,dag_nodes:1,pending:0,mode:'neutral',mpm:0};
          api.apiGet=async (name,params) => {
            const result=await get(name,params);
            if(name==='overview') result.data.sessions=[session];
            if(name==='session') result.data={...session,decision_mode:'persona_model',
              interaction_state:'focused',model_decision:{backend:'jev',action:'reply',state:'focused',
                reason_code:'jev_action_accepted',target_message_ids:['m1'],
                jev:{confidence:0.15,action:{type:'choice',choice:'reply',confidence:window.__actionConfidence},
                  length:{type:'choice',choice:'detailed',confidence:0.15}}}};
            return result;
          };
        })();""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/console/index.html")
        page.locator('#tab-sessions').click()
        page.locator('#traceMeta').filter(has_text='jev_action_accepted').wait_for()
        trace = page.locator('#traceMeta').inner_text()
        assert 'Jev 决策' in trace
        assert ('动作置信 0.93' in trace) is (action_confidence is not None)
        assert '置信 0.15' not in trace and '置信 0.00' not in trace
        assert not errors


@pytest.mark.parametrize("width,theme", [(1366, "day"), (390, "night")])
def test_native_status_and_explicit_history_export(browser, page_server, width, theme):
    with browser.new_context(viewport={"width": width, "height": 900}, accept_downloads=True) as context:
        setup(context, {"ui": theme})
        context.add_init_script("""(() => {
          const api=window.AstrBotPluginPage, get=api.apiGet, post=api.apiPost;
          window.__historyReads=0;
          api.apiGet=async (name, params) => {
            if (name !== 'decision/status') return get(name,params);
            if (window.__statusFail) throw new Error('offline');
            return {ok:true,data:{enabled:true,decision_mode:'persona_model',provider_id:'native/jev',
              service:{model:'jev-1.13-free',calls:4,failures:1,last_latency_ms:970,configured:true,available:true,detail:'ready'}}};
          };
          api.apiPost=async (name,body) => {
            if(name !== 'decision/history') return post(name,body);
            window.__historyReads++;
            return {ok:true,data:{records:[{id:'fake',content_hidden:true}],next_cursor:null,content_hidden:true}};
          };
        })();""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/learning/index.html?ui={theme}")
        page.wait_for_function("document.querySelector('#model').textContent==='jev-1.13-free'")
        assert page.locator('#mode').inner_text() == 'Jev 直接决策'
        assert page.locator('#calls').inner_text() == '4'
        assert page.locator('#latency').inner_text() == '970 ms'
        assert page.locator('[data-nav-page]').count() == 4
        assert page.locator('#pluginSideNav [aria-current="page"]').count() == 1
        assert page.evaluate('window.__historyReads') == 0
        assert 'Kev' not in page.locator('body').inner_text()
        with page.expect_download() as download:
            page.locator('#export').click()
        assert download.value.suggested_filename == 'decision-history-0.jsonl'
        assert page.evaluate('window.__historyReads') == 1
        assert page.locator('#export').is_disabled()
        assert '隐私设置隐藏' in page.locator('#historyStatus').inner_text()
        page.evaluate('window.__statusFail=true')
        page.locator('#refresh').click()
        page.wait_for_function("document.querySelector('#status').dataset.error==='true'")
        assert page.locator('#model').inner_text() == '—'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert not errors


def test_replay_participation_link_opens_console_controls(browser, page_server):
    with browser.new_context() as context:
        setup(context, {})
        page = context.new_page()
        page.goto(f"{page_server}/replay/index.html")
        page.wait_for_load_state('networkidle')
        assert page.locator('#btnGhostTonight,#btnSensibleTonight,#btnLivelyTonight').count() == 0
        # Inspect the dialog without changing any runtime settings.
        page.locator('#btnOpenParticipation').evaluate('node => node.closest("dialog").showModal()')
        page.locator('#btnOpenParticipation').click()
        page.wait_for_url('**/console/index.html?**')
        assert page.locator('#tab-policy').get_attribute('aria-selected') == 'true'
        assert page.locator('#presenceSelect').is_visible()
