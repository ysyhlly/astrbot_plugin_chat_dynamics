"""Request-level reporting must not inflate takeover using per-question rows."""

from astrbot_plugin_chat_dynamics.tools.benchmark_chat import report


def _row(request, question, source, *, expected=2, outcome="suppressed"):
    return {
        "metadata": {"request_id": request, "question_id": question,
                     "request_question_count": expected, "model_only": True,
                     "student_status": "answered", "preparation_latency_ms": 4,
                     "student_latency_ms": 90},
        "execution_source": source,
        "outcome": {"latency_ms": 110, "final_outcome": outcome},
        "student_prediction": ({"noul": .8} if question == "join" else
                               {"choice": "reply"}),
    }


def test_request_report_separates_model_response_adoption_and_delivery():
    rows = [
        _row("private-id-abc", "join", "kev", outcome="delivered"),
        _row("private-id-abc", "action", "kev", outcome="delivered"),
        _row("private-id-def", "join", "kev"),
    ]
    result = report(rows, since=1, until=2)
    assert result["sample_rows"] == 3
    assert result["requests"] == 2
    assert result["complete_request_records"] == 1
    assert result["kev_adopted_full_requests"] == 1
    assert result["final_outcome_requests"] == {"delivered": 1, "suppressed": 1}
    assert result["latency_ms"]["student_http_ms"]["n"] == 2
    assert "private-id-abc" not in str(result) and "private-id-def" not in str(result)
