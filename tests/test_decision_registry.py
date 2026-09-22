import json
import time

import pytest

from astrbot_plugin_chat_dynamics.core.decision_registry import ModelRegistry
from astrbot_plugin_chat_dynamics.core.decision_evaluation import evaluate_task


def artifact(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    (path / "weights.bin").write_bytes(b"weights")
    metrics = evaluate_task(
        [dict(request_id=str(i), label=i % 2, prediction=i % 2, confidence=1, accepted=True, latency_ms=1) for i in range(500)]
    )
    (path / "evaluation.json").write_text(json.dumps({"tasks": {"reply": metrics}}))
    return path


def test_promotion_checksum_and_rollback(tmp_path):
    registry = ModelRegistry(tmp_path / "models")
    registry.register("v1", artifact(tmp_path, "a"))
    registry.promote("v1")
    registry.register("v2", artifact(tmp_path, "b"))
    assert registry.promote("v2")["previous"]["model_id"] == "v1"
    assert registry.rollback()["model_id"] == "v1"
    with pytest.raises(ValueError):
        registry.register("v1", tmp_path / "a")
    (tmp_path / "models/v1/weights.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        registry.validate("v1")


def test_rollout_requires_each_stage_observation(tmp_path):
    registry = ModelRegistry(tmp_path / "models")
    registry.register("v1", artifact(tmp_path, "a"))
    registry.promote("v1")
    with pytest.raises(ValueError):
        registry.advance_rollout(1000, 999999)
    state = registry.status()
    state["stage_started_at"] = time.time() - 86401
    registry._save(state)
    assert registry.advance_rollout(500)["rollout_percent"] == 50
    state = registry.status()
    state["stage_started_at"] = time.time() - 86401
    registry._save(state)
    with pytest.raises(ValueError):
        registry.advance_rollout(999)
    assert registry.advance_rollout(1000)["rollout_percent"] == 100


def test_cannot_bypass_critical_with_flag_or_fabricated_eligible(tmp_path):
    registry = ModelRegistry(tmp_path / "models")
    metrics = evaluate_task(
        [dict(request_id=str(i), label=i % 2, prediction=i % 2, confidence=1, accepted=True, latency_ms=1) for i in range(500)]
    )
    assert not registry._passed(metrics, "join")
    assert not registry._passed({"eligible": True, "reasons": []}, "action")



def test_target_promotion_requires_set_evidence():
    assert not ModelRegistry._target_sets_passed({"eligible": True})
    from astrbot_plugin_chat_dynamics.core.decision_evaluation import evaluate_target_sets
    rows = [dict(request_id=str(i), task_id="target", question_id="target.0", request_question_count=1,
                 label=i%2, prediction=i%2, accepted=True) for i in range(500)]
    assert ModelRegistry._target_sets_passed(evaluate_target_sets(rows))
