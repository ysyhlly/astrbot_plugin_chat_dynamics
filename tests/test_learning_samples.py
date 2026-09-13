"""Learning samples must stay trustworthy: bounded, whitelisted, round-trippable.

Three rules the rest of the layer depends on and that are easy to break by
accident:

* a fact that was never observed is absent, not zero;
* a decision the trace did not record is undecided, not a negative label;
* every row says which policy produced it and which conversation it came from,
  or it cannot be validated later.
"""
import json

import pytest

from astrbot_plugin_chat_dynamics.core.learning import (
    SKIP_NO_BOT_ID,
    SKIP_UNDECIDED_REPLY,
    LearningSample,
    SampleStore,
    build_samples,
    factor_disagreement,
    features_from_trace,
    samples_from_annotation,
    samples_from_annotations,
    session_hash,
    summarize,
)
from astrbot_plugin_chat_dynamics.core.learning.sample import (
    SAMPLE_SCHEMA_VERSION,
    InvalidSample,
)


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


def test_an_undecided_reply_is_not_learned_as_silence():
    # The trace allows should_reply = None and it means "no decision was taken".
    # Recording it as "silent" would teach a learner that not deciding is
    # deciding not to, which is the same class of bug as a fabricated bot id.
    undecided = record()
    undecided["decision_trace"]["participation"]["should_reply"] = None
    report = build_samples([undecided], bot_id="bot")
    assert {s.task for s in report.samples} == {"topic", "recipient"}
    assert report.skipped[SKIP_UNDECIDED_REPLY] == 1
    assert any("未决" in warning for warning in report.warnings())

    decided = record()
    decided["decision_trace"]["participation"]["should_reply"] = False
    assert "participation" in {s.task for s in samples_from_annotation(decided, bot_id="bot")}


def test_the_build_report_accounts_for_every_withheld_row():
    rows = [record(msg_id="a"), record(msg_id="b")]
    for row in rows:
        row["decision_trace"]["participation"]["should_reply"] = None
    report = build_samples(rows, bot_id="")
    assert report.records == 2
    assert report.skipped[SKIP_NO_BOT_ID] == 2
    assert report.skipped[SKIP_UNDECIDED_REPLY] == 2
    assert report.skipped_total == 4
    # Recipient rows need the bot id, so only the topic rows survive.
    assert {s.task for s in report.samples} == {"topic"}
    assert any("bot id" in warning for warning in report.warnings())


def test_samples_carry_provenance_and_a_grouping_key():
    payload = record()
    payload["session_key"] = "umo:group:42"
    payload["decision_trace"]["weights_version"] = "routing-weights-v7"
    payload["decision_trace"]["routing_schema_version"] = 2
    sample = samples_from_annotation(payload, bot_id="bot", plugin_version="v1.6.0")[0]
    assert sample.policy_version == "routing-weights-v7"
    assert sample.routing_schema_version == 2
    assert sample.plugin_version == "v1.6.0"
    assert sample.session_hash == session_hash("umo:group:42")
    assert sample.group_key == sample.session_hash
    assert sample.to_dict()["schema"] == SAMPLE_SCHEMA_VERSION
    assert LearningSample.from_dict(sample.to_dict()) == sample


def test_a_row_without_a_session_cannot_be_grouped():
    sample = samples_from_annotation(record(), bot_id="bot")[0]
    assert sample.session_hash == "" and sample.group_key == ""
    report = build_samples([record()], bot_id="bot")
    assert report.ungroupable == 3
    assert any("session" in warning for warning in report.warnings())


def test_schema_one_rows_still_load_with_empty_provenance():
    legacy = samples_from_annotation(record(), bot_id="bot")[0].to_dict()
    legacy["schema"] = 1
    for key in ("session_hash", "policy_version", "plugin_version",
                "routing_schema_version", "feature_schema_version"):
        legacy.pop(key, None)
    restored = LearningSample.from_dict(legacy)
    assert restored.policy_version == "" and restored.session_hash == ""
    assert restored.feature_schema_version == 1


def test_a_missing_fact_is_absent_rather_than_zero():
    plain = samples_from_annotation(record(), bot_id="bot")[0]
    assert "fact.is_question" not in plain.present
    assert "fact.is_question" not in plain.feature_map()
    kept = samples_from_annotation(record(text="1.21.4"), bot_id="bot")[0]
    # Observed-and-false is a different fact from never-observed.
    assert "fact.is_question" in kept.present
    assert kept.feature_map()["fact.is_question"] == 0.0


def test_participation_features_use_the_raw_fact_not_the_contribution():
    # temporal_gap's raw value is a number of seconds; its contribution is the
    # +0.25 the policy applied. A learner fed the contribution could only
    # recover the weights that are already in force.
    payload = record()
    payload["decision_trace"]["ledger"]["entries"] = [
        {"code": "temporal_gap", "raw_value": 7.3, "contribution": 0.25},
        {"code": "intervening_messages", "raw_value": 3, "contribution": -0.15},
    ]
    features = dict(features_from_trace(payload["decision_trace"], payload["routing"]))
    assert features["temporal_gap"] == pytest.approx(7.3)
    assert features["intervening_messages"] == pytest.approx(3.0)


def test_summary_splits_the_factor_table_by_task():
    right = [_with_turn_factor(f"r{i}", 1.0, "t2") for i in range(6)]
    wrong = [_with_turn_factor(f"w{i}", 0.2, "t9") for i in range(6)]
    report = summarize(right + wrong, min_support=4)
    # Every task gets its own table: one annotation copies its features into
    # three rows, so a code can be wrong for topic and right for recipient at
    # the same time and averaging them describes neither.
    assert "factors" not in report
    assert set(report["tasks"]) == {"topic"}
    factor_rows = {row["code"]: row for row in report["tasks"]["topic"]["factors"]}
    # The separating factor ranks first; the constant routing code contributes
    # nothing and is still reported so its support stays visible.
    assert next(iter(factor_rows)) == "dialogue_turn_factor"
    assert factor_rows["dialogue_turn_factor"]["delta"] == pytest.approx(-0.8)
    assert factor_rows["dialogue_turn_factor"]["coverage"] == pytest.approx(1.0)


def test_factor_rows_report_coverage_instead_of_treating_absence_as_a_value():
    rows = [_with_turn_factor(f"w{i}", 0.2, "t9") for i in range(6)]
    # Half the corpus never saw the extra fact at all.
    rows += [LearningSample("s", f"x{i}", 1.0, "topic", "t2", "t2", 0.5, "t",
                            (("dialogue_turn_factor", 1.0),)) for i in range(6)]
    table = {row["code"]: row for row in factor_disagreement(rows, min_support=4)}
    assert table["dialogue_turn_factor"]["coverage"] == pytest.approx(1.0)
    assert table["dialogue_turn_factor"]["wrong_n"] == 6


def test_binary_tasks_report_false_positives_and_negatives_separately():
    from astrbot_plugin_chat_dynamics.core.learning import outcome_bucket
    from astrbot_plugin_chat_dynamics.core.learning.stats import POSITIVE_CLASS

    def row(predicted, expected):
        return LearningSample("s", f"{predicted}-{expected}", 1.0, "recipient", predicted,
                              expected, 0.5, "t", (("explicit_reply", 1.0),))

    assert POSITIVE_CLASS["recipient"] == "bot"
    assert outcome_bucket(row("bot", "bot")) == "tp"
    assert outcome_bucket(row("bot", "other")) == "fp"
    assert outcome_bucket(row("other", "bot")) == "fn"
    # A true negative names the positive class nowhere, so it must not fall
    # through to the non-binary branch.
    assert outcome_bucket(row("other", "other")) == "tn"


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
