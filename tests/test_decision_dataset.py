import json
import sqlite3
import random
import re
import pytest

from astrbot_plugin_chat_dynamics.core.decision_dataset import (
    DecisionDataset,
    split_samples,
    split_report,
    _sample_groups,
)


def test_dataset_connection_closes_after_transaction(tmp_path):
    store = DecisionDataset(tmp_path / "closed.db")
    with store.connect() as db:
        assert db.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_pseudonyms_queue_restart_and_outcomes(tmp_path):
    path = tmp_path / "data.db"
    store = DecisionDataset(path)
    key = store.record_sample(
        "room",
        "reply",
        {"author_id": "alice", "text": "@alice hello", "reply_to": "msg1"},
        {"options": ["msg1", "none"]},
        teacher_label=None,
    )
    row = store.samples()[0]
    assert "alice" not in json.dumps(row)
    assert row["state"]["reply_to"] == row["candidates"]["options"][0]
    store.enqueue_label(key, {"provider": "test"})
    job = store.claim_label()
    assert store.claim_label() is None
    restarted = DecisionDataset(path)
    assert restarted.anonymous_id("room") == row["session_id"]
    assert not restarted.complete_label(key, "wrong", "yes", "llm")
    assert restarted.complete_label(key, job["token"], "yes", "llm")
    restarted.update_outcome(key, {"sent": False})
    assert restarted.samples()[0]["teacher_label"] == "yes"


def test_retention_and_delete_invalidate_derivatives(tmp_path):
    store = DecisionDataset(tmp_path / "data.db")
    for session in ["one", "two"]:
        store.record_sample(session, "reply", "hello", [], teacher_label="no", teacher_model="llm", created_at=0)
    target = tmp_path / "export.jsonl"
    assert store.export(target) == 2
    assert store.delete_session("one") == 1
    assert not target.exists()
    assert store.purge(now=31 * 86400) == 1


def test_split_groups_sessions_and_near_duplicates():
    rows = [
        dict(id=str(i), session_id=str(i), created_at=i, state=f"unique {i * i * 9191} topic" + chr(65 + i) * 20)
        for i in range(20)
    ]
    rows += [dict(id="copy", session_id="new", created_at=99, state=rows[0]["state"])]
    groups = split_samples(rows)
    locations = {r["id"]: name for name, items in groups.items() for r in items}
    assert locations["0"] == locations["copy"] == "test"
    assert set(locations) == {r["id"] for r in rows}


def test_anonymize_is_idempotent_preserves_technical_ids(tmp_path):
    store = DecisionDataset(tmp_path / "data.db", secret="explicit")
    snapshot = dict(provider_id="provider", question_id="q", author_id="alice", state="@alice says hello")
    masked = store.anonymize(snapshot)
    assert masked == store.anonymize(masked)
    assert masked["provider_id"] == "provider" and masked["question_id"] == "q"
    assert DecisionDataset(tmp_path / "data.db").anonymous_id("alice") == masked["author_id"]


def test_human_review_preserves_teacher_label_and_validates_candidates(tmp_path):
    store = DecisionDataset(tmp_path / "data.db")
    key = store.record_sample(
        "room", "join", "context", {"type": "noul"}, teacher_label={"noul": 1}, teacher_model="llm"
    )
    store.update_human_label(key, False)
    sample = store.samples()[0]
    assert sample["teacher_label"] == {"noul": 1}
    assert sample["metadata"]["human_label"] == {"noul": False}
    with pytest.raises(ValueError):
        store.update_human_label(key, 0.7)
    with pytest.raises(ValueError):
        store.update_human_label("missing", True)


def test_queue_rechecks_opt_in_sessions_after_restart(tmp_path):
    store = DecisionDataset(tmp_path / "data.db")
    ids = {}
    for session in ("withdrawn", "allowed"):
        ids[session] = store.record_sample(session, "join", "context", {"type": "noul"})
        store.enqueue_label(ids[session])
    restarted = DecisionDataset(tmp_path / "data.db")
    assert restarted.claim_label(session_ids=[]) is None
    assert restarted.claim_label(session_ids=["allowed"])["sample_id"] == ids["allowed"]
    assert restarted.claim_label(session_ids=["allowed"]) is None
    assert restarted.claim_label()["sample_id"] == ids["withdrawn"]


def test_one_room_episodes_split_without_breaking_continuous_context():
    rows = []
    for episode, text in enumerate(("astronomy planets", "database transactions", "pottery glazing")):
        for message in range(2):
            rows.append(
                dict(
                    id=f"{episode}-{message}",
                    session_id="same-room",
                    created_at=episode * 3600 + message * 200,
                    state=text + str(message),
                )
            )
    groups = split_samples(reversed(rows))
    assert all(len(group) == 2 for group in groups.values())
    for group in groups.values():
        assert len({r["id"].split("-")[0] for r in group}) == 1
    assert groups["test"][0]["id"].startswith("2-")


def test_near_duplicates_union_across_separate_episodes():
    rows = [
        dict(id=str(i), session_id="one", created_at=i * 3600, state=text)
        for i, text in enumerate(("same exact snapshot", "completely different", "same exact snapshot"))
    ]
    groups = split_samples(rows)
    locations = {r["id"]: key for key, group in groups.items() for r in group}
    assert locations["0"] == locations["2"]


def test_summary_zero_catalog_and_raw_gaps(tmp_path):
    from astrbot_plugin_chat_dynamics.core.decision_tasks import TASKS

    store = DecisionDataset(tmp_path / "coverage.db")
    report = store.summary()
    assert set(TASKS) <= set(report["tasks"])
    assert report["total"] == 0
    join = report["tasks"]["join"]
    assert join["class_counts"] == {"false": 0, "true": 0}
    assert join["collection_gap"] == {
        "samples": 500,
        "classes": {"false": 50, "true": 50},
        "basis": "raw_collection_not_test",
    }
    assert report["tasks"]["persona.warmth"]["class_counts"] == dict.fromkeys(["0", "1", "2", "3", "4"], 0)
    assert report["tasks"]["topic"]["class_counts"] == {"KEEP": 0}


def test_static_catalog_matches_runtime_vocabulary():
    from astrbot_plugin_chat_dynamics.core.decision_catalog import TASK_LABELS
    from astrbot_plugin_chat_dynamics.core.decision_tasks import TASKS, persona_questions
    from astrbot_plugin_chat_dynamics.core.jev_decision import ACTIONS, STATES, LENGTHS, REASONS
    from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode
    from astrbot_plugin_chat_dynamics.core.turn_decisions import wts_questions, gate_questions

    assert set(TASK_LABELS) == set(TASKS)
    for name, labels in [("action", ACTIONS), ("state", STATES), ("length", LENGTHS), ("reason", REASONS)]:
        assert TASK_LABELS[name] == tuple(labels)
    assert set(TASK_LABELS["vibe"]) == {mode.value for mode in GroupChatMode}
    for name, question in {**persona_questions(), **wts_questions(), **gate_questions()}.items():
        assert TASK_LABELS[name] == tuple(str(i) for i in range(len(question["criteria"])))


def test_topic_dynamic_labels_do_not_imply_per_index_class_minimum(tmp_path):
    store = DecisionDataset(tmp_path / "coverage.db")
    store.record_sample(
        "room",
        "topic",
        "context",
        {"type": "choice", "criteria": {"KEEP": "keep", "topic_0": "current candidate"}},
        teacher_label={"choice": "topic_0"},
        teacher_model="teacher",
    )
    task = store.summary()["tasks"]["topic"]
    assert task["dynamic"] is True
    assert task["observed_labels"] == ["topic_0"]
    assert task["class_counts"] == {"KEEP": 0, "topic_0": 1}
    assert task["collection_gap"]["classes"] == {}


def test_old_queue_migration_and_persistent_fairness(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE labels(sample TEXT PRIMARY KEY,status TEXT DEFAULT 'pending',lease_until REAL DEFAULT 0,token TEXT,attempts INTEGER DEFAULT 0)"
        )
    store = DecisionDataset(path)
    keys = []
    for i in range(7):
        key = store.record_sample("room", "join", "context", {"type": "noul"})
        keys.append(key)
        store.enqueue_label(key, priority=0 if i == 0 else 10, reason="coverage")
    for i in range(1, 5):
        job = DecisionDataset(path).claim_label(session_ids=["room"])
        assert job["sample_id"] == keys[i] and job["priority"] == 10
        assert job["reason"] == "coverage"
    assert DecisionDataset(path).claim_label(session_ids=[]) is None
    assert DecisionDataset(path).claim_label()["sample_id"] == keys[0]
    assert DecisionDataset(path).claim_label()["sample_id"] == keys[5]


def test_audit_flags_preserve_labels_and_wait_for_complete_target_labels(tmp_path):
    store = DecisionDataset(tmp_path / "audit.db")
    rows = [
        ("join", {"type": "noul"}, False, True),
        ("action", {"type": "choice", "criteria": {"ignore": "ignore", "reply": "reply"}}, "reply", "ignore"),
        ("target", {"type": "noul"}, None, False),
    ]
    ids = []
    for task, question, label, student in rows:
        ids.append(
            store.record_sample(
                "room",
                task,
                "context",
                question,
                teacher_label=label,
                teacher_model="teacher" if label is not None else None,
                student_prediction=student,
                request_id="request",
                request_question_count=3,
            )
        )
    report = store.audit_request("request")
    assert set(report["flags"]) == {"join_action_conflict", "critical_teacher_student_disagreement"}
    before = [r["teacher_label"] for r in store.samples()]
    assert before == [False, "reply", None]
    store.enqueue_label(ids[-1])
    job = store.claim_label()
    store.complete_label(ids[-1], job["token"], False, "teacher")
    after = store.samples()
    assert [r["teacher_label"] for r in after] == [False, "reply", False]
    assert all("affirmative_without_target" in r["metadata"]["quality_flags"] for r in after)
    assert store.summary()["quality_flags"]["affirmative_without_target"] == 3


def test_request_audit_uses_expression_index_and_only_updates_matching_rows(tmp_path):
    store = DecisionDataset(tmp_path / "indexed.db")
    for request in ["wanted", "unrelated"]:
        store.record_sample(
            "room",
            "join",
            "context",
            {"type": "noul"},
            teacher_label=False,
            teacher_model="teacher",
            student_prediction=True,
            request_id=request,
        )
    with store.connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM samples WHERE json_valid(payload) AND json_extract(payload,'$.metadata.request_id') IN (?,?)",
            ("wanted", store.anonymous_id("wanted")),
        ).fetchall()
    assert any("USING INDEX sample_request" in row[-1] for row in plan)
    assert store.audit_request("wanted")["samples"] == 1
    unrelated = [r for r in store.samples() if r["metadata"]["request_id"] == store.anonymous_id("unrelated")][0]
    assert "quality_flags" not in unrelated["metadata"]


def test_indexed_grouping_matches_exhaustive_jaccard_components():
    rng = random.Random(482)
    texts = ["", "a", "b", "a", "漢語測試", "漢語測試"]
    for _ in range(40):
        base = "".join(rng.choices("abcdefghijklmnop", k=rng.randrange(8, 100)))
        texts.extend([base, base[:-1] + "z", base + base[:4]])
    rows = [dict(id=str(i), session_id=str(i % 17), created_at=i * 3600, state=text) for i, text in enumerate(texts)]
    parent = list(range(len(rows)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    grams = []
    for i, row in enumerate(rows):
        text = re.sub(r"\W+", "", json.dumps(row["state"], ensure_ascii=False, sort_keys=True).lower())
        gram = {text[j : j + 3] for j in range(max(1, len(text) - 2))}
        for j, previous in enumerate(grams):
            if len(gram & previous) / max(1, len(gram | previous)) >= 0.85:
                parent[root(i)] = root(j)
        grams.append(gram)
    expected = {}
    for i, row in enumerate(rows):
        expected.setdefault(root(i), set()).add(row["id"])
    actual = {frozenset(r["id"] for r in group) for group in _sample_groups(rows)}
    assert actual == {frozenset(group) for group in expected.values()}


def test_split_report_contains_only_counts_labels_and_dates():
    rows = [
        dict(
            id=str(i),
            session_id="one",
            created_at=i * 86400,
            state=state,
            task_id="join",
            candidates={"type": "noul"},
            teacher_label=bool(i % 2),
            teacher_model="teacher",
        )
        for i, state in enumerate(["private astronomy", "private databases", "private cooking"])
    ]
    parts = split_samples(rows)
    report = split_report(rows, parts)
    assert (report["groups"], report["max_group_size"], report["sessions"]) == (3, 1, 1)
    assert report["partitions"]["train"]["tasks"]["join"]["class_counts"] == {"false": 1}
    assert report["partitions"]["test"]["date_start"] == "1970-01-03"
    assert "private" not in json.dumps(report)
    assert split_report([], split_samples([]))["partitions"]["test"]["date_start"] is None
    assert split_report(rows, {"validation": rows[:1]})["partitions"]["validation"]["samples"] == 1


def test_pure_persona_duplicates_do_not_bridge_two_room_conversations():
    rows = [
        dict(id="chat-a", session_id="a", created_at=0, state="astronomy telescope", task_id="join"),
        dict(id="chat-b", session_id="b", created_at=0, state="database transaction", task_id="join"),
    ]
    for room in ["a", "b"]:
        rows.append(
            dict(
                id="persona-" + room,
                session_id=room,
                created_at=1,
                task_id="persona.warmth",
                state={"character_card": "Friendly helpful character", "persona_fingerprint": "same"},
            )
        )
    groups = {frozenset(r["id"] for r in group) for group in _sample_groups(rows)}
    assert groups == {frozenset(["chat-a"]), frozenset(["chat-b"]), frozenset(["persona-a", "persona-b"])}
    parts = split_samples(rows)
    locations = {row["id"]: part for part, group in parts.items() for row in group}
    assert locations["persona-a"] == locations["persona-b"]


def test_persona_with_conversation_remains_conservatively_grouped():
    mixed = {"character_card": "Helpful", "persona_fingerprint": "same", "conversation": ["context"]}
    rows = [
        dict(id="chat-a", session_id="a", created_at=0, state="astronomy telescope", task_id="join"),
        dict(id="chat-b", session_id="b", created_at=0, state="database transaction", task_id="join"),
    ]
    for room in ["a", "b"]:
        rows.append(dict(id="persona-" + room, session_id=room, created_at=1, task_id="persona.warmth", state=mixed))
    assert len(_sample_groups(rows)) == 1


def test_pure_persona_cannot_near_duplicate_bridge_nonpersona_state():
    state = {"character_card": "Same snapshot", "persona_fingerprint": "same"}
    rows = [
        dict(id="persona", session_id="a", created_at=0, state=state, task_id="persona.warmth"),
        dict(id="chat", session_id="a", created_at=1, state=state, task_id="join"),
    ]
    assert len(_sample_groups(rows)) == 2


def test_prepared_persona_json_objects_are_isolated_but_extra_context_is_not():
    from astrbot_plugin_chat_dynamics.core.decision_dataset import _pure_persona_sample

    text = '{"character_card":"Friendly"}\n{"persona_fingerprint":"same"}'
    row = dict(id="p", session_id="a", created_at=0, task_id="persona.warmth", state=text)
    assert _pure_persona_sample(row)
    for invalid in [
        text + '\n{"conversation":[]}',
        text + " untrusted tail",
        '{"character_card":"Friendly"}\n{"conversation":[]}',
        '{"character_card":"Friendly"}\n{"character_card":"overwritten"}',
        '[{"character_card":"Friendly"}]',
    ]:
        assert not _pure_persona_sample(dict(row, state=invalid))
    rows = [
        row,
        dict(row, id="p2", session_id="b"),
        dict(id="chat-a", session_id="a", created_at=1, task_id="join", state="astronomy"),
        dict(id="chat-b", session_id="b", created_at=1, task_id="join", state="databases"),
    ]
    groups = {frozenset(r["id"] for r in g) for g in _sample_groups(rows)}
    assert groups == {frozenset(["p", "p2"]), frozenset(["chat-a"]), frozenset(["chat-b"])}


def test_teacher_prompt_versions_count_only_labeled_samples(tmp_path):
    store = DecisionDataset(tmp_path / "versions.db")
    store.record_sample("room", "join", "context", {"type": "noul"}, teacher_label=False, teacher_model="old")
    key = store.record_sample("room", "join", "context", {"type": "noul"})
    store.enqueue_label(key)
    job = store.claim_label()
    assert store.complete_label(key, job["token"], True, "new", teacher_prompt_version="structured-v2")
    store.record_sample("room", "join", "context", {"type": "noul"}, teacher_prompt_version="unlabeled-version")
    assert store.summary()["teacher_prompt_versions"] == {"legacy-v1": 1, "structured-v2": 1}
    assert store.samples()[1]["metadata"]["teacher_prompt_version"] == "structured-v2"


def test_review_quarantine_preserves_flags_and_labels_across_restart(tmp_path):
    path = tmp_path / "review.db"
    store = DecisionDataset(path)
    key = store.record_sample(
        "room",
        "join",
        "context",
        {"type": "noul"},
        teacher_label=False,
        teacher_model="teacher",
        request_id="r",
        quality_flags=["existing_flag"],
    )
    store.enqueue_label(key)
    job = store.claim_label()
    assert store.mark_review(key, "missing_context")
    assert not store.complete_label(key, job["token"], True, "teacher")
    store.audit_request("r")
    restarted = DecisionDataset(path)
    assert restarted.claim_label() is None
    sample = restarted.samples()[0]
    assert sample["teacher_label"] is False
    assert set(sample["metadata"]["quality_flags"]) == {"existing_flag", "insufficient_evidence"}
    assert sample["metadata"]["review_reason"] == "missing_context"
    restarted.update_human_label(key, True)
    assert restarted.samples()[0]["teacher_label"] is False
    assert restarted.summary()["queue"]["review"] == 1
    assert restarted.summary()["quality_flags"]["insufficient_evidence"] == 1


def test_transient_annotation_failure_remains_retryable_and_exhausted_can_be_requeued(tmp_path):
    store = DecisionDataset(tmp_path / "retry.db")
    state = {"conversation": {"text": "请帮我看看", "messages": []}}
    transient_id = store.record_sample("room", "join", state, {"type": "noul"},
                                       snapshot_version="1")
    store.enqueue_label(transient_id, {"provider_id": "teacher", "question_id": "join"})
    for _ in range(6):
        job = store.claim_label()
        assert job and job["sample_id"] == transient_id
        assert store.fail_label(transient_id, job["token"], "timeout", transient=True) == "deferred"
    assert store.claim_label()["sample_id"] == transient_id

    failed_id = store.record_sample("room", "join", state, {"type": "noul"},
                                    snapshot_version="1")
    store.enqueue_label(failed_id, {"provider_id": "teacher", "question_id": "join"})
    # Release the still-leased transient task so claims advance to this sample.
    for _ in range(5):
        job = store.claim_label()
        assert job and job["sample_id"] == failed_id
        status = store.fail_label(failed_id, job["token"], "ValueError")
    assert status == "failed"
    assert store.claim_label() is None
    assert store.requeue_exhausted() == 1
    job = store.claim_label()
    assert job and job["sample_id"] == failed_id and job["attempts"] == 1


def test_requeue_exhausted_recovers_only_expired_valid_snapshot(tmp_path):
    store = DecisionDataset(tmp_path / "legacy-retry.db")
    valid = store.record_sample("room", "join", {"conversation": {"text": "现在的问题"}},
                                {"type": "noul"}, snapshot_version="1")
    old = store.record_sample("room", "join", "old input", {"type": "noul"})
    for sample in (valid, old):
        store.enqueue_label(sample, {"provider_id": "teacher", "question_id": "join"})
    with store.connect() as db:
        db.execute("UPDATE labels SET status='leased',attempts=5,lease_until=0")
    assert store.requeue_exhausted(snapshot_version="1") == 1
    job = store.claim_label()
    assert job and job["sample_id"] == valid and job["attempts"] == 1


def test_requeue_skips_invalid_front_row_without_starving_valid_row(tmp_path):
    store = DecisionDataset(tmp_path / "skip-invalid.db")
    state = {"conversation": {"text": "现在的问题", "messages": []}}
    bad = store.record_sample("room", "join", state, {"type": "noul"}, snapshot_version="1")
    good = store.record_sample("room", "join", state, {"type": "noul"}, snapshot_version="1")
    for sample in (bad, good):
        store.enqueue_label(sample, {"provider_id": "teacher", "question_id": "join"})
    with store.connect() as db:
        row = db.execute("SELECT payload FROM samples WHERE id=?", (bad,)).fetchone()
        payload = json.loads(row[0])
        payload["state"]["conversation"]["truncated"] = True
        db.execute("UPDATE samples SET payload=? WHERE id=?", (json.dumps(payload), bad))
        db.execute("UPDATE labels SET status='failed',attempts=5")
    assert store.requeue_exhausted(snapshot_version="1", limit=1) == 1
    assert store.claim_label()["sample_id"] == good


def test_export_task_version_filter_is_optional(tmp_path):
    store = DecisionDataset(tmp_path / "versions.db")
    for version in ["1", "2"]:
        store.record_sample(
            "room",
            "join",
            "context",
            {"type": "noul"},
            teacher_label=True,
            teacher_model="teacher",
            task_version=version,
        )
    path = tmp_path / "export.jsonl"
    assert store.export(path, task_version=2) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["task_version"] == "2"
    assert store.export(path) == 2


def test_summary_valid_pairs_classes_sessions_and_utc_days(tmp_path):
    store = DecisionDataset(tmp_path / "coverage.db")
    for room, label, student, stamp in [
        ("room", True, {"type": "noul", "noul": 0.8}, 0),
        ("room", False, None, 86399),
        ("second", 0.6, {"type": "noul", "noul": 0.9}, 86400),
        ("second", True, {"type": "noul", "noul": float("nan")}, 86401),
    ]:
        store.record_sample(
            room,
            "join",
            "private text",
            {"type": "noul"},
            teacher_label=label,
            teacher_model="teacher",
            student_prediction=student,
            created_at=stamp,
        )
    report = store.summary()
    join = report["tasks"]["join"]
    assert store.coverage_summary()["join"]["class_counts"] == join["class_counts"]
    assert join["samples"] == join["teacher_labels"] == 4
    assert join["student_predictions"] == 3
    assert join["valid_teacher_labels"] == 3 and join["valid_pairs"] == 1
    assert join["sessions"] == 2
    assert join["days"] == {"1970-01-01": 2, "1970-01-02": 2}
    assert join["class_counts"] == {"false": 1, "true": 2}
    assert join["collection_gap"]["samples"] == 497
    assert "private text" not in json.dumps(report)


def test_summary_choice_candidates_and_invalid_score_not_counted(tmp_path):
    store = DecisionDataset(tmp_path / "coverage.db")
    store.record_sample(
        "room",
        "custom",
        "context",
        {"type": "choice", "criteria": {"a": "one", "b": "two"}},
        teacher_label={"choice": "a"},
        teacher_model="teacher",
        student_prediction={"choice": "invalid"},
    )
    store.record_sample(
        "room",
        "topic_relevance",
        "context",
        {"type": "score", "criteria": ["0", "1", "2", "3", "4"]},
        teacher_label={"score": 2},
        teacher_model="teacher",
        student_prediction={"score": 4.9},
    )
    report = store.summary()["tasks"]
    assert report["custom"]["class_counts"] == {"a": 1, "b": 0}
    assert report["custom"]["valid_pairs"] == report["topic_relevance"]["valid_pairs"] == 0


def test_empty_character_card_is_still_context_free_persona():
    from astrbot_plugin_chat_dynamics.core.decision_dataset import _pure_persona_sample

    assert _pure_persona_sample(
        {"task_id": "persona.warmth", "state": {"character_card": "", "persona_fingerprint": "v1"}}
    )
    assert _pure_persona_sample(
        {"task_id": "persona.warmth", "state": '{"character_card":""}\n{"persona_fingerprint":"v1"}'}
    )
    assert not _pure_persona_sample({"task_id": "persona.warmth", "state": {"character_card": "", "conversation": {}}})
