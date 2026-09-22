"""CPU-only contract tests; these do not claim GPU training quality."""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.laya_service.backend import input_digest, prepare_state  # noqa: E402
from services.laya_service.training import fit_temperature, label_index, supervised_loss  # noqa: E402


class CharacterTokenizer:
    def __call__(self, text, **kwargs):
        return {"input_ids": list(map(ord, text))}

    def decode(self, ids, **kwargs):
        return "".join(map(chr, ids))


class FakeBackend:
    version = "test-1"
    device = "cpu"
    calibration = {}

    def prepare(self, state, questions):
        return prepare_state(CharacterTokenizer(), state, questions, 256, 64)

    def predict(self, state, questions):
        return {qid: {"type": "noul", "noul": 0.8, "confidence": 0.8} for qid in questions}


QUESTION = {"join": {"type": "noul", "instructions": "需要回覆嗎？"}}


def test_plugin_turn_questions_accept_structured_instructions(tmp_path):
    from astrbot_plugin_chat_dynamics.core.decision_tasks import turn_questions
    from astrbot_plugin_chat_dynamics.core.jev_decision import build_state
    from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext

    turn = TurnContext(
        session_key="synthetic",
        author="user",
        text="請問如何學習 Python？",
        messages=(MessageSnapshot("m1", "user", "請問如何學習 Python？"),),
        background=(),
        epoch=0,
        revision=0,
        started_at=0,
        explicit=True,
    )
    questions, _ = turn_questions(turn)
    assert isinstance(questions["join"]["instructions"], dict)
    snapshot = prepare_state(CharacterTokenizer(), build_state(turn), questions, 4096, 256)
    assert "請問如何學習 Python" in snapshot
    assert {"join", "action", "state", "length", "reason", "target.0"} == set(questions)
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from services.laya_service.app import create_app

    class TurnBackend(FakeBackend):
        def prepare(self, state, questions):
            return prepare_state(CharacterTokenizer(), state, questions, 4096, 256)

    app = create_app(
        backend=TurnBackend(), state_root=tmp_path / "state", models_root=tmp_path / "models", admin_token="secret"
    )
    with TestClient(app) as client:
        response = client.post("/prepare", json={"state": build_state(turn), "questions": questions})
        assert response.status_code == 200
        assert response.json()["questions"] == questions
        invalid = client.post("/prepare", json={"state": {"text": "x" * 5000}, "questions": questions})
        assert invalid.status_code == 400
        diagnostics = client.get("/admin/status", headers={"Authorization": "Bearer secret"}).json()
        assert diagnostics["prepare_rejections"] == {"critical_context_over_budget": 1}
    with pytest.raises(ValueError, match="invalid typed question"):
        prepare_state(CharacterTokenizer(), "state", {"x": {"type": "noul", "instructions": {}}})


def test_prepare_priority_and_identical_digest():
    state = {"history": "舊" * 600 + "最新話題", "current_message": "請回答這個問題", "mentions": ["bot"]}
    result = prepare_state(CharacterTokenizer(), state, QUESTION, 128, 32)
    assert "請回答這個問題" in result
    assert "最新話題" in result
    assert len(result) <= 88
    assert input_digest(result, QUESTION, "v1") != input_digest(result, QUESTION, "v2")
    with pytest.raises(ValueError, match="critical"):
        prepare_state(CharacterTokenizer(), {"message": "長" * 1000}, QUESTION, 128, 32)
    nested = prepare_state(
        CharacterTokenizer(), {"conversation": {"text": "新問題", "background": "有用背景"}}, QUESTION, 256, 32
    )
    assert "新問題" in nested and "有用背景" in nested


def test_http_contract_and_administrator_boundary(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from services.laya_service.app import create_app

    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "train.jsonl").write_text("{}\n")
    app = create_app(
        backend=FakeBackend(),
        state_root=tmp_path / "state",
        models_root=tmp_path / "models",
        datasets_root=datasets,
        admin_token="secret",
    )
    with TestClient(app) as client:
        assert client.get("/readyz").json()["device"] == "cpu"
        prepared = client.post("/prepare", json={"state": "你好", "questions": QUESTION}).json()
        result = client.post("/predict", json=prepared)
        assert result.status_code == 200
        assert result.json()["metadata"]["approved_tasks"] == []
        assert result.json()["answers"]["join"]["noul"] == 0.8
        assert client.post("/predict", json=dict(prepared, state="修改")).status_code == 409
        assert client.post("/predict", json=dict(prepared, expected_model_version="old")).status_code == 409
        assert client.get("/admin/status").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        assert (
            client.post("/admin/jobs", json={"dataset": "../outside", "model_id": "v1"}, headers=headers).status_code
            == 400
        )
        response = client.post("/admin/jobs", json={"dataset": "train.jsonl", "model_id": "v1"}, headers=headers)
        assert response.status_code == 200
        job_id = response.json()["id"]
        assert client.post(f"/admin/jobs/{job_id}/cancel", headers=headers).json()["cancel_requested"]
        assert client.get("/admin/jobs", headers=headers).json()["jobs"][0]["id"] == job_id


def test_real_supervised_losses_backpropagate():
    torch = pytest.importorskip("torch")
    for kind, label, values in [
        ("choice", 1, [0.0, 1.0, 0.0]),
        ("noul", 1, [0.0, 1.0]),
        ("score", 2.3, [0.0, 0.0, 1.0, 0.0, 0.0]),
    ]:
        logits = torch.tensor(values, requires_grad=True)
        loss = supervised_loss(logits, kind, label)
        assert torch.isfinite(loss)
        loss.backward()
        assert logits.grad.abs().sum() > 0
    with pytest.raises(ValueError, match="hard binary"):
        label_index({"candidates": {"type": "noul"}, "teacher_label": {"noul": 0.9}})


@pytest.mark.parametrize(
    "kind,teacher,student,criteria",
    [
        ("noul", 1, 0, {}),
        ("choice", "yes", "no", ["yes", "no"]),
        ("score", 4, 0, ["0", "1", "2", "3", "4"]),
    ],
)
def test_rollout_rejects_wrong_accepted_decisions_despite_time_and_volume(tmp_path, kind, teacher, student, criteria):
    import json
    import time

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from services.laya_service.app import create_app

    datasets = tmp_path / "datasets"
    datasets.mkdir()
    registry = tmp_path / "models" / "registry"
    registry.mkdir(parents=True)
    stage = time.time() - 90000
    active = dict(model_id="test-1", tasks=["join"], rollout_percent=10, stage_started_at=stage, comparison_baseline=0)
    (registry / "active.json").write_text(json.dumps(active))
    rows = [
        dict(
            session_id="s",
            task_id="join",
            created_at=stage + 1,
            teacher_model="teacher",
            teacher_label={kind: teacher},
            student_prediction={kind: student},
            execution_source="laya",
            candidates={"type": kind, "criteria": criteria},
            outcome={"model_version": "test-1"},
            metadata={"request_id": str(i), "question_id": "join"},
        )
        for i in range(500)
    ]
    dataset = datasets / "comparisons.jsonl"
    dataset.write_text("\n".join(map(json.dumps, rows)))
    app = create_app(
        backend=FakeBackend(),
        state_root=tmp_path / "state",
        models_root=tmp_path / "models",
        datasets_root=datasets,
        admin_token="secret",
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer secret"}
        response = client.post("/admin/rollout", json={"dataset": dataset.name}, headers=headers)
        assert response.status_code == 400
        assert "error exceeds 5%" in response.json()["detail"]
        assert json.loads((registry / "active.json").read_text())["rollout_percent"] == 10
        for row in rows:
            row["student_prediction"] = {kind: teacher}
        dataset.write_text("\n".join(map(json.dumps, rows)))
        assert (
            client.post("/admin/rollout", json={"dataset": dataset.name}, headers=headers).json()["rollout_percent"]
            == 50
        )


def test_temperature_reduces_overconfidence():
    pytest.importorskip("numpy")
    temperature = fit_temperature([[10, 0], [10, 0], [10, 0], [10, 0]], [0, 0, 1, 1])
    assert temperature > 1


def test_validation_is_carved_only_from_training_groups():
    from services.laya_service.jobs import core_module, training_partitions

    rows = [
        dict(
            id=str(i),
            session_id=str(i),
            created_at=i,
            state=f"independent {i * 918271} topic {i * 79283}",
            task_id="join",
            teacher_label={"noul": 0},
            candidates=QUESTION["join"],
        )
        for i in range(20)
    ]
    original = core_module("dataset").split_samples(rows)
    partitions = training_partitions(rows)

    def ids(samples):
        return {row["id"] for row in samples}

    assert ids(partitions["calibration"]) == ids(original["calibration"])
    assert ids(partitions["test"]) == ids(original["test"])
    assert ids(partitions["train"]) | ids(partitions["validation"]) == ids(original["train"])
    assert not ids(partitions["train"]) & ids(partitions["validation"])
    assert max(row["created_at"] for row in partitions["train"]) < min(
        row["created_at"] for row in partitions["validation"]
    )


def test_task_versions_are_checked_before_partitioning_or_training(tmp_path, monkeypatch):
    import json
    from services.laya_service import jobs

    rows = [
        dict(task_id="target.0", task_version="1", teacher_model="t", teacher_label={"noul": 0}),
        dict(task_id="target.1", task_version="2", teacher_model="t", teacher_label={"noul": 1}),
    ]
    dataset = tmp_path / "mixed.jsonl"
    dataset.write_text("\n".join(map(json.dumps, rows)))

    def forbidden(*args, **kwargs):
        raise AssertionError("partitioning or GPU loading occurred before contract validation")

    monkeypatch.setattr(jobs, "training_partitions", forbidden)
    monkeypatch.setattr(jobs, "train", forbidden)
    with pytest.raises(ValueError, match="mixed task versions for target"):
        jobs.run_job({"dataset": dataset.name}, None, tmp_path / "models", tmp_path)


def test_teacher_prompt_provenance_counts_and_legacy_defaults():
    from services.laya_service.jobs import dataset_provenance

    result = dataset_provenance(
        [
            {"task_id": "join"},
            {"task_id": "join", "task_version": 1, "metadata": {"teacher_prompt_version": "zh-rubric-v2"}},
            {"task_id": "action", "task_version": "2", "metadata": {"teacher_prompt_version": "zh-rubric-v2"}},
        ]
    )
    assert result["task_versions"] == {"join": "1", "action": "2"}
    assert result["teacher_prompt_versions"] == {"legacy-v1": 1, "zh-rubric-v2": 2}


def test_balanced_sampling_caps_repetition_per_epoch():
    from collections import Counter
    from services.laya_service.training import balanced_epoch_indices

    rows = [dict(task_id="join", teacher_label={"noul": int(i == 20)}, candidates=QUESTION["join"]) for i in range(21)]
    indices = balanced_epoch_indices(rows, seed=9, max_repeats=3)
    assert len(indices) == len(rows)
    assert max(Counter(indices).values()) <= 3
    assert indices.count(20) == 3
    assert indices == balanced_epoch_indices(rows, seed=9, max_repeats=3)


def test_validation_early_stopping_remembers_best_epoch():
    from services.laya_service.training import EarlyStopping

    tracker = EarlyStopping(patience=2)
    assert tracker.observe(0.6, 1) == (True, False)
    assert tracker.observe(0.3, 2) == (True, False)
    assert tracker.observe(0.4, 3) == (False, False)
    assert tracker.observe(0.5, 4) == (False, True)
    assert tracker.best_epoch == 2 and tracker.best_loss == 0.3
    tiny = EarlyStopping(patience=1)
    tiny.observe(0.3, 1)
    assert tiny.observe(0.29999, 2) == (True, True)
    assert tiny.best_epoch == 2  # best checkpoint is truly lowest, even below min_delta


def test_calibration_requires_independent_requests_and_wilson_bound():
    from services.laya_service.training import grouped_threshold

    repeated = grouped_threshold([0.99] * 100, [True] * 100, ["one"] * 100)
    assert repeated["effective_groups"] == 1 and repeated["threshold"] > 1
    twenty = grouped_threshold([0.99] * 20, [True] * 20, list(range(20)))
    assert twenty["threshold"] > 1  # zero observed errors is not enough statistical evidence
    unknown = grouped_threshold([0.99] * 100, [True] * 100, [None] * 100)
    assert unknown["effective_groups"] == 0 and unknown["threshold"] > 1
    enough = grouped_threshold([0.99] * 100, [True] * 100, list(range(100)))
    assert enough["threshold"] == 0.99 and enough["accepted_error_upper"] <= 0.05
    one_wrong_candidate = grouped_threshold([0.99] * 200, [True, False] * 100, [i // 2 for i in range(200)])
    assert one_wrong_candidate["threshold"] > 1


def test_evaluation_includes_full_target_sets():
    from services.laya_service.jobs import evaluate

    class TargetBackend:
        calibration = {"target:2": {"threshold": 0.9}, "action:2": {"threshold": 0.9}}

        def predict(self, state, questions):
            return {
                "action": {"type": "choice", "choice": "reply", "confidence": 0.99},
                "target.0": {"type": "noul", "noul": 0.99, "confidence": 0.99},
                "target.1": {"type": "noul", "noul": 0.01, "confidence": 0.99},
            }

    rows = []
    for qid, task, q, label in [
        ("action", "action", {"type": "choice", "criteria": ["ignore", "reply"]}, {"choice": "reply"}),
        ("target.0", "target", {"type": "noul"}, {"noul": 1}),
        ("target.1", "target", {"type": "noul"}, {"noul": 0}),
    ]:
        rows.append(
            dict(
                id=qid,
                session_id="s",
                task_id=task,
                state="x",
                candidates=q,
                teacher_label=label,
                metadata={"request_id": "r", "question_id": qid, "request_question_count": 3},
            )
        )
    report = evaluate(TargetBackend(), rows)
    assert report["target_sets"]["count"] == 1
    assert report["target_sets"]["exact_match"] == 1
    assert report["target_sets"]["population"] == "complete_requests_only"
    assert report["tasks"]["target"]["accepted_request_count"] == 1


def test_jobs_survive_reopen_and_cancel(tmp_path):
    from services.laya_service.jobs import Jobs

    jobs = Jobs(tmp_path)
    created = jobs.create("export.jsonl", "model-1")
    fresh = Jobs(tmp_path)
    assert fresh.get(created["id"])["state"] == "queued"
    fresh.cancel(created["id"])
    assert jobs.cancelled(created["id"])
    with pytest.raises(ValueError):
        jobs.get("../escape")


def test_real_http_benchmark_contract():
    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    from services.laya_service.jobs import http_predictor

    with http_predictor(FakeBackend()) as predict:
        assert predict("繁體中文訊息", QUESTION)["join"]["noul"] == 0.8


def test_evaluation_never_counts_partial_request_as_full_latency():
    from services.laya_service.jobs import evaluate

    row = {
        "id": "sample",
        "session_id": "room",
        "state": "hello",
        "task_id": "join",
        "candidates": QUESTION["join"],
        "teacher_label": {"noul": 1},
        "metadata": {"request_id": "request", "request_question_count": 2, "question_id": "join"},
    }
    report = evaluate(FakeBackend(), [row])
    assert report["incomplete_requests"] == 1
    assert report["tasks"] == {}


def test_pinned_upstream_scorer_training_contract():
    torch = pytest.importorskip("torch")
    pytest.importorskip("laya")
    from types import SimpleNamespace
    from laya.common import DecisionModel, collate_items

    class TinyEncoder(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.config = SimpleNamespace(hidden_size=8)
            self.embedding = torch.nn.Embedding(32, 8)

        def forward(self, input_ids, attention_mask):
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))

    model = DecisionModel(TinyEncoder(), head_layers=0)
    items = [dict(ids=[1, 2, 3, 4], markers=[1, 2], qtype=2)]
    batch = collate_items([items], 0)
    logits, action = model(*(batch[k] for k in ("input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype")))
    assert logits.shape == (1, 2)
    supervised_loss(logits[0], "noul", 1).backward()
    assert model.encoder.embedding.weight.grad.abs().sum() > 0
    assert model.act_head[0].weight.grad is None


def test_real_multilingual_checkpoint_when_provided():
    import os

    checkpoint = os.environ.get("LAYA_TEST_CHECKPOINT")
    if not checkpoint:
        pytest.skip("set LAYA_TEST_CHECKPOINT for actual multilingual weight verification")
    from services.laya_service.backend import LayaBackend

    backend = LayaBackend(checkpoint, device="cpu")
    questions = dict(
        QUESTION,
        tone={"type": "choice", "instructions": "情緒？", "criteria": ["平靜", "憤怒"]},
        urgency={"type": "score", "instructions": "緊急程度？", "criteria": ["低", "中", "高"]},
    )
    prepared = backend.prepare({"text": "我想了解這個功能，請幫忙說明。"}, questions)
    answers = backend.predict(prepared, questions)
    assert set(answers) == set(questions)
    assert 0 <= answers["join"]["noul"] <= 1
    assert answers["tone"]["choice"] in questions["tone"]["criteria"]
    assert 0 <= answers["urgency"]["score"] <= 2
