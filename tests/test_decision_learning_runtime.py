"""Teacher/student integration checks at the host boundary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core.decision_learning import DecisionLearning, TeacherAnswers
from astrbot_plugin_chat_dynamics.core.decision_tasks import TASK_VERSION, TEACHER_PROMPT_VERSION, teacher_answers


QUESTIONS = {
    "join": {"type": "noul", "instructions": "Respond?"},
    "action": {"type": "choice", "criteria": {"reply": "Reply", "ignore": "Ignore"}},
}
STUDENT = {
    "join": {"type": "noul", "noul": 0.99},
    "action": {
        "type": "choice",
        "choice": "reply",
        "confidence": 0.99,
        "probabilities": {"reply": 0.99, "ignore": 0.01},
    },
}


def make_runtime(tmp_path, *, mode="active", collect=False):
    cfg = SimpleNamespace(
        decision_learning_mode=mode,
        decision_learning_sessions=["room"] if collect else [],
        decision_timeout=1.0,
        laya_timeout=0.2,
        decision_learning_jev_fallback=False,
        jev_timeout=0.1,
        jev_min_confidence=0.9,
        decision_provider_id="teacher-model",
        decision_learning_sample_rate=1.0,
        decision_learning_retention_days=30,
        decision_learning_labels_per_hour=100,
    )
    metadata = {
        "model_version": "v1",
        "approved_tasks": ["join", "action"],
        "task_versions": {"join": TASK_VERSION, "action": TASK_VERSION},
        "thresholds": {"join:2": 0.05, "action:2": 0.05},
        "rollout_percent": 100,
    }

    async def request(path, payload, **kwargs):
        if path == "/prepare":
            return {
                "state": payload["state"],
                "questions": payload["questions"],
                "model_version": "v1",
                "input_id": "input",
            }
        return {"answers": STUDENT, "metadata": metadata}

    host = SimpleNamespace(
        _runtime_config=cfg,
        _sessions={"room": SimpleNamespace(epoch=0, revision=0)},
        laya=SimpleNamespace(request_json=AsyncMock(side_effect=request)),
        jev=SimpleNamespace(evaluate=AsyncMock(return_value={})),
        llm=SimpleNamespace(resolve_provider_id=AsyncMock(return_value="resolved-provider")),
        _create_background_task=asyncio.create_task,
    )
    runtime = DecisionLearning(host, tmp_path)
    runtime._teacher = AsyncMock(return_value={"join": {"type": "noul", "noul": 0.0}})
    return runtime, host, metadata


def test_all_managed_sessions_still_respect_exclusions(tmp_path):
    runtime, host, _ = make_runtime(tmp_path)
    host._runtime_config.decision_learning_sessions = ("*",)
    host._sessions = {"allowed": SimpleNamespace(group_id="1"), "excluded": SimpleNamespace(group_id="2")}
    host.is_group_takeover_enabled = lambda group: group == "1"
    assert runtime.collecting("allowed")
    assert not runtime.collecting("excluded")
    assert not runtime.collecting("unknown")
    assert runtime.collection_sessions() == ("allowed",)


@pytest.mark.asyncio
async def test_option_bucket_per_task_takeover_and_missing_teacher(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path)
    metadata["thresholds"]["action:2"] = 0.001
    runtime._teacher.return_value = {
        "action": {"type": "choice", "choice": "ignore", "confidence": 1.0, "probabilities": {"ignore": 1.0}}
    }
    result = await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    assert result["join"]["noul"] == 0.99 and result["action"]["choice"] == "ignore"
    assert set(runtime._teacher.call_args.args[2]) == {"action"}
    assert runtime.stats["laya"] == runtime.stats["teacher"] == 1
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "task", "threshold"])
async def test_stale_or_uncalibrated_model_never_takes_over(tmp_path, change):
    runtime, host, metadata = make_runtime(tmp_path)
    if change == "model":
        metadata["model_version"] = "v2"
    elif change == "task":
        metadata["task_versions"] = {}
    else:
        metadata["thresholds"] = {}
    await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    assert runtime.stats["laya"] == 0
    assert runtime._teacher.await_count == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_partial_student_response_falls_back_safely(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path)
    original = host.laya.request_json.side_effect

    async def partial(path, payload, **kwargs):
        response = await original(path, payload, **kwargs)
        if path == "/predict":
            response["answers"] = {"join": STUDENT["join"]}
        return response

    host.laya.request_json.side_effect = partial
    result = await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    assert result["join"]["noul"] == 0.0
    assert runtime.stats["laya"] == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_reset_during_teacher_discards_result(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path, mode="collect")

    async def teacher(*args, **kwargs):
        host._sessions["room"].epoch += 1
        return {"join": {"type": "noul", "noul": 1.0}}

    runtime._teacher.side_effect = teacher
    assert await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS) is None
    assert runtime.stats["stale"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_collection_snapshot_and_queue_keep_provider_provenance(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path, collect=True)
    runtime.start_worker = lambda: None
    await runtime.evaluate(session_id="room", state={"author_id": "alice", "text": "@alice hello"}, questions=QUESTIONS)
    if runtime.tasks:
        await asyncio.gather(*tuple(runtime.tasks))
    prepare = host.laya.request_json.call_args_list[0].args[1]
    assert "alice" not in str(prepare)
    records = runtime.store.samples("room")
    assert len(records) == 2 and all(r["teacher_label"] is None for r in records)
    assert all(r["state"] == prepare["state"] for r in records)
    job = runtime.store.claim_label()
    assert job["payload"]["provider_id"] == "teacher-model"
    assert job["payload"]["question_id"] in QUESTIONS
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_path", ["/prepare", "/predict"])
async def test_student_exception_reserves_teacher_fallback(tmp_path, failed_path):
    runtime, host, metadata = make_runtime(tmp_path)
    original = host.laya.request_json.side_effect

    async def failure(path, payload, **kwargs):
        if path == failed_path:
            raise asyncio.TimeoutError()
        return await original(path, payload, **kwargs)

    host.laya.request_json.side_effect = failure
    result = await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    assert runtime._teacher.await_count == 1
    assert result["join"]["noul"] == 0.0
    await runtime.close()


@pytest.mark.asyncio
async def test_jev_failure_falls_through_and_deadline_bounds_teacher(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path)
    metadata["approved_tasks"] = []
    host._runtime_config.decision_learning_jev_fallback = True
    host.jev.evaluate.side_effect = RuntimeError("unavailable")
    assert (await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS))["join"]["noul"] == 0.0
    assert runtime._teacher.await_count == 1

    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    runtime._teacher.side_effect = slow
    assert (
        await asyncio.wait_for(
            runtime.evaluate(session_id="room", state="context", questions=QUESTIONS, timeout=0.03), 0.2
        )
        is None
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_close_during_teacher_discards_pending_result(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path, mode="collect")

    async def teacher(*args, **kwargs):
        await runtime.close()
        return {"join": {"type": "noul", "noul": 1.0}}

    runtime._teacher.side_effect = teacher
    assert await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS) is None
    assert not runtime.enabled("room")


@pytest.mark.asyncio
async def test_collection_does_not_store_unapproved_session(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path, mode="collect", collect=True)
    runtime.start_worker = lambda: None
    await runtime.evaluate(session_id="other", state="private", questions=QUESTIONS)
    assert runtime.store is None and not runtime.path.exists()
    await runtime.close()


@pytest.mark.asyncio
async def test_real_teacher_parses_hard_labels_with_per_call_model_provenance(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path)
    del runtime._teacher

    async def budget_run(provider, purpose, invoke):
        assert (provider, purpose) == ("provider", "decision_annotation")
        return await invoke()

    host.llm.provider_budget = SimpleNamespace(run=AsyncMock(side_effect=budget_run))
    host.context = SimpleNamespace(
        get_provider_by_id=lambda key: SimpleNamespace(get_model=lambda: "model-revision"),
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"join":true}')),
    )
    result = await runtime._teacher("provider", {"text": "context"}, {"join": QUESTIONS["join"]}, background=True)
    assert result.model == "provider:model-revision"
    assert result["join"]["noul"] == 1.0
    assert result["join"]["label_kind"] == "hard"
    assert result["join"]["confidence_is_calibrated"] is False
    assert json.loads(host.context.llm_generate.call_args.kwargs["prompt"])["state"] == {"text": "context"}
    host.context.llm_generate.return_value = SimpleNamespace(completion_text='{"join":"yes"}')
    assert await runtime._teacher("provider", {}, {"join": QUESTIONS["join"]}, background=True) is None
    await runtime.close()


@pytest.mark.asyncio
async def test_annotation_worker_persists_model_not_provider_alias(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path, collect=True)
    key = await runtime._io("record_sample", "room", "join", "context", QUESTIONS["join"], execution_source="laya")
    await runtime._io("enqueue_label", key, {"provider_id": "teacher-alias", "question_id": "join"})
    runtime._teacher.return_value = TeacherAnswers({"join": {"type": "noul", "noul": 0.0}}, "teacher-alias:revision")
    original_io = runtime._io

    async def tracked(method, *args, **kwargs):
        result = await original_io(method, *args, **kwargs)
        if method == "complete_label":
            host._runtime_config.decision_learning_mode = "off"
        return result

    runtime._io = tracked
    await asyncio.wait_for(runtime._labels(), 1)
    row = runtime.store.samples()[0]
    assert row["teacher_model"] == "teacher-alias:revision"
    assert row["teacher_label"] == {"type": "noul", "noul": 0.0}
    assert row["execution_source"] == "laya"
    assert runtime._teacher.call_args.kwargs["background"] is True
    await runtime.close()


@pytest.mark.asyncio
async def test_export_paging_delete_and_stale_record_cannot_repopulate(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path, collect=True)
    runtime.start_worker = lambda: None
    for room in ["room", "room", "other"]:
        await runtime._io("record_sample", room, "join", "context", QUESTIONS["join"])
    first = await runtime.management("samples/export", {"session_key": "room", "limit": 1})
    second = await runtime.management(
        "samples/export", {"session_key": "room", "limit": 1, "cursor": first["next_cursor"]}
    )
    assert len(first["records"]) == len(second["records"]) == 1
    assert first["records"][0]["id"] != second["records"][0]["id"]
    assert second["next_cursor"] is None
    with pytest.raises(ValueError):
        await runtime.management("samples/export", {"cursor": "../bad"})
    assert (await runtime.management("samples/delete", {"session_key": "room"}))["deleted"] == 2
    await runtime._record("room", "context", QUESTIONS, {}, STUDENT, {}, "teacher", 0.1, {}, collection_epoch=0)
    assert runtime.store.samples("room") == []
    assert len(runtime.store.samples("other")) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_jobs_create_exports_only_teacher_and_ignores_supplied_path(tmp_path, monkeypatch):
    runtime, host, metadata = make_runtime(tmp_path, collect=True)
    directory = tmp_path / "exports"
    monkeypatch.setenv("DECISION_DATASETS_DIR", str(directory))
    monkeypatch.setenv("LAYA_ADMIN_TOKEN", "admin-token")
    await runtime._io(
        "record_sample", "room", "join", "context", QUESTIONS["join"], teacher_label=True, teacher_model="teacher", task_version=TASK_VERSION
    )
    await runtime._io("record_sample", "room", "join", "student only", QUESTIONS["join"])
    host.laya.request_json = AsyncMock(return_value={"job_id": "job"})
    result = await runtime.management("jobs/create", {"model_id": "model", "dataset": "../../outside.jsonl"})
    assert result == {"job_id": "job"}
    call = host.laya.request_json.call_args
    assert call.args[0] == "/admin/jobs"
    filename = call.args[1]["dataset"]
    assert filename.startswith("decisions-") and "/" not in filename and "\\" not in filename
    rows = [json.loads(line) for line in (directory / filename).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["teacher_label"] is True
    assert call.kwargs["token"] == "admin-token"
    await runtime.close()


@pytest.mark.asyncio
async def test_snapshot_uses_active_denominator_and_service_status(tmp_path, monkeypatch):
    runtime, host, metadata = make_runtime(tmp_path)
    monkeypatch.setenv("LAYA_ADMIN_TOKEN", "admin")
    runtime.stats.update(active_questions=4, laya=1, teacher_fallback=2, teacher=20)
    runtime.recent.append({"latency_ms": 25, "model_version": "v1"})
    host.laya.request_json = AsyncMock(return_value={"metadata": {"approved_tasks": ["join"]}, "jobs": [{"id": "job"}]})
    result = await runtime.snapshot()
    assert result["takeover_rate"] == 0.25 and result["teacher_fallback_rate"] == 0.5
    assert result["p95_ms"] == 25 and result["model_id"] == "v1"
    assert next(t for t in result["tasks"] if t["task_id"] == "join")["status"] == "approved"
    assert result["jobs"] == [{"id": "job"}]
    await runtime.snapshot()
    assert host.laya.request_json.await_count == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_outcome_listener_preserves_label_and_terminal_success(tmp_path):
    from astrbot_plugin_chat_dynamics.core.outcome_recorder import mark_delivered, mark_delivery_failed

    runtime, host, metadata = make_runtime(tmp_path, mode="collect", collect=True)
    runtime.start_worker = lambda: None
    node = SimpleNamespace(metadata={})
    await runtime._record(
        "room",
        "context",
        {"join": QUESTIONS["join"]},
        TeacherAnswers({"join": {"type": "noul", "noul": 1.0}}, "teacher:revision"),
        {},
        {"join": "teacher"},
        "teacher",
        0.1,
        {},
        outcome_node=node,
    )
    mark_delivery_failed(node)
    await asyncio.gather(*tuple(runtime.tasks))
    assert runtime.store.samples()[0]["outcome"]["delivered"] is False
    mark_delivered(node)
    await asyncio.gather(*tuple(runtime.tasks))
    mark_delivery_failed(node)
    sample = runtime.store.samples()[0]
    assert sample["outcome"]["final_outcome"] == "delivered"
    assert sample["teacher_label"]["noul"] == 1.0
    await runtime.close()


@pytest.mark.asyncio
async def test_shadow_starts_teacher_before_student_finishes(tmp_path):
    runtime, host, _ = make_runtime(tmp_path, mode="shadow")
    original = host.laya.request_json.side_effect
    teacher_started = asyncio.Event()

    async def request(path, payload, **kwargs):
        if path == "/predict":
            await asyncio.wait_for(teacher_started.wait(), .2)
        return await original(path, payload, **kwargs)

    async def teacher(*args):
        teacher_started.set()
        await asyncio.sleep(.02)
        return {"join": {"type": "noul", "noul": 0.0}}

    host.laya.request_json.side_effect = request
    runtime._teacher.side_effect = teacher
    result = await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    assert result["join"]["noul"] == 0.0
    assert runtime.stats["student_answered"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_shadow_cancels_slow_student_when_teacher_returns(tmp_path):
    runtime, host, _ = make_runtime(tmp_path, mode="shadow")
    original = host.laya.request_json.side_effect
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def request(path, payload, **kwargs):
        if path == "/predict":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original(path, payload, **kwargs)

    async def teacher(*args):
        await started.wait()
        return {"join": {"type": "noul", "noul": 0.0}}

    host.laya.request_json.side_effect = request
    runtime._teacher.side_effect = teacher
    result = await asyncio.wait_for(runtime.evaluate(session_id="room", state="context", questions=QUESTIONS), .3)
    assert result and cancelled.is_set()
    assert runtime.stats["student_teacher_finished_first"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_student_http_diagnostic_is_request_scoped(tmp_path):
    runtime, host, _ = make_runtime(tmp_path)
    original = host.laya.request_json.side_effect

    async def request(path, payload, **kwargs):
        if path == "/predict":
            kwargs["diagnostics"]["status"] = "http_503"
            return None
        return await original(path, payload, **kwargs)

    host.laya.request_json.side_effect = request
    assert await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    assert runtime.stats["student_http_503"] == 1
    assert runtime.recent[-1]["student_status"] == "http_503"
    await runtime.close()


@pytest.mark.asyncio
async def test_targeted_annotation_keeps_whole_request_and_never_uses_student_as_teacher(tmp_path):
    runtime, host, _ = make_runtime(tmp_path, collect=True)
    host._runtime_config.decision_learning_sample_rate = 0
    runtime.start_worker = lambda: None
    await runtime._record("room", {"conversation": {"text": "hello", "explicit": True}},
                          QUESTIONS, {}, STUDENT, {k: "laya" for k in QUESTIONS}, "teacher", .1, {})
    rows = runtime.store.samples("room")
    assert len(rows) == 2 and all(r["teacher_label"] is None for r in rows)
    assert all(r["metadata"]["sampling_reasons"][0] == "explicit_address" for r in rows)
    jobs = [runtime.store.claim_label(session_ids=["room"]) for _ in rows]
    assert all(job["priority"] == 3 for job in jobs)
    assert {job["payload"]["question_id"] for job in jobs} == set(QUESTIONS)
    await runtime.close()


@pytest.mark.asyncio
async def test_annotation_failure_backs_off_without_consuming_whole_budget(tmp_path, monkeypatch):
    runtime, host, _ = make_runtime(tmp_path, collect=True)
    key = await runtime._io("record_sample", "room", "join", "context", QUESTIONS["join"])
    await runtime._io("enqueue_label", key, {"provider_id": "teacher", "question_id": "join"})
    runtime._teacher.side_effect = OSError("do not expose provider details")
    delays = []

    async def sleep(delay):
        delays.append(delay)
        host._runtime_config.decision_learning_mode = "off"

    monkeypatch.setattr(asyncio, "sleep", sleep)
    await runtime._labels()
    assert delays == [1]
    assert runtime.stats["label_failed_OSError"] == 1
    assert len(runtime._label_times) == 1
    assert runtime.store.samples()[0]["teacher_label"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_annotation_missing_provider_resolves_only_original_session(tmp_path):
    runtime, host, _ = make_runtime(tmp_path, collect=True)
    class ProviderNotFoundError(Exception):
        pass
    sid = await runtime._io("record_sample", "room", "join", "context", QUESTIONS["join"])
    await runtime._io("enqueue_label", sid, {"provider_id": "removed", "question_id": "join"})
    host._runtime_config.decision_provider_id = ""
    original_io = runtime._io
    async def tracked(method, *args, **kwargs):
        result = await original_io(method, *args, **kwargs)
        if method == "complete_label":
            host._runtime_config.decision_learning_mode = "off"
        return result
    runtime._io = tracked
    runtime._teacher.side_effect = [ProviderNotFoundError(), TeacherAnswers({"join": {"noul": 1.0}}, "new:revision")]
    await asyncio.wait_for(runtime._labels(), 1)
    host.llm.resolve_provider_id.assert_awaited_once_with("room")
    assert runtime.store.samples()[0]["teacher_model"] == "new:revision"
    assert runtime.stats["label_provider_reresolved"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_null_teacher_label_is_review_not_training_negative(tmp_path):
    runtime, host, _ = make_runtime(tmp_path, collect=True, mode="collect")
    runtime.start_worker = lambda: None
    labels = teacher_answers('{"join":null,"action":"ignore"}', QUESTIONS)
    runtime._teacher.return_value = TeacherAnswers(labels, "teacher:revision")
    await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    if runtime.tasks:
        await asyncio.gather(*tuple(runtime.tasks))
    row = next(row for row in runtime.store.samples() if row["task_id"] == "join")
    assert row["teacher_label"] is None
    assert "insufficient_evidence" in row["metadata"]["quality_flags"]
    assert row["metadata"]["teacher_prompt_version"] == TEACHER_PROMPT_VERSION
    assert runtime.store.claim_label() is None
    await runtime.close()


@pytest.mark.asyncio
async def test_prompt_comparison_reuses_snapshot_and_does_not_overwrite(tmp_path):
    runtime, host, _ = make_runtime(tmp_path, collect=True)
    await runtime._io("record_sample", "room", "join", "same-context", QUESTIONS["join"],
                      teacher_label={"noul":0.0}, teacher_model="old", task_version="1")
    async def teacher(provider, state, questions, **kwargs):
        assert state == "same-context"
        return TeacherAnswers({"join": {"noul": float(kwargs["prompt_version"] != "legacy-v1")}}, "model", kwargs["prompt_version"])
    runtime._teacher.side_effect = teacher
    report = await runtime.management("models/compare_teacher", {"limit": 1})
    assert report["changed"] == 1 and report["human_review_required"]
    assert runtime.store.samples()[0]["teacher_label"] == {"noul":0.0}
    assert report["comparisons"][0]["legacy_model"] == "model"
    await runtime.close()


@pytest.mark.asyncio
async def test_old_task_version_cannot_take_over_new_rubric(tmp_path):
    runtime, host, metadata = make_runtime(tmp_path)
    metadata["task_versions"] = {"join": "1", "action": "1"}
    await runtime.evaluate(session_id="room", state="context", questions=QUESTIONS)
    assert runtime.stats["laya"] == 0
    assert runtime._teacher.await_count == 1
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget, expected", [(2, 2), (100, 3), (0, 0)])
async def test_parallel_annotations_share_budget_and_close_cancels_calls(tmp_path, budget, expected):
    runtime, host, _ = make_runtime(tmp_path, collect=True)
    host._runtime_config.decision_learning_labels_per_hour = budget
    for index in range(6):
        sid = await runtime._io("record_sample", "room", "join", f"context {index}", QUESTIONS["join"])
        await runtime._io("enqueue_label", sid, {"provider_id": "teacher", "question_id": "join"})
    entered = asyncio.Event()
    active = 0
    calls = 0

    async def generate(**kwargs):
        nonlocal active, calls
        active += 1
        calls += 1
        if active == expected:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    # Exercise the real annotation semaphore and shared provider admission too.
    host.context = SimpleNamespace(llm_generate=generate)
    runtime._teacher = DecisionLearning._teacher.__get__(runtime)
    runtime.start_worker()
    worker = runtime.worker
    runtime.start_worker()
    assert runtime.worker is worker
    try:
        if expected:
            await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(.05)
        assert calls == expected
        assert active == expected
        assert len(runtime._label_times) == expected
        assert (await runtime._io("summary"))["queue"].get("leased", 0) == expected
    finally:
        await runtime.close()
    assert active == 0
    assert worker.done()
