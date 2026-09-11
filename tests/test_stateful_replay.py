"""Replay must advance the same cross-message state production advances.

A routing-only replay cannot see hover accumulation, TTL expiry, or an active
dialogue that waits for one participant. These cases execute the production
state transitions between turns and lock the resulting boundaries.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/routing_golden.json"


def load_evaluator():
    spec = importlib.util.spec_from_file_location("stateful_evaluator", ROOT / "scripts/evaluate_routing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def golden_case(case_id):
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return next(case for case in cases if case["id"] == case_id)


def turns_by_index(row):
    return {turn["index"]: turn for turn in row["turns"]}


def test_replay_commits_hover_state_between_turns():
    report = load_evaluator().evaluate([golden_case("hover_escalation_bounded")])
    row = report["results"][0]
    turns = turns_by_index(row)
    # The first ambiguous turn is buffered only after it is scored.
    assert turns[2]["state"]["pending_hover"] is False
    assert turns[3]["state"]["pending_hover"] is True
    assert turns[3]["state"]["pending_hover_user"] == "B"
    assert [turns[i]["actual"]["level"] for i in (2, 3)] == ["hover", "hover"]
    assert report["failed"] == 0


def test_replay_does_not_score_bot_turns():
    row = load_evaluator().evaluate([golden_case("hover_escalation_bounded")])["results"][0]
    # Turn 1 is the bot's own message: production anchors it, never scores it.
    assert sorted(turns_by_index(row)) == [0, 2, 3]


def test_author_identity_decides_whether_a_hover_resolves():
    report = load_evaluator().evaluate([
        golden_case("pending_hover_resolves_for_author"),
        golden_case("hover_escalation_bounded")])
    interlocutor, bystander = report["results"]
    # Same messages, different author: only the bot's own interlocutor escalates.
    assert interlocutor["turns"][-1]["actual"]["level"] == "strong"
    assert interlocutor["turns"][-1]["actual"]["bot_targeted"] is True
    assert bystander["turns"][-1]["actual"]["level"] == "hover"
    assert bystander["turns"][-1]["actual"]["bot_targeted"] is False


def test_stale_hover_is_expired_before_scoring():
    row = load_evaluator().evaluate([golden_case("stale_hover_expires")])["results"][0]
    turns = turns_by_index(row)
    assert turns[2]["actual"]["level"] == "hover"
    assert turns[3]["state"]["pending_hover"] is False
    assert turns[3]["actual"]["level"] == "weak"


def test_waiting_answer_is_visible_and_scoped_to_one_participant():
    row = load_evaluator().evaluate([golden_case("answer_then_third_party_stays_hover")])["results"][0]
    turns = turns_by_index(row)
    assert turns[2]["state"]["waiting_for_answer"] is True
    assert turns[3]["state"]["intervening_users"] == 1
    assert turns[3]["state"]["waiting_for_answer"] is False
    assert turns[3]["actual"]["bot_targeted"] is False


def test_committed_state_is_reported_in_the_decision_trace():
    row = load_evaluator().evaluate([golden_case("hover_escalation_bounded")])["results"][0]
    state = row["traces"]["legacy"]["state"]
    # The trace now carries real cross-message state instead of placeholders.
    assert state["active_interlocutor"] == "A"
    assert state["last_bot_message_id"] == "1"
    assert state["last_bot_was_question"] is False


@pytest.mark.parametrize("case_id", ["hover_escalation_bounded", "pending_hover_resolves_for_author",
                                     "answer_then_third_party_stays_hover", "stale_hover_expires"])
def test_stateful_cases_are_deterministic(case_id):
    evaluator = load_evaluator()
    case = golden_case(case_id)
    assert evaluator.evaluate([case]) == evaluator.evaluate([case])
