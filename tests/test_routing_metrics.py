import importlib.util
import json
from pathlib import Path
import sys

import pytest

from astrbot_plugin_chat_dynamics.core.routing_metrics import routing_metrics


def test_pair_metrics_are_invariant_under_topic_renaming():
    rows = [{"expected_topic": "a", "topic_id": "x"},
            {"expected_topic": "a", "topic_id": "y"},
            {"expected_topic": "b", "topic_id": "x"}]
    result = routing_metrics([rows])
    renamed = [{"expected_topic": {"a": "fruit", "b": "sport"}[r["expected_topic"]],
                "topic_id": {"x": "foo", "y": "bar"}[r["topic_id"]]} for r in rows]
    assert routing_metrics([renamed]) == result
    assert result["topic"]["wrong_merge"] == 1
    assert result["topic"]["fragmentation"] == 1


def test_missing_unknown_and_empty_labels_do_not_score():
    rows = [{"expected_topic": value, "expected_parent": value, "topic_id": "x"}
            for value in ("", "  ", "UNKNOWN", "unknown")]
    result = routing_metrics([rows])
    assert result["topic"]["pairs"] == 0
    assert result["parent"]["accuracy"] is None
    assert result["topic"]["candidate_recall"]["1"]["value"] is None
    assert routing_metrics([[{"expected_parent": None}]])["parent"]["accuracy"] == 1


def test_candidate_recall_requires_observable_prior_truth():
    rows = [{"expected_topic": "a", "topic_id": "x", "topic_candidates": []},
            {"expected_topic": "a", "topic_id": "x", "topic_candidates": [[.9, "y"], [.8, "x"]]},
            {"expected_topic": "a", "topic_id": "x"}]
    metrics = routing_metrics([rows])["topic"]["candidate_recall"]
    assert metrics["1"] == {"hits": 0, "eligible": 1, "value": 0.0}
    assert metrics["3"] == {"hits": 1, "eligible": 1, "value": 1.0}


def test_multiturn_real_router_is_deterministic():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("eval_multiturn", root / "scripts/evaluate_routing.py")
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    cases = json.loads((root / "tests/fixtures/routing_multiturn.json").read_text(encoding="utf-8"))
    result = evaluator.evaluate(cases)
    assert result == evaluator.evaluate(cases)
    assert "benchmark" not in result
    assert result["routing_metrics"]["parent"]["labeled"] == 8
    assert result["failed"] == 0
    assert set(result["recipient_groups"]) == {"explicit", "ambient", "continuation"}


@pytest.mark.parametrize("messages, constraint", [
    ([{"text": "显卡风扇怎么设置？", "expected_topic": "a"},
      {"text": "自动模式", "reply_to": "0", "expected_topic": "b"}], "topic_wrong_merge"),
    ([{"text": "你好", "expected_parent": "missing"}], "parent_exact"),
])
def test_check_rejects_supervised_metric_errors(tmp_path, monkeypatch, capsys, messages, constraint):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("eval_metric_cli", root / "scripts/evaluate_routing.py")
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps([{"id": "bad_truth", "messages": messages}]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["evaluate_routing.py", "--fixtures", str(fixtures), "--check"])
    assert evaluator.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 0
    assert constraint in report["metric_failures"]
