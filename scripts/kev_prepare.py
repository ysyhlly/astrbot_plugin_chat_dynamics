"""Prepare evidence-checked Kev records from the private per-question export.

Old records may recover the bot identity only after the dataset's HMAC mapping
is verified against flagged bot messages. Output stays in a private directory.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__:
    from ..core import decision_dataset as _dataset
    from ..core.decision_state import build_decision_snapshot
    from .agentjev_prepare import _label, _snapshot_quality, _state, _teacher_state
else:
    sys.path.insert(0, str(ROOT.parent))
    from astrbot_plugin_chat_dynamics.core import decision_dataset as _dataset
    from astrbot_plugin_chat_dynamics.core.decision_state import build_decision_snapshot
    from astrbot_plugin_chat_dynamics.scripts.agentjev_prepare import (
        _label, _snapshot_quality, _state, _teacher_state,
    )

CORE_QUESTIONS = {"join", "action", "recipient", "recipient_choice", "reply_length"}


def _supported(qid: str) -> bool:
    return qid in CORE_QUESTIONS or qid.startswith("target.")


def build_case(rows: list[dict], *, verified_bot_id: str | None = None) -> tuple[dict, dict]:
    """Build one independent scenario with only its trustworthy labeled tasks."""
    if not rows:
        raise ValueError("empty_request")
    metadata = rows[0].get("metadata") or {}
    request_id = metadata.get("request_id")
    session_id = rows[0].get("session_id")
    if not isinstance(request_id, str) or not request_id or not session_id:
        raise ValueError("request_identity_missing")
    bot_id = metadata.get("bot_speaker_id") or verified_bot_id
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("bot_identity_missing")
    if verified_bot_id is not None and bot_id != verified_bot_id:
        raise ValueError("bot_identity_mismatch")
    source = _state(rows[0])
    canonical = json.dumps(source, ensure_ascii=False, sort_keys=True)
    all_specs = {}
    for row in rows:
        meta = row.get("metadata") or {}
        qid = meta.get("question_id")
        if (row.get("session_id") != session_id or meta.get("request_id") != request_id
                or (meta.get("bot_speaker_id") or verified_bot_id) != bot_id):
            raise ValueError("request_mismatch")
        if json.dumps(_state(row), ensure_ascii=False, sort_keys=True) != canonical:
            raise ValueError("state_mismatch")
        _teacher_state(row, source)
        if not isinstance(qid, str) or qid in all_specs:
            raise ValueError("duplicate_question")
        all_specs[qid] = row.get("candidates")
    if "join" not in all_specs:
        raise ValueError("join_spec_missing")

    labeled = {}
    skipped = Counter()
    teacher_identity = None
    for row in rows:
        qid = row["metadata"]["question_id"]
        if not _supported(qid):
            continue
        if row.get("teacher_label") is None:
            skipped["teacher_label_missing"] += 1
            continue
        try:
            _snapshot_quality(row)
        except (KeyError, TypeError, ValueError) as exc:
            skipped[str(exc) or "invalid_label"] += 1
            continue
        identity = (row.get("teacher_model"), row["metadata"].get("teacher_prompt_version"))
        if teacher_identity is None:
            teacher_identity = identity
        elif identity != teacher_identity:
            raise ValueError("mixed_teacher_provenance")
        spec = row["candidates"]
        kind = spec.get("type")
        label = _label(row)
        if kind == "noul":
            label = label == "true"
        elif kind == "score":
            label = int(label)
        if kind not in {"choice", "noul", "score"}:
            skipped["unsupported_type"] += 1
            continue
        labeled[qid] = {**{field: spec[field] for field in
                            ("type", "instructions", "criteria") if field in spec},
                        "label": label}
    if not labeled:
        raise ValueError("no_reliable_labels")
    if "join" in labeled and "action" in labeled:
        joined = labeled["join"]["label"]
        action = labeled["action"]["label"]
        if (joined and action == "ignore") or (not joined and action != "ignore"):
            raise ValueError("join_action_conflict")
    snapshot_specs = {"join": all_specs["join"]}
    snapshot_specs.update({key: all_specs[key] for key in labeled if key.startswith("target.")})
    snapshot = build_decision_snapshot({**source, "bot_speaker_id": bot_id}, snapshot_specs)
    if snapshot is None:
        raise ValueError("snapshot_invalid_or_over_budget")
    return ({"state": snapshot, "questions": labeled,
             "_meta": {"source": "chat_dynamics_v2", "group_id": request_id}},
            {"skipped_questions": dict(skipped), "question_count": len(labeled)})


def _finish_cases(cases: list[dict], *, input_rows: int, input_requests: int,
                  reasons: Counter, partial: Counter) -> tuple[dict[str, list[dict]], dict]:
    groups = _dataset._sample_groups(cases) if cases else []
    # Keep the actual episode/near-duplicate grouping in exported metadata so
    # downstream confidence intervals do not count related cases as independent.
    for group in groups:
        identity = sorted((str(row["session_id"]), str(row["id"])) for row in group)
        group_id = "episode_" + hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        for row in group:
            row["record"]["_meta"]["group_id"] = group_id
    n = len(groups)
    boundaries = [int(n * .60), int(n * .75), int(n * .85), n]
    names = ("train", "development", "calibration", "test")
    output = {}
    group_counts = {}
    start = 0
    for name, stop in zip(names, boundaries):
        selected = groups[start:stop]
        output[name] = [row["record"] for group in selected for row in group]
        group_counts[name] = {"independent_groups": len(selected),
                              "largest_group_size": max(map(len, selected), default=0)}
        start = stop
    counts = {name: {"cases": len(partition), **group_counts[name],
                     "questions": sum(len(record["questions"]) for record in partition),
                     "join_true": sum(record["questions"].get("join", {}).get("label") is True
                                      for record in partition),
                     "join_labeled": sum("join" in record["questions"] for record in partition),
                     "question_families": dict(Counter(
                         qid.split(".")[0] for record in partition
                         for qid in record["questions"]))}
              for name, partition in output.items()}
    ordered_sizes = sorted(len(group) for group in groups)
    positive_groups = sum(any(row["record"]["questions"].get("join", {}).get("label") is True
                              for row in group) for group in groups)
    report = {"input_rows": input_rows, "input_requests": input_requests,
              "accepted_cases": len(cases), "independent_groups": n,
              "groups_with_positive_join": positive_groups,
              "largest_group_sizes": ordered_sizes[-10:][::-1],
              "rejected_requests": dict(reasons),
              "skipped_individual_questions": dict(partial), "partitions": counts,
              "requires_tokenizer_admission": True,
              "snapshot_schema": "chat_decision_v1"}
    release_gaps = []
    for name in ("development", "calibration", "test"):
        if counts[name]["join_true"] < 10:
            release_gaps.append(f"{name}_join_positives_below_10")
    for family in ("recipient", "recipient_choice", "reply_length"):
        if counts["train"]["question_families"].get(family, 0) == 0:
            release_gaps.append(f"{family}_training_labels_missing")
    report["release_data_gate"] = {"ready": not release_gaps,
                                   "reasons": release_gaps}
    return output, report


def prepare(rows: list[dict], *, verified_bot_id: str | None = None) -> tuple[dict[str, list[dict]], dict]:
    grouped = defaultdict(list)
    reasons = Counter()
    for row in rows:
        meta = row.get("metadata") or {}
        request_id = meta.get("request_id")
        if isinstance(request_id, str) and request_id:
            grouped[(row.get("session_id"), request_id)].append(row)
        else:
            reasons["request_id_missing"] += 1
    cases = []
    partial = Counter()
    for (session_id, request_id), group in grouped.items():
        try:
            record, detail = build_case(group, verified_bot_id=verified_bot_id)
        except (KeyError, TypeError, ValueError) as exc:
            reasons[str(exc) or "malformed"] += 1
            continue
        cases.append({"id": request_id, "session_id": session_id,
                      "created_at": min(row["created_at"] for row in group),
                      "state": record["state"], "record": record})
        partial.update(detail["skipped_questions"])
    return _finish_cases(cases, input_rows=len(rows), input_requests=len(grouped),
                         reasons=reasons, partial=partial)


def prepare_sqlite(path: Path, raw_bot_id: str) -> tuple[dict[str, list[dict]], dict]:
    """Read only the original SQLite store; never export its HMAC secret or raw ID."""
    if not raw_bot_id or raw_bot_id.startswith("anon_"):
        raise ValueError("raw_bot_id_required")
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
        secret_row = db.execute("SELECT value FROM settings WHERE key='secret_hex'").fetchone()
        if secret_row is None:
            raise ValueError("dataset_secret_missing")
        secret = bytes.fromhex(secret_row[0])
        bot_id = "anon_" + hmac.new(secret, raw_bot_id.encode(), hashlib.sha256).hexdigest()[:32]
        observed = 0
        for (payload,) in db.execute(
            "SELECT payload FROM samples INDEXED BY sample_labeled "
            "WHERE json_extract(payload,'$.teacher_label') IS NOT NULL"
        ):
            row = json.loads(payload)
            source = (row.get("metadata") or {}).get("source_state") or {}
            conversation = source.get("conversation") or {}
            for message in (conversation.get("background") or []) + (conversation.get("messages") or []):
                if (isinstance(message, dict)
                        and (message.get("semantics") or {}).get("sender_is_bot") is True):
                    observed += 1
                    if not hmac.compare_digest(str(message.get("author")), bot_id):
                        raise ValueError("bot_identity_evidence_conflict")
        if observed == 0:
            raise ValueError("bot_identity_unverified")

        # A partial index makes the first pass small; the request index fetches
        # unlabeled siblings so that every original question stays in the case.
        request_ids = [value for (value,) in db.execute(
            "SELECT DISTINCT json_extract(payload,'$.metadata.request_id') "
            "FROM samples INDEXED BY sample_labeled "
            "WHERE json_extract(payload,'$.teacher_label') IS NOT NULL"
        ) if isinstance(value, str) and value]
        cases = []
        reasons, partial = Counter(), Counter()
        input_rows = 0
        input_requests = 0
        for request_id in request_ids:
            by_session = defaultdict(list)
            for (payload,) in db.execute(
                "SELECT payload FROM samples INDEXED BY sample_request "
                "WHERE json_valid(payload) AND json_extract(payload,'$.metadata.request_id')=?",
                (request_id,),
            ):
                row = json.loads(payload)
                input_rows += 1
                by_session[row.get("session_id")].append(row)
            for session_id, group in by_session.items():
                input_requests += 1
                try:
                    record, detail = build_case(group, verified_bot_id=bot_id)
                except (KeyError, TypeError, ValueError) as exc:
                    reasons[str(exc) or "malformed"] += 1
                    continue
                cases.append({"id": request_id, "session_id": session_id,
                              "created_at": min(row["created_at"] for row in group),
                              "state": record["state"], "record": record})
                partial.update(detail["skipped_questions"])
        partitions, report = _finish_cases(
            cases, input_rows=input_rows, input_requests=input_requests,
            reasons=reasons, partial=partial)
        report["bot_identity_evidence_occurrences"] = observed
        report["source"] = "sqlite_read_only"
        return partitions, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="private JSONL export or SQLite database")
    parser.add_argument("output", type=Path, help="private output directory")
    parser.add_argument("--raw-bot-id", help="raw bot ID; required for historical SQLite recovery")
    args = parser.parse_args()
    if args.input.suffix in {".sqlite3", ".sqlite"}:
        if not args.raw_bot_id:
            parser.error("--raw-bot-id is required with SQLite input")
        partitions, report = prepare_sqlite(args.input, args.raw_bot_id)
    else:
        rows = [json.loads(line) for line in args.input.open(encoding="utf-8") if line.strip()]
        partitions, report = prepare(rows)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, records in partitions.items():
        (args.output / f"{name}.jsonl").write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8")
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
