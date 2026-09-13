"""Learning samples must stay trustworthy: bounded, whitelisted, round-trippable."""
import json

import pytest

from astrbot_plugin_chat_dynamics.core.learning import (
    LearningSample,
    SampleStore,
    factor_disagreement,
    samples_from_annotation,
    samples_from_annotations,
    summarize,
)
from astrbot_plugin_chat_dynamics.core.learning.sample import InvalidSample


def record(**overrides):
    base = {
        "msg_id": "m1", "predicted_topic": "t1", "expected_topic": "t2",
        "error_type": "topic_split", "bot_targeted": True, "recipient_error_type": "missed_bot",
        "expected_reply": True, "annotated_at": 1000.0,
        "routing": {"evidence": ["active_dialogue_answer"]},
        "decision_trace": {
            "topic": {"confidence": 0.61},
            "recipient": {"confidence": 0.44, "ids": ["h1"]},
            "participation": {"confidence": 0.5, "should_reply": False},
            "ledger": {"entries": [
                {"code": "dialogue_time_decay", "raw_value": 0.98, "contribution": 0.59},
                {"code": "dialogue_turn_factor", "raw_value": 1.0, "contribution": 1.0},
                {"code": "quote_edge", "raw_value": 0.4},
            ]},
        },
    }
    base.update(overrides)
    return base


def test_one_annotation_yields_three_labelled_tasks():
    samples = samples_from_annotation(record(), bot_id="bot")
    by_task = {s.task: s for s in samples}
    assert set(by_task) == {"topic", "recipient", "participation"}
    assert by_task["topic"].predicted == "t1" and by_task["topic"].expected == "t2"
    assert by_task["recipient"].predicted == "other" and by_task["recipient"].expected == "bot"
    assert by_task["participation"].predicted == "silent"
    assert by_task["participation"].expected == "reply"
    assert all(s.correct is False for s in samples)
    assert by_task["topic"].confidence == pytest.approx(0.61)


def test_optional_labels_are_simply_absent():
    without_target = record(bot_targeted=None, expected_reply=None)
    without_target.pop("bot_targeted")
    without_target.pop("expected_reply")
    tasks = {s.task for s in samples_from_annotation(without_target, bot_id="bot")}
    assert tasks == {"topic"}
    empty = samples_from_annotation({}, bot_id="bot")
    assert empty == []


def test_features_come_from_the_ledger_allowlist_only():
    sample = samples_from_annotation(record(), bot_id="bot")[0]
    features = sample.feature_map()
    assert features["dialogue_time_decay"] == pytest.approx(0.98)
    assert features["dialogue_turn_factor"] == pytest.approx(1.0)
    # An unregistered code is dropped rather than stored.
    assert "quote_edge" not in features
    # A code the router applied without a numeric factor is recorded as present.
    assert features["active_dialogue_answer"] == pytest.approx(1.0)


def test_recipient_samples_are_withheld_without_the_bot_id():
    # Guessing here would mark every row "not the bot", which reads as a
    # flawless corpus whenever the annotator agreed with the router.
    tasks = {s.task for s in samples_from_annotation(record(), bot_id="")}
    assert tasks == {"topic", "participation"}
    with_id = {s.task for s in samples_from_annotation(record(), bot_id="bot")}
    assert "recipient" in with_id


def test_message_facts_appear_only_when_the_annotation_kept_the_text():
    plain = samples_from_annotation(record(), bot_id="bot")
    trace_facts = {code: value for code, value in plain[0].features if code.startswith("fact.")}
    # Identity facts come from the trace and never need the message text.
    assert set(trace_facts) == {"fact.bot_mentioned", "fact.bot_vocative",
                                "fact.bot_subject", "fact.has_reply"}
    assert "fact.is_short" not in plain[0].feature_map()

    kept = samples_from_annotation(record(text="1.21.4"), bot_id="bot")[0]
    facts = kept.feature_map()
    assert facts["fact.is_short"] == 1.0 and facts["fact.is_answer_like"] == 1.0
    assert facts["fact.is_question"] == 0.0 and facts["fact.reaction_like"] == 0.0
    # Shape facts are derived, and the text they came from is still not stored.
    assert "1.21.4" not in json.dumps(kept.to_dict())


def test_sample_rejects_anything_it_cannot_vouch_for():
    ok = dict(session_id="s", message_id="m", timestamp=1.0, task="topic",
              predicted="a", expected="b", confidence=0.5, source="test")
    with pytest.raises(InvalidSample):
        LearningSample(**ok, features=(("not_a_real_code", 1.0),))
    with pytest.raises(InvalidSample):
        LearningSample(**ok, features=(("semantic", float("nan")),))
    with pytest.raises(InvalidSample):
        LearningSample(**{**ok, "task": "guess"})
    with pytest.raises(InvalidSample):
        LearningSample(**{**ok, "session_id": "x" * 200})
    with pytest.raises(InvalidSample):
        LearningSample(**{**ok, "timestamp": float("inf")})


def test_round_trip_and_corrupt_rows():
    sample = samples_from_annotation(record(), bot_id="bot")[0]
    assert LearningSample.from_dict(sample.to_dict()) == sample
    with pytest.raises(InvalidSample):
        LearningSample.from_dict({"schema": 99})
    with pytest.raises(InvalidSample):
        LearningSample.from_dict({**sample.to_dict(), "features": [["a"]]})


def _with_turn_factor(marker: str, turn: float, predicted_topic: str):
    payload = record(msg_id=marker, predicted_topic=predicted_topic, bot_targeted=False)
    payload["decision_trace"]["ledger"]["entries"][1]["raw_value"] = turn
    return samples_from_annotation(payload, bot_id="bot")[0]


def test_factor_table_reports_the_direction_of_the_difference():
    right = [_with_turn_factor(f"r{i}", 1.0, "t2") for i in range(6)]
    wrong = [_with_turn_factor(f"w{i}", 0.2, "t9") for i in range(6)]
    rows = {row["code"]: row for row in factor_disagreement(right + wrong, min_support=4)}
    turn = rows["dialogue_turn_factor"]
    # Lower interruption factor on wrong decisions shows up as a negative delta,
    # which is the whole point of keeping raw values instead of only outcomes.
    assert turn["wrong_n"] == 6 and turn["right_n"] == 6
    assert turn["mean_wrong"] == pytest.approx(0.2)
    assert turn["mean_right"] == pytest.approx(1.0)
    assert turn["delta"] == pytest.approx(-0.8)


def test_summary_counts_what_it_measured():
    samples = samples_from_annotations([record(msg_id="a"), record(msg_id="b")], bot_id="bot")
    report = summarize(samples, min_support=1)
    assert report["total"] == 6
    assert report["tasks"]["topic"]["total"] == 2
    assert report["tasks"]["topic"]["accuracy"] == 0.0
    assert "仅统计已标注样本" in report["sample_note"]


def test_store_is_off_until_enabled_and_bounded(tmp_path):
    samples = samples_from_annotations([record(msg_id=f"m{i}") for i in range(5)], bot_id="bot")
    store = SampleStore(tmp_path, enabled=False)
    assert store.append(samples) == 0 and store.read() == []
    store.enabled = True
    assert store.append(samples) == 15
    assert len(store.read()) == 15
    # The cap drops the oldest rows instead of growing without bound.
    store.limit = 4
    store.append(samples)
    assert len(store.read()) == 4
    assert store.dropped > 0
    # No message text ever reaches disk.
    assert "text" not in json.dumps([s.to_dict() for s in store.read()])


def test_store_skips_corrupt_lines(tmp_path):
    store = SampleStore(tmp_path, enabled=True)
    store.append(samples_from_annotation(record(), bot_id="bot"))
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n\n")
    rows = store.read()
    assert rows and all(isinstance(row, LearningSample) for row in rows)
