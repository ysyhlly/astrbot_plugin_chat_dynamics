"""Partial hard supervision keeps independent target booleans and rejects guesses."""

import copy
from collections import Counter
import hashlib
import hmac
import json
import sqlite3

import pytest

from astrbot_plugin_chat_dynamics.scripts.kev_prepare import (
    _finish_cases, build_case, prepare_sqlite,
)


SOURCE = {
    "persona": "安安话少，但会回应明确提问。",
    "environment": {"schema_version": 1, "mentioned_self": True},
    "conversation": {"text": "@安安 帮我看看", "author": "u1", "background": [],
                     "messages": [
                         {"message_id": "m1", "author": "u1", "text": "@安安 帮我看看",
                          "timestamp": 1, "reply_to": None, "mentioned_users": ["bot"]},
                         {"message_id": "m2", "author": "u1", "text": "还有这条",
                          "timestamp": 2, "reply_to": "m1", "mentioned_users": []}]},
    "target_candidates": {
        "target.0": {"message_id": "m1", "author": "u1", "text": "@安安 帮我看看",
                     "text_missing": False},
        "target.1": {"message_id": "m2", "author": "u1", "text": "还有这条",
                     "text_missing": False}},
}


def row(qid, task, spec, label):
    return {"session_id": "session", "task_id": task, "task_version": "2",
            "created_at": 100.0, "state": copy.deepcopy(SOURCE),
            "candidates": spec, "teacher_label": label,
            "teacher_model": "teacher-version",
            "metadata": {"request_id": "request", "question_id": qid,
                         "snapshot_version": "1", "source_state": copy.deepcopy(SOURCE),
                         "teacher_prompt_version": "zh-rubric-v2",
                         "bot_speaker_id": "bot"}}


def example_rows():
    return [
        row("join", "join", {"type": "noul", "instructions": "参与？"},
            {"type": "noul", "noul": 1.0}),
        row("action", "action", {"type": "choice", "instructions": "动作？",
                                 "criteria": {"ignore": "忽略", "reply": "回复"}},
            {"type": "choice", "choice": "reply"}),
        row("target.0", "target", {"type": "noul",
                                   "instructions": "Should the response address message m1?"},
            {"type": "noul", "noul": 1.0}),
        row("target.1", "target", {"type": "noul",
                                   "instructions": "Should the response address message m2?"},
            {"type": "noul", "noul": 1.0}),
        row("reply_length", "reply_length", {"type": "choice", "instructions": "多长？",
                                             "criteria": {"tiny": "短", "short": "稍长"}}, None),
    ]


def test_partial_labels_keep_two_positive_targets_without_filling_length():
    record, detail = build_case(example_rows())
    assert record["questions"]["target.0"]["label"] is True
    assert record["questions"]["target.1"]["label"] is True
    assert "reply_length" not in record["questions"]
    assert detail["skipped_questions"] == {"teacher_label_missing": 1}
    assert record["state"]["target_candidates"]["target.1"]["text"] == "还有这条"


def test_missing_bot_identity_is_not_reconstructed_from_chat():
    rows = example_rows()
    for item in rows:
        del item["metadata"]["bot_speaker_id"]
    with pytest.raises(ValueError, match="bot_identity_missing"):
        build_case(rows)


def test_verified_historical_identity_recovers_missing_metadata():
    rows = example_rows()
    for item in rows:
        del item["metadata"]["bot_speaker_id"]
    record, _ = build_case(rows, verified_bot_id="anon_bot")
    assert record["state"]["bot_speaker_id"] == "anon_bot"
    rows[0]["metadata"]["bot_speaker_id"] = "another_bot"
    with pytest.raises(ValueError, match="bot_identity_mismatch"):
        build_case(rows, verified_bot_id="anon_bot")


def test_sqlite_recovery_requires_matching_flagged_bot_message(tmp_path):
    database = tmp_path / "samples.sqlite3"
    secret = b"test secret"
    bot_id = "anon_" + hmac.new(secret, b"raw-bot", hashlib.sha256).hexdigest()[:32]
    rows = example_rows()
    for item in rows:
        del item["metadata"]["bot_speaker_id"]
        item["metadata"]["source_state"]["conversation"]["background"] = [
            {"message_id": "old-bot-message", "author": bot_id, "text": "上一轮回复",
             "timestamp": 0, "reply_to": None, "mentioned_users": [],
             "semantics": {"sender_is_bot": True}}
        ]
        item["state"] = copy.deepcopy(item["metadata"]["source_state"])
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE settings (key TEXT, value TEXT)")
        db.execute("INSERT INTO settings VALUES ('secret_hex', ?)", (secret.hex(),))
        db.execute("CREATE TABLE samples (id TEXT, payload TEXT)")
        db.execute("CREATE INDEX sample_request ON samples(json_extract(payload,'$.metadata.request_id')) WHERE json_valid(payload)")
        db.execute("CREATE INDEX sample_labeled ON samples(id) WHERE json_extract(payload,'$.teacher_label') IS NOT NULL")
        for index, item in enumerate(rows):
            db.execute("INSERT INTO samples VALUES (?,?)", (str(index), json.dumps(item)))
    partitions, report = prepare_sqlite(database, "raw-bot")
    assert report["accepted_cases"] == 1
    assert report["bot_identity_evidence_occurrences"] == 4
    assert sum(map(len, partitions.values())) == 1
    assert not report["release_data_gate"]["ready"]
    with pytest.raises(ValueError, match="bot_identity_evidence_conflict"):
        prepare_sqlite(database, "wrong-bot")


def test_export_retains_independent_episode_groups():
    cases = []
    for case_id, timestamp, state_text in [
        ("a", 0, "alpha " * 20),
        ("b", 10, "beta " * 20),
        ("c", 3600, "gamma " * 20),
    ]:
        cases.append({
            "id": case_id, "session_id": "one-session", "created_at": timestamp,
            "state": {"text": state_text},
            "record": {"state": {"text": state_text},
                       "questions": {"join": {"label": False}},
                       "_meta": {"group_id": case_id}},
        })
    partitions, report = _finish_cases(
        cases, input_rows=3, input_requests=3,
        reasons=Counter(), partial=Counter(),
    )
    exported = {case["id"]: case["record"]["_meta"]["group_id"] for case in cases}
    assert exported["a"] == exported["b"]
    assert exported["c"] != exported["a"]
    assert report["independent_groups"] == 2
    assert sum(p["independent_groups"] for p in report["partitions"].values()) == 2
    assert sum(map(len, partitions.values())) == 3
