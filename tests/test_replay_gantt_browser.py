"""Populated replay timeline interaction and responsive layout."""
import pytest

from . import test_ui_theme_browser as theme_browser

browser = theme_browser.browser
page_server = theme_browser.page_server
setup = theme_browser.setup


@pytest.mark.parametrize("theme", ["day", "night"])
def test_replay_gantt_details(browser, page_server, theme, tmp_path):
    with browser.new_context(viewport={"width": 1366, "height": 1000}) as context:
        setup(context, {"ui": theme})
        context.add_init_script("""
          const original = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
            if (endpoint !== 'replay') return original(endpoint, params);
            return {ok:true,data:{sessions:[],speak_count:1,silent_count:6,
              topic_blocks:['speak','manners','media','rhythm','arbiter','proactive'].map((lane,i) => ({
                topic_id:'topic-'+i,topic_title:'聊天主题 '+i,message_count:2,lane,action:i ? 'silent':'speak',start_ts:1700000000+i*60,end_ts:1700000000+i*60,
                count:2,reason_zh:'场景原因 '+i,reason_code:lane,
                events:[{ts:1700000000+i*60,reason_zh:'第一次具体判断 <安全文本>',session_id:'room-a'},
                        {ts:1700000005+i*60,reason_zh:'第二次具体判断',session_id:'room-a'}]
              }))}};
          };
        """)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{page_server}/replay/index.html")
        page.wait_for_function("document.querySelectorAll('.replay-block').length === 6")
        assert page.locator('.gantt-track').count() == 6
        assert len(set(page.locator('.replay-block').evaluate_all(
            "nodes => nodes.map(node => getComputedStyle(node).backgroundColor)"))) == 6
        for width in (1366, 375):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(tmp_path / f'replay-workspace-{theme}-{width}.png'), full_page=True)
        page.set_viewport_size({"width": 1366, "height": 1000})
        page.locator('[data-index="0"]').click()
        assert page.locator('#detailDialog').is_visible()
        assert page.locator('#detailTitle').inner_text() == '聊天主题 0'
        assert page.locator('#blockEvents li').count() == 2
        assert '<安全文本>' in page.locator('#blockEvents').inner_text()
        assert page.locator('[data-index="0"]').get_attribute('aria-pressed') == 'true'
        page.keyboard.press('Escape')
        assert not page.locator('#detailDialog').is_visible()
        page.locator('[data-index="1"]').focus()
        page.keyboard.press('Enter')
        assert page.locator('#detailTitle').inner_text() == '聊天主题 1'
        for width in (1366, 375):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.replay-block').evaluate_all("""nodes => nodes.every(node => {
                const bar = node.getBoundingClientRect();
                const track = node.parentElement.getBoundingClientRect();
                return bar.width >= 84 && bar.height >= 44 && bar.right <= track.right + 1;
            })""")
            page.screenshot(path=str(tmp_path / f'replay-{theme}-{width}.png'))
        page.evaluate("() => {window.AstrBotPluginPage.apiGet = async () => {throw new Error('offline')}}")
        page.locator('#btnCloseDetail').click()
        page.locator('#btnRefresh').click()
        page.wait_for_function("document.querySelector('#blockDetail').textContent.includes('暂时不可用')")
        assert page.locator('#blockEvents li').count() == 0
        assert page.locator('#blockMeta').inner_text() == ''
        assert not errors


def test_replay_unassigned_messages_remain_blank(browser, page_server, tmp_path):
    with browser.new_context(viewport={"width": 390, "height": 900}) as context:
        setup(context, {"ui": "day"})
        context.add_init_script("""
          const original = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
            if (endpoint !== 'replay') return original(endpoint, params);
            return {ok:true,data:{sessions:[],unassigned_message_count:12,
              topic_blocks:[{topic_id:'UNKNOWN',topic_title:'未知话题'},
                            {topic_id:'candidate',topic_status:'pending',topic_title:'未形成'}]}};
          };
        """)
        page = context.new_page()
        page.goto(f"{page_server}/replay/index.html")
        page.wait_for_function("document.querySelector('#unassignedNote').textContent.includes('12')")
        assert page.locator('.replay-block').count() == 0
        assert page.locator('#railEmpty').is_visible()
        assert '留白' in page.locator('#railEmpty').inner_text()
        assert page.locator('#railCounts').inner_text() == '0 个主题场景'
        assert not page.locator('#detailDialog').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / 'replay-blank.png'))


def test_replay_filters_topics_without_network_reload(browser, page_server):
    with browser.new_context(viewport={"width": 1100, "height": 900}) as context:
        setup(context, {"ui": "day"})
        context.add_init_script("""
          const original = window.AstrBotPluginPage.apiGet;
          window.replayReads = 0;
          window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
            if (endpoint !== 'replay') return original(endpoint, params);
            window.replayReads++;
            return {ok:true,data:{sessions:[],topic_blocks:[
              {topic_id:'a',topic_title:'Breakfast',message_count:2,start_ts:1700000000,end_ts:1700000010,events:[{action:'speak'}]},
              {topic_id:'b',topic_title:'Travel',message_count:3,start_ts:1700000010,end_ts:1700000020,events:[{action:'silent'}]}
            ]}};
          };
        """)
        page = context.new_page()
        page.goto(f"{page_server}/replay/index.html")
        page.wait_for_function("document.querySelectorAll('.replay-block').length === 2")
        assert page.locator('#summaryMessages').inner_text() == '5'
        page.locator('#topicSearch').fill('Travel')
        assert page.locator('.replay-block').count() == 1
        page.locator('#decisionFilter').select_option('speak')
        assert page.locator('.replay-block').count() == 0
        assert page.locator('#railEmpty').is_visible()
        page.locator('#topicSearch').fill('')
        assert page.locator('.replay-block').count() == 1
        page.locator('.replay-block').click()
        assert page.locator('#detailTitle').inner_text() == 'Breakfast'
        assert page.evaluate('window.replayReads') == 1


def test_replay_500_message_detail_on_mobile(browser, page_server):
    with browser.new_context(viewport={"width": 390, "height": 900}) as context:
        setup(context, {"ui": "night"})
        context.add_init_script("""
          const original = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
            if (endpoint === 'replay') return {ok:true,data:{sessions:[],message_limit:500,retained_message_count:500,
              topic_blocks:[{session_id:'room',topic_id:'topic',topic_title:'Long discussion',message_count:500,
                start_ts:1700000000,end_ts:1700000500,events:[],messages:Array.from({length:500},(_,i)=>({
                  msg_id:String(i),text:'Message '+i,topic_id:'topic',confidence:1,ambiguous:false
                }))}]}};
            if (endpoint === 'topic_annotations') return {ok:true,data:{records:[],metrics:{total:0,error_counts:{},sample_note:''}}};
            return original(endpoint, params);
          };
        """)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'{page_server}/replay/index.html?ui=night')
        page.wait_for_selector('.replay-block')
        assert page.locator('#summaryMessages').inner_text() == '500'
        assert '当前回看 500 条' in page.locator('#trackHint').inner_text()
        page.locator('.replay-block').click()
        assert page.locator('.annotation-row').count() == 500
        last = page.locator('.annotation-row').last
        last.scroll_into_view_if_needed()
        assert 'Message 499' in last.inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.keyboard.press('Escape')
        assert not page.locator('#detailDialog').is_visible()
        assert not errors
