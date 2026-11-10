import pytest
from .test_config_simple_browser import setup_config
from .test_ui_theme_browser import browser as browser, page_server as page_server


@pytest.mark.parametrize('width', [390, 1366])
def test_native_rerank_selection_is_separate_from_chat(browser, page_server, width):
    with browser.new_context(viewport={'width': width, 'height': 940}) as context:
        setup_config(context)
        context.add_init_script("""(() => {
          const api = window.AstrBotPluginPage, get = api.apiGet;
          window.__stored.decision_provider = 'jev-test';
          api.apiGet = async (endpoint, params) => endpoint !== 'providers' ? get(endpoint, params) :
            {ok:true,data:{chat:[{id:'chat',label:'聊天模型'}],decision:[{id:'jev-test',label:'决策模型',decision:true}],embedding:[],
              rerank:[{id:'native-rank',label:'重排序模型'}],systemone_router_ready:true}};
        })();""")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'{page_server}/config/index.html')
        page.wait_for_load_state('networkidle')
        page.locator('#configSearch').fill('discussion_reranker_provider')
        select = page.locator('[data-config-key="discussion_reranker_provider"]')
        assert select.locator('option[value="native-rank"]').count() == 1
        assert select.locator('option[value="chat"]').count() == 0
        select.select_option('native-rank')
        page.locator('#btnConfigSave').click()
        page.wait_for_function("window.__stored.discussion_reranker_provider === 'native-rank'")
        assert not errors
