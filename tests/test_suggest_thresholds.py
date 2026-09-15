"""Offline suggestions must fail closed when exports cannot support replay."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.suggest_thresholds import records_from, suggest


def cohort():
    return [dict(session_hash=f"session-{session}", msg_id=str(i),
                 label_source="human", expected_reply=i % 2 == 0,
                 decision_trace={"mode": "legacy", "weights_version": "v1",
                     "shadow": {"reason": "ambient", "score": .65 if i % 2 == 0 else .4,
                                "baseline_threshold": .7, "baseline_reply": False}})
            for session in range(4) for i in range(30)]


def test_suggestion_uses_held_out_sessions_and_reports_counts():
    result = suggest(cohort())
    assert result["suggestions"][0]["value"] == .65
    assert result["eligible_samples"] == 120
    assert result["split"] == {"train": 60, "validation": 60}
    assert result["validation"]["candidate"]["fn"] == 0


def test_duplicates_do_not_inflate_support_and_conflicts_excluded():
    rows = cohort()[:20]
    assert suggest(rows * 10)["suggestions"] == []
    conflicting = copy.deepcopy(rows[0])
    conflicting["expected_reply"] = False
    result = suggest(rows + [conflicting])
    assert result["eligible_samples"] == 19
    assert result["excluded"]["conflicting_identity"] == 1


def test_missing_labels_structural_and_persona_are_not_calibrated():
    for change in (lambda r: r.pop("label_source"),
                   lambda r: r["decision_trace"].update(mode="persona"),
                   lambda r: r["decision_trace"]["shadow"].update(reason="structural"),
                   lambda r: r["decision_trace"]["shadow"].update(score=float("nan")),
                   lambda r: r["decision_trace"]["shadow"].update(score=10 ** 1000),
                   lambda r: r["decision_trace"]["shadow"].update(baseline_threshold=10 ** 1000),
                   lambda r: r["decision_trace"]["shadow"].update(baseline_reply=True)):
        rows = cohort()
        for row in rows:
            change(row)
        assert suggest(rows)["eligible_samples"] == 0


def test_mixed_cohorts_and_single_session_abstain():
    rows = cohort()
    rows[0]["decision_trace"]["weights_version"] = "v2"
    assert suggest(rows)["reason"].startswith("mixed_weights")
    rows = cohort()
    for i, row in enumerate(rows):
        row.update(session_hash="one", msg_id=str(i))
    assert suggest(rows)["reason"].startswith("insufficient_sessions")


def test_one_class_and_no_gain_abstain():
    rows = cohort()
    for row in rows:
        row["expected_reply"] = True
    assert suggest(rows)["suggestions"] == []
    rows = cohort()
    for row in rows:
        if row["expected_reply"]:
            row["decision_trace"]["shadow"].update(score=.8, baseline_reply=True)
    assert suggest(rows)["reason"].startswith("no_validated_improvement")


def test_cli_reads_api_and_export_formats_without_mutating_files(tmp_path):
    path = tmp_path / "annotations.json"
    payload = {"data": {"records": cohort()}}
    original = json.dumps(payload).encode()
    path.write_bytes(original)
    script = Path(__file__).resolve().parents[1] / "scripts" / "suggest_thresholds.py"
    result = subprocess.run([sys.executable, str(script), str(path)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["suggestions"]
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    assert records_from({"records": []}) == []
    assert records_from([]) == []


@pytest.mark.asyncio
async def test_real_shadow_trace_and_saved_annotation_contract():
    from astrbot_plugin_chat_dynamics.core.graph import ConversationNode
    from astrbot_plugin_chat_dynamics.core.learning_policy import shadow_decision
    from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace
    from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations

    kv = {}

    async def get(key, default):
        return kv.get(key, default)

    async def put(key, value):
        kv[key] = value

    plugin = SimpleNamespace(get_kv_data=get, put_kv_data=put, dags={},
                             console_show_message_content=False)
    annotations = TopicAnnotations(plugin)
    exported = []
    for session in range(4):
        session_key = f"session-{session}"
        nodes = {}
        plugin.dags[session_key] = SimpleNamespace(nodes=nodes)
        for i in range(30):
            mid = str(i)
            score = .65 if i % 2 == 0 else .4
            shadow = shadow_decision(score=score, level="hover", evidence_codes=[],
                                     has_prior_bot=True, baseline_threshold=.7,
                                     params={"strong_addressivity_threshold": .65},
                                     policy_id="test-policy")
            routing = {"topic_id": "t"}
            trace = build_routing_trace(routing=routing, mode="legacy",
                                        weights_version="v1", shadow=shadow)
            nodes[mid] = ConversationNode(mid, "user", "body", i,
                                           metadata={"routing": routing,
                                                     "decision_trace": trace})
            await annotations.save({"session_key": session_key, "msg_id": mid,
                                    "expected_topic": "CORRECT", "error_type": "correct",
                                    "expected_reply": i % 2 == 0})
        exported.extend(records_from(await annotations.read(session_key)))
    result = suggest(exported)
    assert result["eligible_samples"] == 120
    assert result["suggestions"][0]["value"] == .65
