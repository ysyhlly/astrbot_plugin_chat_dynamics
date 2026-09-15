"""Browser behavior for the AI label review page (drafts)."""

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from .test_console_redesign_verification import _launch_browser, sync_playwright

ROOT = Path(__file__).resolve().parents[1]

PAYLOAD = {
    "content_hidden": True,
    "total_drafts": 3,
    "sessions": [
        {
            "session_key": "aiocqhttp:GroupMessage:10001",
            "generated_at": 1730000000.0,
            "provider_id": "provider-draft",
            "items": [
                {
                    "msg_id": "m1",
                    "expected_reply": True,
                    "bot_targeted": False,
                    "confidence": 0.8,
                    "reason": "直接提问",
                    "annotated": False,
                    "saveable": True,
                    "text": "",
                    "topic_id": "t1",
                    "ts": 1730000000.0,
                },
                {
                    "msg_id": "m2",
                    "expected_reply": False,
                    "bot_targeted": True,
                    "confidence": 0.55,
                    "reason": "在跟别人说话",
                    "annotated": False,
                    "saveable": False,
                    "stale_reason": "evicted",
                    "text": "",
                    "topic_id": "t1",
                    "ts": 1730000060.0,
                },
            ],
        },
        {
            "session_key": "aiocqhttp:GroupMessage:10002",
            "generated_at": 1730000100.0,
            "provider_id": "provider-draft",
            "items": [
                {
                    "msg_id": "m3",
                    "expected_reply": True,
                    "bot_targeted": True,
                    "confidence": 0.9,
                    "reason": "点名",
                    "annotated": True,
                    "saveable": True,
                    "text": "",
                    "topic_id": "t2",
                    "ts": 1730000100.0,
                },
            ],
        },
    ],
}


@pytest.fixture(scope="module")
def page_server():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "pages"), **kwargs)

        def log_message(self, *_args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        yield browser
        browser.close()


def setup(context, payload=PAYLOAD):
    context.add_init_script(
        "window.__calls = []; window.__payload = " + json.dumps(payload, ensure_ascii=False) + ";"
        """
      window.AstrBotPluginPage = {
        ready: async () => {},
        t: (_key, fallback) => fallback,
        apiGet: async (endpoint, params = {}) => {
          if (endpoint === 'annotation_drafts') return {ok: true, data: window.__payload};
          if (endpoint === 'ui_preferences') return {ok: true, data: {ui: 'day'}};
          if (endpoint === 'page_nav') return {ok: true, data: {content_path: '/' + params.page + '/index.html?asset_token=fresh'}};
          return {ok: true, data: {}};
        },
        apiPost: async (endpoint, body) => {
          window.__calls.push({endpoint: endpoint, body: body});
          const ids = body.msg_ids || [];
          return {ok: true, data: body.action === 'dismiss' ? {removed: ids.length} : {saved: ids.length, failed: []}};
        }
      };
    """
    )


def open_page(context, page_server, payload=PAYLOAD, dialogs=None):
    setup(context, payload)
    page = context.new_page()
    # Batch writes are guarded by a confirmation dialog. Accept it, and record
    # the copy so a test can assert the warning actually appears.
    seen = dialogs if dialogs is not None else []
    page.on("dialog", lambda dialog: (seen.append(dialog.message), dialog.accept()))
    page.goto(f"{page_server}/drafts/index.html?ui=day")
    page.wait_for_selector(".draft-card")
    return page


def test_the_review_page_lists_every_pending_draft_with_its_verdict(browser, page_server):
    with browser.new_context() as context:
        page = open_page(context, page_server)

        assert page.locator(".draft-card").count() == 3
        assert page.locator(".draft-session-head").count() == 2
        assert "待审 3 条" in page.locator("#summaryLine").inner_text()
        text = page.locator(".draft-list").inner_text()
        assert "该回复：是" in text and "对 Bot 说话：否" in text and "置信度 0.8" in text
        assert "理由：直接提问" in text
        assert "已失效：消息已超出保留窗口" in text
        assert "以下 1 条已失效" in text
        assert page.locator(".draft-card.is-stale").count() == 1
        assert "可采纳 2 条" in page.locator("#draftCount").inner_text()
        # An expired draft offers dismiss only.
        assert page.locator(".draft-card.is-stale button[data-accept]").is_disabled()
        assert page.locator('.draft-card:not(.is-stale) button[data-accept]').first.is_enabled()
        # Text stays hidden by default; the page says so instead of showing blanks.
        assert "脱敏" in page.locator("#hiddenNote").inner_text()
        assert page.locator("#listEmpty").is_hidden()


def test_a_filter_that_vanishes_falls_back_to_every_session(browser, page_server):
    """筛选的会话消失后，下拉显示“全部会话”，列表也必须真的回到全部。"""
    with browser.new_context() as context:
        page = open_page(context, page_server)
        page.select_option("#sessionFilter", "aiocqhttp:GroupMessage:10001")
        assert page.locator(".draft-card").count() == 2

        # That session's drafts are gone now (accepted or dismissed elsewhere).
        remaining = json.loads(json.dumps(PAYLOAD))
        remaining["sessions"] = [group for group in remaining["sessions"]
                                if group["session_key"] != "aiocqhttp:GroupMessage:10001"]
        remaining["total_drafts"] = sum(len(group["items"])
                                        for group in remaining["sessions"])
        page.evaluate("payload => { window.__payload = payload; }", remaining)
        page.locator("#btnRefresh").click()
        page.wait_for_function("document.querySelector('#sessionFilter').value === ''")

        assert page.locator("#sessionFilter").input_value() == ""
        assert page.locator(".draft-card").count() == 1, "列表空着，下拉却说全部会话"
        assert page.locator("#listEmpty").is_hidden()


def test_a_failed_load_says_so_instead_of_looking_empty(browser, page_server):
    """An error is not an empty list."""

    with browser.new_context() as context:
        setup(context)
        page = context.new_page()
        page.add_init_script("""
          const original = window.AstrBotPluginPage.apiGet;
          window.AstrBotPluginPage.apiGet = async (endpoint, params) => {
            if (endpoint === 'annotation_drafts') throw new Error('annotation drafts unavailable');
            return original(endpoint, params);
          };
        """)
        page.goto(f"{page_server}/drafts/index.html?ui=day")
        page.wait_for_function("document.querySelector('#listEmpty')?.classList.contains('is-error')")
        assert "is-error" in (page.locator("#listEmpty").get_attribute("class") or "")
        assert "暂时读不到" in page.locator("#listEmpty").inner_text()
        assert page.locator("#listHost").inner_text().strip() == ""
        assert "草稿列表暂时读不到" in page.locator("#reviewStatus").inner_text()


def test_text_is_shown_when_the_content_switch_is_on(browser, page_server):
    shown = json.loads(json.dumps(PAYLOAD))
    shown["content_hidden"] = False
    shown["sessions"][0]["items"][0]["text"] = "在吗"
    with browser.new_context() as context:
        page = open_page(context, page_server, shown)

        assert "在吗" in page.locator(".draft-card").first.inner_text()
        assert page.locator("#hiddenNote").inner_text() == ""


def test_batch_accept_sends_one_request_per_session_with_the_topic_choice(browser, page_server):
    with browser.new_context() as context:
        dialogs = []
        page = open_page(context, page_server, dialogs=dialogs)

        page.locator("#btnSelectAll").click()
        page.locator("#btnAccept").click()
        page.wait_for_function("window.__calls.length >= 2")

        calls = page.evaluate("window.__calls")
        assert {call["body"]["session_key"] for call in calls} == {
            "aiocqhttp:GroupMessage:10001",
            "aiocqhttp:GroupMessage:10002",
        }
        assert all(call["body"]["action"] == "accept" for call in calls)
        assert all(call["body"]["expected_topic"] == "CORRECT" for call in calls)
        # m2 is expired: it must never be sent as an accept, or the batch turns
        # into a wall of identical 'message no longer available' failures.
        assert sorted(sum((call["body"]["msg_ids"] for call in calls), [])) == ["m1", "m3"]
        status = page.locator("#reviewStatus").inner_text()
        assert "已采纳 2 条" in status and "1 条已失效" in status
        # m3 already carries a human label, so accepting overwrites it and must
        # have asked first.
        assert dialogs and "覆盖" in dialogs[-1], dialogs

        page.evaluate("window.__calls = []")
        page.locator("#acceptTopic").select_option("NEW")
        page.locator("#btnSelectAll").click()
        page.locator("#btnAccept").click()
        page.wait_for_function("window.__calls.length >= 2")
        assert all(call["body"]["expected_topic"] == "NEW"
                   for call in page.evaluate("window.__calls"))


def test_dismissing_selected_drafts_asks_before_dropping_them(browser, page_server):
    """Dismissing throws drafts away for good, so it must confirm first."""

    with browser.new_context() as context:
        dialogs = []
        page = open_page(context, page_server, dialogs=dialogs)

        page.locator("#btnSelectAll").click()
        page.locator("#btnDismiss").click()
        page.wait_for_function("window.__calls.length >= 1")
        assert dialogs and "无法恢复" in dialogs[-1], dialogs


def test_accepting_only_expired_drafts_explains_instead_of_failing(browser, page_server):
    expired_only = {
        "content_hidden": True,
        "total_drafts": 2,
        "sessions": [{
            "session_key": "aiocqhttp:GroupMessage:10001",
            "generated_at": 1730000000.0,
            "provider_id": "provider-draft",
            "items": [
                {
                    "msg_id": "m1",
                    "expected_reply": True,
                    "bot_targeted": False,
                    "confidence": 0.8,
                    "reason": "直接提问",
                    "annotated": False,
                    "saveable": False,
                    "text": "",
                    "topic_id": "t1",
                    "ts": 1730000000.0,
                },
            ],
        }],
    }
    with browser.new_context() as context:
        page = open_page(context, page_server, expired_only)

        page.locator("#btnSelectAll").click()
        page.locator("#btnAccept").click()
        page.wait_for_function("document.querySelector('#reviewStatus').textContent.includes('都已失效')")

        assert page.evaluate("window.__calls") == [], "失效草稿不该发请求"
        status = page.locator("#reviewStatus").inner_text()
        assert "无法采纳" in status and "忽略选中" in status

        # Dismissing them still works, so a restart cannot leave dead weight.
        page.locator("#btnDismiss").click()
        page.wait_for_function("window.__calls.length >= 1")
        assert page.evaluate("window.__calls[0].body")["action"] == "dismiss"


def test_a_single_card_can_be_dismissed_and_a_session_cleared(browser, page_server):
    with browser.new_context() as context:
        dialogs = []
        page = open_page(context, page_server, dialogs=dialogs)

        page.locator(".draft-card button[data-dismiss]").first.click()
        page.wait_for_function("window.__calls.length >= 1")
        dismissed = page.evaluate("window.__calls[0]")
        assert dismissed["endpoint"] == "annotation_drafts"
        assert dismissed["body"] == {
            "action": "dismiss",
            "session_key": "aiocqhttp:GroupMessage:10001",
            "msg_ids": ["m1"],
        }
        assert "已忽略 1 条" in page.locator("#reviewStatus").inner_text()

        page.evaluate("window.__calls = []")
        page.locator("[data-clear-session]").first.click()
        page.wait_for_function("window.__calls.length >= 1")
        cleared = page.evaluate("window.__calls[0]")
        assert cleared["body"] == {
            "action": "clear_session",
            "session_key": "aiocqhttp:GroupMessage:10001",
        }
        # Clearing a whole session's drafts asks first.
        assert dialogs and "无法恢复" in dialogs[-1], dialogs


def test_the_side_nav_links_every_page_and_marks_review_current(browser, page_server):
    with browser.new_context() as context:
        page = open_page(context, page_server)

        assert page.locator(".plugin-side-nav .plugin-side-item").count() == 7
        current = page.locator(".plugin-side-item.is-current")
        assert current.count() == 1 and "AI 标注审批" in current.inner_text()
        assert page.locator('[data-nav-page="replay"]').count() == 1


def test_an_empty_backlog_says_so(browser, page_server):
    with browser.new_context() as context:
        setup(context, {"content_hidden": True, "total_drafts": 0, "sessions": []})
        page = context.new_page()

        setup(context, {"content_hidden": True, "total_drafts": 0, "sessions": []})
        page.goto(f"{page_server}/drafts/index.html?ui=day")
        page.wait_for_function("document.querySelector('#listEmpty') && !document.querySelector('#listEmpty').classList.contains('hidden')")

        assert page.locator(".draft-card").count() == 0
        assert "生成 AI 草稿" in page.locator("#listEmpty").inner_text()
