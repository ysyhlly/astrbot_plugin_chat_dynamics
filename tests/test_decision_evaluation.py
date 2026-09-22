from astrbot_plugin_chat_dynamics.core.decision_evaluation import evaluate_task


def perfect():
    return [
        dict(
            request_id=str(i), label=i % 2,
            prediction=i % 2,
            confidence=1,
            accepted=True,
            latency_ms=20,
            human_label=i % 2,
            teacher_prediction=i % 2,
        )
        for i in range(500)
    ]


def test_perfect_critical_and_sparse_rejection():
    assert evaluate_task(perfect(), critical=True)["eligible"]
    assert "insufficient_test_samples" in evaluate_task(perfect()[:10])["reasons"]


def test_human_latency_and_coverage_gates():
    rows = perfect()
    for row in rows:
        row.pop("human_label")
        row["latency_ms"] = 501
        row["accepted"] = False
    result = evaluate_task(rows, critical=True)
    assert set(result["reasons"]) >= {"critical_human_comparison", "latency", "coverage", "accepted_error"}


def test_overconfidence_rejected():
    rows = perfect()
    for row in rows[:100]:
        row["prediction"] = 1 - row["label"]
    result = evaluate_task(rows)
    assert result["ece"] > 0.05
    assert not result["eligible"]


def test_missing_required_class_and_score_argmax():
    assert not evaluate_task(perfect(), required_labels=[0, 1, 2])["eligible"]
    rows = [
        dict(request_id=str(i), label=i % 2, prediction=i % 2 + 0.2, prediction_class=i % 2, confidence=1, accepted=True, latency_ms=1)
        for i in range(500)
    ]
    result = evaluate_task(rows, kind="score")
    assert result["eligible"] and 0.19 < result["mae"] < 0.21
    assert result["accepted_error"] == 0




def test_low_point_error_is_not_sufficient_and_repeated_request_is_not_independent():
    rows = perfect()
    for row in rows[:20]:
        row["prediction"] = 1-row["label"]
    result = evaluate_task(rows)
    assert result["accepted_error"] == .04
    assert "accepted_error_uncertainty" in result["reasons"]
    rows = perfect()
    for row in rows:
        row["request_id"] = "same-request"
    result = evaluate_task(rows)
    assert result["accepted_request_count"] == 1
    assert not result["eligible"]


def target_records():
    return [dict(request_id=str(i), question_id=q, task_id="target", request_question_count=2,
                 label=label, prediction=label, accepted=True)
            for i in range(500) for q,label in (("target.0",1),("target.1",0))]


def test_target_sets_reject_incomplete_and_report_missed_targets():
    from astrbot_plugin_chat_dynamics.core.decision_evaluation import evaluate_target_sets
    rows = target_records()
    assert evaluate_target_sets(rows)["eligible"]
    del rows[0]
    report = evaluate_target_sets(rows)
    assert report["incomplete_requests"] == 1 and not report["eligible"]
    rows = target_records()
    for row in rows:
        row["prediction"] = 0
    report = evaluate_target_sets(rows)
    assert report["false_negative_targets"] == 500
    assert report["exact_match"] == 0 and not report["eligible"]


def test_empty_target_with_reply_is_invalid_plan():
    from astrbot_plugin_chat_dynamics.core.decision_evaluation import evaluate_target_sets
    rows = [dict(request_id="r", question_id="target.0", task_id="target", request_question_count=2,
                 label=0, prediction=0, accepted=True),
            dict(request_id="r", question_id="action", task_id="action", request_question_count=2,
                 label=1, prediction=1, prediction_action="reply", accepted=True)]
    assert evaluate_target_sets(rows)["invalid_plans"] == 1
