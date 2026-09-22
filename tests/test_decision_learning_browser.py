"""Exercise learning UI against the real management response envelopes."""
import json

import pytest

from .test_ui_theme_browser import browser as browser, page_server as page_server


def open_learning(context, server, *, fail=None, repeated_cursor=False, stats_override=None):
    calls = []

    def bridge(_source, method, endpoint, body):
        calls.append((method, endpoint, body))
        if endpoint == "ui_preferences":
            return {"ok": True, "data": {"ui": "day"}}
        if endpoint == "page_nav":
            return {"ok": True, "data": {"content_path": "/learning/index.html?asset_token=signed"}}
        if endpoint == fail or fail == "second_export" and body.get("cursor"):
            return {"ok": False, "error": "验收未通过"}
        data = {}
        if endpoint == "learning/stats":
            data = {"mode": "shadow", "model_id": "candidate-1", "p95_ms": 123,
                    "takeover_rate": .25, "teacher_fallback_rate": .1,
                    "tasks": [{"task_id": "join", "samples": 650, "status": "collecting"}],
                    "disagreements": [{"task_id": "join", "teacher": True, "student": False}]}
            data.update(stats_override or {})
        elif endpoint == "learning/jobs/status":
            data = {"jobs": [{"id": "job-123", "state": "running", "progress": {"epoch": 1}}]}
        elif endpoint == "learning/models/compare_teacher":
            data = {"compared": 6, "changed": 2, "needs_human_review": True}
        elif endpoint == "learning/samples/export":
            data = {"records": [{"id": "second" if body.get("cursor") else "first"}],
                    "next_cursor": "1000" if repeated_cursor or not body.get("cursor") else None}
        return {"ok": True, "data": data}

    context.expose_binding("__learningBridge", bridge)
    context.add_init_script("""
      window.__timeouts = [];
      const originalTimeout = window.setTimeout;
      window.setTimeout = (fn, ms, ...args) => {
        window.__timeouts.push(ms); return originalTimeout(fn, ms, ...args);
      };
      window.AstrBotPluginPage = {
        ready: async () => {}, t: (_key, fallback) => fallback,
        apiGet: (endpoint, body = {}) => window.__learningBridge('GET', endpoint, body),
        apiPost: (endpoint, body = {}) => window.__learningBridge('POST', endpoint, body),
      };
    """)
    page = context.new_page()
    page.goto(f"{server}/learning/index.html")
    page.wait_for_selector('[data-cancel="job-123"]')
    return page, calls


def test_stats_jobs_envelope_and_exact_cancel_request(browser, page_server):
    with browser.new_context() as context:
        page, calls = open_learning(context, page_server)
        assert page.locator("#mode").inner_text() == "旁路比较"
        assert page.locator("#model").inner_text() == "candidate-1"
        assert page.locator("#takeover").inner_text() == "25.0%"
        assert "650" in page.locator("#tasks").inner_text()
        assert "教师：true" in page.locator("#disagreements").inner_text()
        page.locator('[data-cancel="job-123"]').click()
        page.wait_for_function("document.getElementById('status').textContent === '操作完成'")
        assert ("POST", "learning/jobs/cancel", {"job_id": "job-123"}) in calls


def test_jev_comparison_has_management_timeout(browser, page_server):
    with browser.new_context() as context:
        page, calls = open_learning(context, page_server)
        page.evaluate("window.__timeouts = []")
        page.locator("#compareJev").click()
        page.wait_for_function("document.getElementById('status').textContent === '操作完成'")
        assert ("POST", "learning/models/compare_jev", {}) in calls
        assert 35000 in page.evaluate("window.__timeouts")


def test_teacher_prompt_comparison_and_hard_label_explanation(browser, page_server):
    with browser.new_context() as context:
        page, calls = open_learning(context, page_server, stats_override={"dataset": {
            "teacher_prompt_versions": {"v1": 32, "v2": 6},
            "quality_flags": {"insufficient_evidence": 4},
        }})
        assert "不是 100% 置信度" in page.locator("#teacherLabelMeaning").inner_text()
        assert "概率需要校准" in page.locator("#teacherLabelMeaning").inner_text()
        assert "证据不足" in page.locator("#qualityFlags").inner_text()
        assert "v1" in page.locator("#teacherPromptVersions").inner_text()
        assert "32" in page.locator("#teacherPromptVersions").inner_text()
        assert "标签改变不等于质量改善" in page.locator("#teacherComparisonMeaning").inner_text()
        page.evaluate("window.__timeouts = []")
        page.locator("#compareTeacher").click()
        page.wait_for_function("document.getElementById('status').textContent === '操作完成'")
        assert ("POST", "learning/models/compare_teacher", {"limit": 6}) in calls
        assert 120000 in page.evaluate("window.__timeouts")
        assert json.loads(page.locator("#result").inner_text()) == {
            "compared": 6, "changed": 2, "needs_human_review": True}


@pytest.mark.parametrize("seed", [None, "2147483648"])
def test_training_job_seed_payload(browser, page_server, seed):
    with browser.new_context() as context:
        page, calls = open_learning(context, page_server)
        page.locator("#trainModel").fill("repeat-001")
        assert page.locator("#seed").input_value() == "42"
        assert "不能用测试集结果挑选" in page.locator("#seedHelp").inner_text()
        if seed is not None:
            page.locator("#seed").fill(seed)
        page.locator('#trainForm button[type="submit"]').click()
        page.wait_for_function("document.getElementById('status').textContent === '操作完成'")
        assert ("POST", "learning/jobs/create", {"model_id": "repeat-001", "epochs": 3,
                                                "seed": int(seed) if seed is not None else 42}) in calls


@pytest.mark.parametrize("seed", ["0", "1.5", "2147483649"])
def test_invalid_training_seed_is_blocked_before_submission(browser, page_server, seed):
    with browser.new_context() as context:
        page, calls = open_learning(context, page_server)
        page.locator("#trainModel").fill("repeat-001")
        page.locator("#seed").fill(seed)
        page.locator('#trainForm button[type="submit"]').click()
        assert not page.locator("#seed").evaluate("node => node.checkValidity()")
        assert not any(endpoint == "learning/jobs/create" for _, endpoint, _ in calls)


def test_collection_gaps_zero_tasks_pairs_and_student_diagnostics(browser, page_server):
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        page, _ = open_learning(context, page_server, stats_override={
            "tasks": [{"task_id": "join", "samples": 650, "status": "pending"},
                      {"task_id": "recipient", "samples": 0, "status": "pending"}],
            "dataset": {"tasks": {
                "join": {"samples": 650, "teacher_labels": 620, "valid_pairs": 42,
                         "class_counts": {"true": 620, "false": 0},
                         "collection_gap": {"samples": 0, "classes": {"true": 0, "false": 50}, "basis": "raw_collection_not_test"}},
                "recipient": {"samples": 0, "teacher_labels": 0, "valid_pairs": 0,
                              "class_counts": {"true": 0, "false": 0},
                              "collection_gap": {"samples": 500, "classes": {"true": 50, "false": 50}, "basis": "raw_collection_not_test"}},
            }},
            "stats": {"student_answered": 42, "student_teacher_finished_first": 5,
                      "student_version_mismatch": 2, "student_http_503": 3},
            "recent": [{"tasks": ["recipient"], "student_status": "http_503", "latency_ms": 30},
                       {"tasks": ["join"], "student_status": "teacher_finished_first", "latency_ms": 20}],
        })
        assert "1 个尚无采集样本" in page.locator("#collectionGaps").inner_text()
        assert "原始采集量不等于独立测试量" in page.locator("#sampleMeaning").inner_text()
        row = page.locator('[data-task-id="join"]')
        assert row.locator("td").nth(3).inner_text() == "42"
        assert row.locator("td").nth(4).inner_text() == "1 / 2"
        assert "false 类缺 50 条" in row.inner_text()
        zero = page.locator('[data-task-id="recipient"]')
        assert zero.locator("td").nth(1).inner_text() == "0"
        assert zero.locator("td").nth(4).inner_text() == "0 / 2"
        assert "教师采集缺 500 条" in zero.inner_text()
        assert "查看模型评估报告" in zero.inner_text()
        assert "版本不匹配" in page.locator("#studentCounters").inner_text()
        assert "student_http_503" in page.locator("#studentCounters").inner_text()
        assert "教师先完成" in page.locator("#studentRecent li").first.inner_text()
        assert "http_503" in page.locator("#studentRecent").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_quality_flags_are_review_hints_and_optional_queue_is_safe(browser, page_server):
    with browser.new_context(viewport={"width": 390, "height": 844}) as context:
        page, _ = open_learning(context, page_server, stats_override={"dataset": {
            "quality_flags": {"needs_review": 7, "future_<flag>": 2, "disagreement": 0, "join_action_conflict": 4},
            "queue_priority": {"high": 3, "normal": 6},
        }})
        assert "需复核不等于教师错误" in page.locator("#qualityMeaning").inner_text()
        assert "需人工复核" in page.locator("#qualityFlags").inner_text()
        assert "参与意愿与动作矛盾" in page.locator("#qualityFlags").inner_text()
        assert "future_<flag>" in page.locator("#qualityFlags").inner_text()
        assert page.locator("#qualityFlags flag").count() == 0
        assert page.locator("#queuePrioritySection").is_visible()
        assert "high" in page.locator("#queuePriority").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_missing_quality_statistics_are_not_reported_as_clean(browser, page_server):
    with browser.new_context() as context:
        page, _ = open_learning(context, page_server)
        assert "尚无统计" in page.locator("#qualityFlags").inner_text()
        assert page.locator("#queuePrioritySection").is_hidden()


@pytest.mark.parametrize("button,endpoint,body", [
    ("#modelForm button[type=submit]", "models/evaluate", {"model_id": "candidate-2"}),
    ("#promote", "models/promote", {"model_id": "candidate-2"}),
    ("#rollback", "models/rollback", {}),
    ("#rollout", "models/rollout", {}),
])
def test_model_operation_error_is_visible_and_controls_recover(browser, page_server, button, endpoint, body):
    with browser.new_context() as context:
        page, calls = open_learning(context, page_server, fail="learning/" + endpoint)
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#candidate").fill("candidate-2")
        page.locator(button).click()
        page.wait_for_function("document.getElementById('status').dataset.error === 'true'")
        assert page.locator("#status").inner_text() == "验收未通过"
        assert not page.locator(button).is_disabled()
        assert ("POST", "learning/" + endpoint, body) in calls


def test_export_download_contains_every_page(browser, page_server, tmp_path):
    with browser.new_context(accept_downloads=True) as context:
        page, calls = open_learning(context, page_server)
        page.locator("#session").fill("platform:group:123")
        with page.expect_download() as pending:
            page.locator('#sampleForm button[type="submit"]').click()
        target = tmp_path / "samples.json"
        pending.value.save_as(target)
        assert json.loads(target.read_text(encoding="utf-8")) == {"records": [{"id": "first"}, {"id": "second"}]}
        exports = [body for _, endpoint, body in calls if endpoint == "learning/samples/export"]
        assert exports == [{"session_key": "platform:group:123", "limit": 1000},
                           {"session_key": "platform:group:123", "limit": 1000, "cursor": "1000"}]


@pytest.mark.parametrize("failure", ["second_export", "repeated_cursor"])
def test_export_failure_never_downloads_partial_data(browser, page_server, failure):
    with browser.new_context(accept_downloads=True) as context:
        page, _ = open_learning(context, page_server, fail=failure, repeated_cursor=failure == "repeated_cursor")
        downloads = []
        page.on("download", lambda download: downloads.append(download))
        page.locator('#sampleForm button[type="submit"]').click()
        page.wait_for_function("document.getElementById('status').dataset.error === 'true'")
        assert not downloads
        assert not page.locator('#sampleForm button[type="submit"]').is_disabled()
