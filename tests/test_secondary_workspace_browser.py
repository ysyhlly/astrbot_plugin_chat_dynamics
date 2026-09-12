"""Real-browser contracts for the ancillary workspaces using the host bridge seam."""
from pathlib import Path
import os

import pytest
from .test_ui_theme_browser import browser, page_server
from .test_console_redesign_verification import expect


def setup_workspace(context):
    context.add_init_script(r"""
      localStorage.setItem('cd_wire_umo', 'group-a');
      window.writes = [];
      window.stored = {presence_knob:'sensible', relay_baton_enabled:true};
      window.book = {anniversaries:[{id:'a1',title:'群庆',month:6,day:12}],reminders:[],slang_trials:[]};
      const ok = data => ({ok:true,data});
      window.AstrBotPluginPage = {
        ready: async () => {}, t: (_key, fallback) => fallback,
        apiGet: async (endpoint, params = {}) => {
          if(endpoint === 'ui_preferences') return ok({});
          if(endpoint === 'page_nav') return ok({content_path:'/'+params.page+'/index.html'});
          if(endpoint === 'config') return ok({stored:window.stored});
          if(endpoint === 'read_air') return ok({sessions:[{session_key:'group-a'},{session_key:'group-b'}],occasion:{kind:'neutral'},one_liner:'大家正在轻松聊天',presence_knob:window.stored.presence_knob,thermometer:{quiet_count:8,intervene_count:2,intervene_ratio:0.2},decisions:[{reason_zh:'正在认真倾听',ts:1}]});
          if(endpoint === 'notebook') return ok(window.book);
          return ok({});
        },
        apiPost: async (endpoint, body) => {
          window.writes.push({endpoint,body});
          if(endpoint === 'config') {Object.assign(window.stored,body.config);return ok({stored:window.stored});}
          if(endpoint === 'notebook') {
            if(body.action === 'add_anniversary') window.book.anniversaries.push({...body,id:'a2'});
            if(body.action === 'remove_anniversary') window.book.anniversaries = window.book.anniversaries.filter(x => x.id !== body.id);
          }
          return ok({});
        }
      };
    """)


@pytest.mark.parametrize('name', ['today', 'manners', 'memory'])
@pytest.mark.parametrize('theme', ['day', 'night'])
@pytest.mark.parametrize('width', [1366, 375])
def test_secondary_workspace_layout(browser, page_server, name, theme, width):
    with browser.new_context(viewport={'width':width,'height':940}) as context:
        setup_workspace(context)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'{page_server}/{name}/index.html?ui={theme}')
        expect(page.locator('#linkLabel')).to_have_text('已连接')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.wire-card').first.evaluate('(node) => getComputedStyle(node).backgroundColor') != 'rgb(255, 255, 255)' if theme == 'night' else True
        screenshot_dir = os.environ.get('SECONDARY_SCREENSHOT_DIR')
        if screenshot_dir:
            path = Path(screenshot_dir)
            path.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(path/f'{name}-{theme}-{width}.png'),full_page=True)
        assert errors == []


def test_today_presence_scope_and_stale_read(browser, page_server):
    with browser.new_context() as context:
        setup_workspace(context)
        page = context.new_page()
        page.goto(f'{page_server}/today/index.html')
        page.locator('#btnLivelyTonight').click()
        expect(page.locator('#btnLivelyTonight')).to_have_attribute('aria-pressed','true')
        assert page.evaluate('window.writes[0]') == {'endpoint':'config','body':{'config':{'presence_knob':'lively'}}}
        page.evaluate("""() => {
          const original = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'read_air' && params.umo === 'group-b'
            ? new Promise(resolve => window.releaseRead = () => resolve({ok:true,data:{sessions:[{session_key:'group-a'},{session_key:'group-b'}],one_liner:'过期结果'}}))
            : original(endpoint, params);
        }""")
        page.locator('#sessionSelect').select_option('group-b')
        page.wait_for_function("typeof window.releaseRead === 'function'")
        page.locator('#sessionSelect').select_option('group-a')
        expect(page.locator('#oneLiner')).to_have_text('大家正在轻松聊天')
        page.evaluate('window.releaseRead()')
        expect(page.locator('#oneLiner')).to_have_text('大家正在轻松聊天')


def test_manners_toggle_and_keyboard_focus(browser, page_server):
    with browser.new_context() as context:
        setup_workspace(context)
        page = context.new_page()
        page.goto(f'{page_server}/manners/index.html')
        switch = page.locator('[data-key="relay_baton_enabled"]')
        expect(switch).to_have_attribute('aria-pressed','true')
        switch.focus()
        switch.press('Space')
        expect(switch).to_have_attribute('aria-pressed','false')
        expect(switch).to_be_focused()
        assert page.evaluate('window.writes[0].body') == {'config':{'relay_baton_enabled':False}}
        page.locator('#presenceRange').fill('2')
        page.locator('#btnSavePresence').click()
        expect(page.locator('#presenceNote')).to_have_text('已保存并应用到运行时。')
        assert page.evaluate('window.stored.presence_knob') == 'lively'


def test_memory_add_forget_draft_refresh_and_tabs(browser, page_server):
    with browser.new_context() as context:
        setup_workspace(context)
        page = context.new_page()
        page.goto(f'{page_server}/memory/index.html')
        expect(page.locator('#memoryCount')).to_have_text('1 条')
        page.locator('[name="title"]').fill('新的纪念日')
        page.locator('#btnLoad').click()
        expect(page.locator('[name="title"]')).to_have_value('新的纪念日')
        page.locator('[name="month"]').fill('9')
        page.locator('[name="day"]').fill('12')
        page.locator('#btnAdd').click()
        expect(page.locator('#memoryCount')).to_have_text('2 条')
        assert page.evaluate('window.writes[0].body.umo') == 'group-a'
        page.locator('.memory-card').filter(has_text='新的纪念日').get_by_role('button',name='忘掉').click()
        expect(page.locator('#memoryCount')).to_have_text('1 条')
        page.locator('#tab-anniversaries').focus()
        page.keyboard.press('ArrowRight')
        expect(page.locator('#tab-reminders')).to_be_focused()
        expect(page.locator('#tab-reminders')).to_have_attribute('aria-selected','true')
        expect(page.locator('[name="due_local"]')).to_be_visible()


def test_memory_late_response_cannot_restore_other_session(browser, page_server):
    with browser.new_context() as context:
        setup_workspace(context)
        page = context.new_page()
        page.goto(f'{page_server}/memory/index.html')
        expect(page.locator('#memoryCount')).to_have_text('1 条')
        page.evaluate("""() => {
          const original = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'notebook' && params.umo === 'group-b'
            ? new Promise(resolve => window.releaseBook = () => resolve({ok:true,data:{anniversaries:[{id:'b',title:'过期会话记录'}]}}))
            : original(endpoint, params);
        }""")
        page.locator('#sessionSelect').select_option('group-b')
        page.wait_for_function("typeof window.releaseBook === 'function'")
        page.locator('#sessionSelect').select_option('group-a')
        expect(page.locator('#listHost')).to_contain_text('群庆')
        page.evaluate('window.releaseBook()')
        expect(page.locator('#listHost')).not_to_contain_text('过期会话记录')
