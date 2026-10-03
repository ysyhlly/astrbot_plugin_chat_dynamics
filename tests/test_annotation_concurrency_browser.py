"""The real replay editor submits and retains optimistic concurrency tokens."""
from .test_ui_theme_browser import browser as browser, page_server as page_server
from .test_replay_gantt_browser import live_replay


def annotation_editor(context, absent=False):
    live_replay(context)
    context.add_init_script("window.__annotationAbsent = " + str(absent).lower())
    context.add_init_script("""(() => {
      window.__replayStage = 2;
      window.__annotationPosts = [];
      window.__annotationRows = window.__annotationAbsent ? [] : [
        {msg_id:'m',predicted_topic:'topic',expected_topic:'topic',error_type:'correct',topic_reviewed:true},
        {msg_id:'m2',predicted_topic:'topic',expected_topic:'topic',error_type:'correct',topic_reviewed:true}];
      window.__annotationRevisions = window.__annotationAbsent ? {} : {m:'m:0',m2:'m2:0'};
      const get = window.AstrBotPluginPage.apiGet;
      window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
        if (endpoint === 'topic_annotations') {
          if (window.__holdAnnotations) await new Promise(resolve => window.__releaseAnnotations = resolve);
          return {ok:true,data:{records:structuredClone(window.__annotationRows),
            revisions:structuredClone(window.__annotationRevisions),empty_revision:'empty',
            metrics:{total:window.__annotationRows.length,error_counts:{},sample_note:'人工样本'},
            recipient_metrics:{total:0}}};
        }
        const result = await get(endpoint, params);
        if (endpoint === 'replay') result.data.topic_blocks[0].messages.push(
          {msg_id:'m2',topic_id:'topic',text:'第二条消息',confidence:.8});
        return result;
      };
      window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
        if (endpoint !== 'topic_annotations') return {ok:true,data:{}};
        window.__annotationPosts.push(body);
        const expected = window.__annotationRevisions[body.msg_id] || 'empty';
        if (body.expected_revision !== expected) return {ok:false,error:'标注已被其他页面修改，请刷新后重新确认'};
        const revision = body.msg_id + ':' + window.__annotationPosts.length;
        window.__annotationRevisions[body.msg_id] = revision;
        window.__annotationRows = window.__annotationRows.filter(row => row.msg_id !== body.msg_id);
        window.__annotationRows.push({msg_id:body.msg_id,predicted_topic:'topic',expected_topic:body.expected_topic,
          error_type:body.error_type,topic_reviewed:true});
        return {ok:true,data:{saved:true,revision}};
      };
    })();""")


def open_editor(context, page_server):
    page = context.new_page()
    page.goto(f"{page_server}/replay/index.html")
    page.locator('.replay-block').click()
    page.wait_for_function("document.querySelector('#annotationMetrics').textContent.includes('已标注')")
    return page


def test_loaded_revision_is_submitted(browser, page_server):
    with browser.new_context() as context:
        annotation_editor(context)
        page = open_editor(context, page_server)
        page.locator('[data-target]').nth(0).select_option('NEW')
        page.locator('[data-annotate]').nth(0).click()
        page.wait_for_function("window.__annotationPosts.length === 1")
        assert page.evaluate('window.__annotationPosts[0].expected_revision') == 'm:0'
        page.wait_for_function("document.querySelector('#annotationStatus').textContent.includes('已保存')")


def test_first_annotation_uses_absence_revision(browser, page_server):
    with browser.new_context() as context:
        annotation_editor(context, absent=True)
        page = open_editor(context, page_server)
        page.locator('[data-annotate]').nth(0).click()
        page.wait_for_function("window.__annotationPosts.length === 1")
        assert page.evaluate('window.__annotationPosts[0].expected_revision') == 'empty'


def test_conflict_keeps_unsaved_edits(browser, page_server):
    with browser.new_context() as context:
        annotation_editor(context)
        page = open_editor(context, page_server)
        page.locator('[data-target]').nth(0).select_option('NEW')
        page.evaluate("window.__annotationRevisions.m = 'another-editor'")
        page.locator('[data-annotate]').nth(0).click()
        page.wait_for_function("document.querySelector('#annotationStatus').textContent.includes('修改')")
        assert page.locator('[data-target]').nth(0).input_value() == 'NEW'
        assert page.locator('.annotation-row').nth(0).get_attribute('data-dirty') == 'true'
        assert page.evaluate('window.__annotationRevisions.m') == 'another-editor'


def test_saving_one_row_does_not_refresh_other_editors_token(browser, page_server):
    with browser.new_context() as context:
        annotation_editor(context)
        page = open_editor(context, page_server)
        page.locator('[data-target]').nth(1).select_option('NEW')
        page.evaluate("window.__annotationRevisions.m2 = 'other-editor'")
        page.locator('[data-annotate]').nth(0).click()
        page.wait_for_function("document.querySelector('#annotationStatus').textContent.includes('已保存')")
        page.locator('[data-annotate]').nth(1).click()
        page.wait_for_function("window.__annotationPosts.length === 2")
        assert page.evaluate('window.__annotationPosts[1].expected_revision') == 'm2:0'
        page.wait_for_function("document.querySelector('#annotationStatus').textContent.includes('修改')")
        assert page.locator('[data-target]').nth(1).input_value() == 'NEW'


def test_successful_row_can_be_saved_again_with_new_revision(browser, page_server):
    with browser.new_context() as context:
        annotation_editor(context)
        page = open_editor(context, page_server)
        page.locator('[data-annotate]').nth(0).click()
        page.wait_for_function("document.querySelector('#annotationStatus').textContent.includes('已保存')")
        page.locator('[data-target]').nth(0).select_option('NEW')
        page.locator('[data-annotate]').nth(0).click()
        page.wait_for_function("window.__annotationPosts.length === 2")
        assert page.evaluate('window.__annotationPosts[1].expected_revision') == 'm:1'


def test_topic_only_edit_keeps_hidden_recipient_labels(browser, page_server):
    with browser.new_context() as context:
        annotation_editor(context)
        context.add_init_script("""(() => {
          Object.assign(window.__annotationRows[0], {recipient_ids:[],subject_ids:[],
            hidden_fields:['recipient_ids','subject_ids']});
        })();""")
        page = open_editor(context, page_server)
        assert page.locator('[data-recipient="recipient_ids"]').first.is_disabled()
        assert page.locator('[data-recipient="recipient_ids"]').first.input_value() == ''
        page.locator('[data-target]').first.select_option('NEW')
        page.locator('[data-annotate]').first.click()
        page.wait_for_function('window.__annotationPosts.length === 1')
        posted = page.evaluate('window.__annotationPosts[0]')
        assert 'recipient_ids' not in posted and 'subject_ids' not in posted
        assert 'clear_recipient_fields' not in posted


def test_only_edited_recipient_fields_are_submitted_and_clear_is_explicit(browser, page_server):
    with browser.new_context() as context:
        annotation_editor(context)
        context.add_init_script("""Object.assign(window.__annotationRows[0], {
            recipient_ids:['member'],expected_reply:true,bot_targeted:true});""")
        page = open_editor(context, page_server)
        page.locator('.recipient-editor summary').first.click()
        page.locator('[data-recipient="expected_reply"]').first.select_option('')
        page.locator('[data-recipient="recipient_ids"]').first.fill('alice, bob')
        page.locator('[data-annotate]').first.click()
        page.wait_for_function('window.__annotationPosts.length === 1')
        posted = page.evaluate('window.__annotationPosts[0]')
        assert posted['recipient_ids'] == ['alice', 'bob']
        assert posted['clear_recipient_fields'] == ['expected_reply']
        assert 'bot_targeted' not in posted
        page.wait_for_function("document.querySelector('#annotationStatus').textContent.includes('已保存')")
