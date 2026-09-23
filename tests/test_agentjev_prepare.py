"""A request stays intact and incomplete candidate snapshots never train."""

from copy import deepcopy
import json
import random
from collections import Counter

import pytest

from astrbot_plugin_chat_dynamics.scripts.agentjev_prepare import build_case, prepare


def request(number: int, *, join: bool = True):
    request_id = f"request_{number}"
    topic = "".join(random.Random(number).choices("显卡驱动频率功耗内存网络延迟温度散热系统排障", k=240))
    state = {
        "conversation": {
            "author": "user",
            "text": f"请看第 {number} 个完全不同的问题：{topic}",
            "messages": [{"message_id": f"m{number}a", "author": "user", "text": "显卡频率异常"}],
            "background": [{"message_id": f"m{number}b", "author": "bot", "text": "请检查功耗限制"}],
        },
        "target_candidates": {
            "target.0": {"message_id": f"m{number}a", "author": "user", "text": "显卡频率异常", "text_missing": False},
            "target.1": {"message_id": f"m{number}b", "author": "bot", "text": "请检查功耗限制", "text_missing": False},
        },
    }
    common = {"session_id": f"session_{number}", "task_version": "2", "state": "prepared state",
              "teacher_model": "deepseek-v4.1-flash", "created_at": number * 3600}
    meta = {"request_id": request_id, "request_question_count": 6,
            "teacher_prompt_version": "zh-rubric-v2", "snapshot_version": "1",
            "source_state": state, "quality_flags": []}

    def row(qid, question, label):
        return {**common, "task_id": qid.split(".")[0], "candidates": question,
                "teacher_label": label, "metadata": {**meta, "question_id": qid}}

    return [
        row("join", {"type": "noul", "instructions": "是否参与？"}, {"noul": float(join)}),
        row("action", {"type": "choice", "instructions": "如何回应？",
                       "criteria": {"ignore": "保持安静", "reply": "回复"}},
            {"choice": "reply" if join else "ignore"}),
        row("target.0", {"type": "noul", "instructions": f"Should address message m{number}a?"},
            {"noul": float(join)}),
        row("target.1", {"type": "noul", "instructions": f"Should address message m{number}b?"},
            {"noul": 0.0}),
        row("recipient_choice", {"type": "choice", "instructions": "主要回复谁？",
                                 "criteria": {"user": "发出当前消息的群友", "none": "没有单一对象"}},
            {"choice": "user" if join else "none"}),
        row("reply_length", {"type": "choice", "instructions": "应该回复多长？",
                             "criteria": {key: key for key in ("tiny", "short", "medium", "long", "very_long")}},
            {"choice": "short"}),
    ]


def test_complete_request_becomes_one_choice_case():
    case, positive = build_case(request(1))
    assert positive
    assert case["state"].startswith('{"current":{"text":')
    assert [q["id"] for q in case["questions"]] == ["join", "recipient", "target", "action", "reply_length"]
    target = case["questions"][2]
    assert len(target["candidates"]) == 3
    assert "显卡频率异常" in target["candidates"][0]
    assert target["gold"]["distribution"] == [1, 0, 0]


def test_long_target_candidates_retain_distinct_tails_after_upstream_truncation():
    rows = request(22)
    state = rows[0]["metadata"]["source_state"]
    state["conversation"]["text"] = "帮我看看"
    common_tail = "相同日志尾部" * 30
    for index in (0, 1):
        state["target_candidates"][f"target.{index}"]["text"] = f"不同前缀{index}" + common_tail
    case, _ = build_case(rows)
    options = case["questions"][2]["candidates"]
    assert options[0][-64:] != options[1][-64:]


def test_prepared_teacher_view_is_used_and_current_state_precedes_history():
    rows = request(11)
    source = rows[0]["metadata"]["source_state"]
    source["conversation"]["background"].append(
        {"message_id": "extra", "author": "other", "text": "旧消息" * 1500})
    prepared = deepcopy(source)
    prepared["conversation"]["background"].pop()
    for row in rows:
        row["metadata"]["tokenizer_prepared"] = True
        row["state"] = json.dumps(prepared, ensure_ascii=False)
    case, _ = build_case(rows)
    packed = json.loads(case["state"])
    assert packed["current"]["text"] == prepared["conversation"]["text"]
    assert packed["background"] == prepared["conversation"]["background"]
    assert "extra" not in case["state"]
    bad = deepcopy(rows)
    bad[0]["state"] = json.dumps({**prepared, "conversation": {
        **prepared["conversation"], "text": "不同的触发正文"}}, ensure_ascii=False)
    with pytest.raises(ValueError, match="prepared_evidence_mismatch"):
        build_case(bad)


def test_tokenizer_gate_rejects_current_message_that_would_be_cut():
    rows = request(12)
    def tokenizer(text, **_):
        return {"input_ids": list(text)}
    with pytest.raises(ValueError, match="current_state_over_token_budget"):
        build_case(rows, tokenizer=tokenizer, max_state_tokens=100)
    rows = request(13)
    rows[0]["metadata"]["source_state"]["conversation"]["text"] = "帮我看看"
    case, _ = build_case(rows, tokenizer=tokenizer, max_state_tokens=256)
    assert json.loads(case["state"])["current"]["text"] == "帮我看看"


def test_incomplete_body_and_multiple_targets_are_rejected():
    rows = request(2)
    rows[2]["metadata"]["source_state"]["target_candidates"]["target.0"]["text_missing"] = True
    with pytest.raises(ValueError, match="target_candidate_body_missing"):
        build_case(rows)
    rows = request(2)
    rows[3]["teacher_label"] = {"noul": 1.0}
    with pytest.raises(ValueError, match="target_multiple_positives"):
        build_case(rows)


def test_split_keeps_cases_and_calibration_natural():
    rows = [row for n in range(1, 11) for row in request(n, join=n % 2 == 0)]
    partitions, report = prepare(deepcopy(rows), min_cases=3, min_positive_per_split=0)
    all_ids = [{case["id"] for case in partitions[name]} for name in ("train", "calibration", "test")]
    assert all_ids[0].isdisjoint(all_ids[1]) and all_ids[0].isdisjoint(all_ids[2])
    assert all_ids[1].isdisjoint(all_ids[2])
    assert sum(len(partitions[name]) for name in ("train", "calibration", "test")) == 10
    assert report["accepted_cases"] == 10
    assert len(partitions["train_sampled"]) >= len(partitions["train"])
    assert max(Counter(case["id"] for case in partitions["train_sampled"]).values()) <= 3
    assert all(len(case["questions"]) in (3, 5) for case in partitions["calibration"] + partitions["test"])


def test_legacy_or_unverified_snapshot_does_not_enter_training():
    rows = [row for n in range(1, 5) for row in request(n)]
    rows[0]["metadata"]["snapshot_version"] = None
    partitions, report = prepare(rows, min_cases=3, min_positive_per_split=0)
    assert report["accepted_cases"] == 3
    assert report["rejected_requests"]["snapshot_not_verified"] == 1
    assert all(case["id"] != "request_1" for group in partitions.values() for case in group)


def test_positive_case_needs_five_level_length_label_and_population_gate():
    rows = request(1)
    rows.pop()
    for row in rows:
        row["metadata"]["request_question_count"] = 5
    with pytest.raises(ValueError, match="reply_length_missing"):
        build_case(rows)
    rows = [row for row in request(1) if row["metadata"]["question_id"] != "recipient_choice"]
    for row in rows:
        row["metadata"]["request_question_count"] = 5
    with pytest.raises(ValueError, match="recipient_choice_missing"):
        build_case(rows)
    negatives = [row for n in range(1, 11) for row in request(n, join=False)]
    with pytest.raises(ValueError, match="need at least 1000 complete"):
        prepare(negatives)
    with pytest.raises(ValueError, match="join positives and negatives"):
        prepare(negatives, min_cases=3)
