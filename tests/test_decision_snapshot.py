import copy
import json

import pytest

from astrbot_plugin_chat_dynamics.core.decision_snapshot import parse_snapshot, validate_snapshot
from astrbot_plugin_chat_dynamics.core.decision_dataset import DecisionDataset
from services.laya_service.backend import prepare_state


class Tokenizer:
    def __call__(self, text, **kwargs):
        return {"input_ids": list(text)}


QUESTION = {"join": {"type": "noul", "instructions": "Speak?"}}
TARGET = {"target.0": {"type": "noul", "instructions": "Should the response address message m1?"}}


def test_json_is_complete_idempotent_and_source_unmodified():
    source = {"text": '新消息 "quote" 😀', "history": [{"text": "旧" * 100}, {"text": "最新"}]}
    before = copy.deepcopy(source)
    prepared = prepare_state(Tokenizer(), source, QUESTION, 128, 32)
    value = json.loads(prepared)
    assert value["text"] == source["text"]
    assert value["history"] == [{"text": "最新"}]
    assert source == before
    assert prepare_state(Tokenizer(), prepared, QUESTION, 128, 32) == prepared


def test_complete_legacy_pair_merges_but_corrupt_suffix_is_rejected():
    assert parse_snapshot('{"conversation":{"text":"hi"}}\n{"conversation":{"background":[]}}') == {
        "conversation": {"text": "hi", "background": []}}
    for bad in ('{"text":"hi"}\nuthor":"clipped"}', '{"text":"unterminated', '{"text":"hi"}{"text":"changed"}'):
        with pytest.raises(ValueError, match="complete JSON"):
            prepare_state(Tokenizer(), bad, QUESTION)


def test_critical_candidates_and_reply_evidence_are_never_trimmed():
    candidate = {"message_id": "m1", "text": "候选正文" * 40, "reply_to": "parent", "timestamp": 20}
    state = {"text": "current", "target_candidates": {"target.0": candidate},
             "background": [{"message_id": "parent", "text": "回复原文" * 40, "timestamp": 10}]}
    with pytest.raises(ValueError, match="critical"):
        prepare_state(Tokenizer(), state, TARGET, 200, 32)
    prepared = json.loads(prepare_state(Tokenizer(), state, TARGET, 2000, 32))
    assert prepared == state


def test_missing_candidate_is_rejected_before_recording(tmp_path):
    state = {"target_candidates": {"target.0": {"message_id": "m1", "text": "", "text_missing": True}}}
    store = DecisionDataset(tmp_path / "data.db")
    with pytest.raises(ValueError, match="candidate text"):
        store.record_sample("room", "target", state, TARGET["target.0"], question_id="target.0")
    assert store.samples() == []


def test_invalid_historical_snapshots_are_not_teacher_exports(tmp_path):
    store = DecisionDataset(tmp_path / "data.db")
    sid = store.record_sample("room", "join", {"text": "valid"}, QUESTION["join"],
                              teacher_label={"noul": 0}, teacher_model="teacher")
    row = store.samples()[0]
    row["state"] = '{"text":"valid"}\nclipped JSON'
    with store.connect() as db:
        db.execute("UPDATE samples SET payload=? WHERE id=?", (json.dumps(row), sid))
    assert store.export(tmp_path / "train.jsonl") == 0
    assert store.export(tmp_path / "archive.jsonl", teacher_only=False) == 1
    assert store.audit_snapshots(quarantine=True)["quarantined"] == 1
    quarantined = store.samples()[0]
    assert quarantined["teacher_label"] == {"noul": 0}
    assert "invalid_snapshot" in quarantined["metadata"]["quality_flags"]
    assert store.claim_label() is None


def test_source_mentions_are_anonymized_together_with_rendered_text(tmp_path):
    store = DecisionDataset(tmp_path / "data.db")
    state = {"text": "@123456789", "mentioned_users": ["123456789"]}
    anonymous = store.anonymize({"state": state, "source_state": state})
    assert "123456789" not in json.dumps(anonymous)
    assert anonymous["state"] == anonymous["source_state"]


def test_missing_recipient_evidence_rejected_but_identified_bot_allowed():
    q = {"recipient.0": {"type": "noul", "instructions": "recipient?"}}
    state = {"routing_semantics": {"recipient_candidates": {"recipient.0": {"user_id": "bot", "is_bot": True, "messages": []}}}}
    validate_snapshot(state, q)
    state["routing_semantics"]["recipient_candidates"]["recipient.0"]["is_bot"] = False
    with pytest.raises(ValueError, match="recipient candidate text"):
        validate_snapshot(state, q)
