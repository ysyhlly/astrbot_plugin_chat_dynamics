import pytest
from .test_config_simple_browser import setup_config
from .test_ui_theme_browser import ROOT, browser as browser, page_server as page_server, setup


@pytest.mark.parametrize('width', [390, 1366])
def test_llm_selection_saves_without_jev_and_preserves_both_model_drafts(browser, page_server, width):
    with browser.new_context(viewport={'width': width, 'height': 960}) as context:
        setup_config(context)
        context.add_init_script("""(() => {
          const api=window.AstrBotPluginPage,get=api.apiGet;
          window.__stored.decision_provider='';
          api.apiGet=async(name,params)=>name!=='providers'?get(name,params):
            {ok:true,data:{chat:[{id:'existing-model',label:'原回复模型'},{id:'judge',label:'普通决策模型'}],
              decision:[{id:'native',label:'Jev 模型',decision:true}],embedding:[],rerank:[],systemone_router_ready:true}};
        })();""")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'{page_server}/config/index.html')
        page.wait_for_load_state('networkidle')
        page.locator('[data-config-key="takeover_groups"]').fill('123')
        mode = page.locator('[data-config-key="participation_backend"]')
        mode.select_option('llm')
        llm = page.locator('[data-config-key="decision_llm_provider"]')
        assert llm.is_visible() and not page.locator('[data-config-key="decision_provider"]').is_visible()
        assert llm.locator('option[value="native"]').count() == 0
        llm.select_option('judge')
        page.locator('[data-config-key="decision_llm_timeout"]').fill('5')
        assert '普通 LLM' in page.locator('#configDecisionHeadline').inner_text()
        page.locator('#btnRefreshModels').click()
        page.wait_for_function("document.querySelector('#configNote').textContent.includes('已刷新')")
        assert llm.input_value() == 'judge'
        assert page.locator('[data-config-key="takeover_groups"]').input_value() == '123'
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__stored.participation_backend==='llm'")
        assert page.evaluate("window.__stored.decision_provider") == ''
        assert page.evaluate("window.__stored.decision_llm_provider") == 'judge'
        mode.select_option('jev')
        page.locator('[data-config-key="decision_provider"]').select_option('native')
        mode.select_option('llm')
        assert llm.input_value() == 'judge'
        mode.select_option('jev')
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__stored.participation_backend==='jev'")
        assert page.evaluate("window.__stored.decision_provider") == 'native'
        assert page.evaluate("window.__stored.decision_llm_provider") == 'judge'
        assert page.evaluate("window.__stored.decision_llm_timeout") == 5
        assert page.evaluate("window.__stored.reply_provider") == 'existing-model'
        mode.select_option('llm')
        (ROOT / 'output').mkdir(exist_ok=True)
        page.screenshot(path=str(ROOT / 'output' / f'llm-participation-settings-{width}.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert not errors


def test_llm_status_page_names_the_selected_backend_without_reading_history(browser, page_server):
    with browser.new_context() as context:
        setup(context, {})
        context.add_init_script("""(() => {
          const api=window.AstrBotPluginPage,get=api.apiGet;
          api.apiGet=async(name,params)=>name!=='decision/status'?get(name,params):
            {ok:true,data:{enabled:true,backend:'llm',decision_mode:'persona_model',provider_id:'judge',
              service:{model:'judge-model',calls:2,failures:0,last_latency_ms:230,status:'available',configured:true,available:true}}};
        })();""")
        page = context.new_page()
        page.goto(f'{page_server}/learning/index.html')
        page.wait_for_function("document.querySelector('#mode').textContent==='普通 LLM 参与决策'")
        assert page.locator('#model').inner_text() == 'judge-model'
        assert page.locator('#calls').inner_text() == '2'
        assert page.locator('#latency').inner_text() == '230 ms'
