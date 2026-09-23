"""Prepare complete v2 decision requests for AgentJev without reading private keys.

Input is the plugin's per-question DecisionDataset JSONL export. Output is one
AgentJev sample per request; rejected requests are counted, never partly trained.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import random


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("agentjev_decision_dataset", ROOT / "core" / "decision_dataset.py")
_dataset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dataset)

LENGTH_OPTIONS = {
    "tiny": "一句很短的确认或回应。",
    "short": "一至三句话，适合普通群聊。",
    "medium": "简短但完整地解释一个问题。",
    "long": "较详细地分析或分步骤说明。",
    "very_long": "明确需要教程、复杂排障或代码分析。",
}
SUPPORTED = {"join", "recipient", "target", "action", "reply_length"}
CONFLICT_FLAGS = {"join_action_conflict", "affirmative_without_target", "decision_conflict"}


def _label(row: dict):
    question = row["candidates"]
    return _dataset._coverage_label(row.get("teacher_label"), question, hard=True)


def _state(row: dict) -> dict:
    metadata = row.get("metadata") or {}
    state = metadata.get("source_state", metadata.get("structured_snapshot", row.get("state")))
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except ValueError as error:
            raise ValueError("structured_snapshot_missing") from error
    if not isinstance(state, dict):
        raise ValueError("structured_snapshot_missing")
    conversation = state.get("conversation")
    if not isinstance(conversation, dict) or not isinstance(conversation.get("text"), str) or not conversation["text"].strip():
        raise ValueError("trigger_text_missing")
    return state


def _teacher_state(row: dict, source: dict) -> dict:
    """Use the state actually sent to the teacher, retaining source for audit."""
    if (row.get("metadata") or {}).get("tokenizer_prepared") is not True:
        return source
    prepared = row.get("state")
    if isinstance(prepared, str):
        try:
            prepared = json.loads(prepared)
        except ValueError as error:
            raise ValueError("teacher_state_missing") from error
    if not isinstance(prepared, dict):
        raise ValueError("teacher_state_missing")
    current = prepared.get("conversation")
    original = source["conversation"]
    if (not isinstance(current, dict) or
            any(current.get(key) != original.get(key) for key in ("text", "author", "messages")) or
            prepared.get("target_candidates") != source.get("target_candidates")):
        raise ValueError("prepared_evidence_mismatch")
    _dataset.validate_snapshot(prepared, {
        (row.get("metadata") or {}).get("question_id", row["task_id"]): row["candidates"]})
    return prepared


def _pack_state(state: dict) -> tuple[str, str]:
    """Put the triggering message before optional history for head truncation."""
    conversation = state["conversation"]
    semantic_keys = ("recipient_ids", "basis", "certainty", "quoted_message_id",
                     "quoted_author_id", "parent_message_id", "mentioned_user_ids",
                     "bot_is_addressee", "subject_is_bot", "routing_ambiguous")
    compact_messages = []
    for message in conversation.get("messages") or []:
        if not isinstance(message, dict):
            continue
        compact = {key: message[key] for key in
                   ("message_id", "author", "text", "reply_to", "mentioned_users", "timestamp")
                   if message.get(key) not in (None, "", [], ())}
        semantics = message.get("semantics")
        if isinstance(semantics, dict):
            compact["routing"] = {key: semantics[key] for key in semantic_keys
                                  if semantics.get(key) not in (None, "", [], ())}
        compact_messages.append(compact)
    current = {key: conversation[key] for key in ("text", "author", "explicit", "truncated")
               if key in conversation}
    current["messages"] = compact_messages
    packed = {"current": current}
    for key in ("target_candidates", "routing_semantics", "observations",
                "participation_policy", "previous_state", "persona"):
        if key in state:
            packed[key] = state[key]
    if "background" in conversation:
        packed["background"] = conversation["background"]
    packed["message_details"] = conversation.get("messages") or []
    extra = {key: value for key, value in conversation.items()
             if key not in ("text", "author", "explicit", "truncated", "messages", "background")}
    if extra:
        packed["conversation_extra"] = extra
    packed.update({key: value for key, value in state.items()
                   if key not in packed and key != "conversation"})
    return (json.dumps(packed, ensure_ascii=False, separators=(",", ":")),
            json.dumps({"current": current}, ensure_ascii=False, separators=(",", ":"))[:-1])


def _validate_agentjev_budget(tokenizer, current_prefix: str, questions: list[dict],
                              *, max_len: int, max_state_tokens: int) -> None:
    """Match upstream collate's per-question state budget before exporting."""
    def token_count(value):
        return len(tokenizer(value, add_special_tokens=False)["input_ids"])

    current_tokens = token_count("[STATE] " + current_prefix)
    for question in questions:
        q_tokens = token_count("\n[QUESTION] " + question["text"])
        candidate_tokens = max(min(token_count("\n[CANDIDATE] " + str(option)), max_len // 8)
                               for option in question["candidates"])
        budget_sq = max_len - candidate_tokens
        q_tokens = min(q_tokens, max(1, budget_sq - 8))
        state_budget = min(max_state_tokens, budget_sq - q_tokens)
        # A small margin covers tokenizer merges at the current/context boundary.
        if current_tokens + 8 > state_budget:
            raise ValueError("current_state_over_token_budget")


def _snapshot_quality(row: dict) -> None:
    metadata = row.get("metadata") or {}
    if str(row.get("task_version")) != "2":
        raise ValueError("not_v2")
    if str(metadata.get("snapshot_version")) != "1" or metadata.get("snapshot_usable") is False:
        raise ValueError("snapshot_not_verified")
    if (not isinstance(row.get("teacher_model"), str) or not row["teacher_model"]
            or not isinstance(metadata.get("teacher_prompt_version"), str)
            or not metadata["teacher_prompt_version"]):
        raise ValueError("teacher_provenance_missing")
    if metadata.get("teacher_prompt_version") == "legacy-v1":
        raise ValueError("legacy_teacher")
    flags = set(metadata.get("quality_flags") or ())
    if flags & (CONFLICT_FLAGS | {"insufficient_evidence"}):
        raise ValueError("quality_flags")
    if _label(row) is None:
        raise ValueError("teacher_label_missing")
    if not isinstance(row.get("session_id"), str) or not row["session_id"]:
        raise ValueError("session_id_missing")
    if type(row.get("created_at")) not in (int, float):
        raise ValueError("created_at_missing")
    state = _state(row)
    if row.get("task_id") == "target":
        candidate = (state.get("target_candidates") or {}).get(metadata.get("question_id"))
        if not isinstance(candidate, dict) or candidate.get("text_missing") is not False:
            raise ValueError("target_candidate_body_missing")
        if not isinstance(candidate.get("text"), str) or not candidate["text"].strip():
            raise ValueError("target_candidate_body_missing")
    _dataset.validate_snapshot(state, {metadata.get("question_id", row["task_id"]): row["candidates"]})


def _message_options(state: dict) -> dict[str, str]:
    conversation = state["conversation"]
    options = {}
    for item in (conversation.get("background") or []) + (conversation.get("messages") or []):
        if not isinstance(item, dict):
            continue
        key, body = item.get("message_id"), item.get("text")
        if isinstance(key, str) and key and isinstance(body, str) and body.strip():
            options[key] = f"{item.get('author', 'unknown')}: {body.strip()}"
    for item in (state.get("target_candidates") or {}).values():
        if not isinstance(item, dict) or item.get("text_missing") is not False:
            continue
        key, body = item.get("message_id"), item.get("text")
        if isinstance(key, str) and key and isinstance(body, str) and body.strip():
            options[key] = f"{item.get('author', 'unknown')}: {body.strip()}"
    return options


def _grouped_choice(rows: list[dict], family: str, state: dict) -> tuple[dict, str]:
    if any(len(row["metadata"]["question_id"].split(".")) != 2 for row in rows):
        raise ValueError(f"{family}_candidate_gap")
    indexed = sorted(rows, key=lambda row: int(row["metadata"]["question_id"].split(".")[1]))
    indices = [int(row["metadata"]["question_id"].split(".")[1]) for row in indexed]
    if indices != list(range(len(indexed))):
        raise ValueError(f"{family}_candidate_gap")
    messages = _message_options(state)
    options = {}
    positives = []
    for row in indexed:
        question = row["candidates"]
        if question.get("type") != "noul":
            raise ValueError(f"{family}_not_binary")
        candidate_id = (row.get("metadata") or {}).get("candidate_id")
        if family == "target":
            from_snapshot = (state.get("target_candidates") or {}).get(row["metadata"]["question_id"])
            if isinstance(from_snapshot, dict):
                candidate_id = from_snapshot.get("message_id")
            if not candidate_id:
                import re
                match = re.search(r"message\s+(\S+)\?", str(question.get("instructions", "")))
                candidate_id = match.group(1) if match else None
            if candidate_id not in messages:
                raise ValueError("target_candidate_body_missing")
            description = messages[candidate_id]
        else:
            description = (row.get("metadata") or {}).get("candidate_description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("recipient_candidate_body_missing")
        key = str(candidate_id or len(options))
        if key in options:
            raise ValueError(f"{family}_candidate_duplicate")
        options[key] = description
        if _label(row) == "true":
            positives.append(key)
    if len(positives) > 1:
        raise ValueError(f"{family}_multiple_positives")
    options["none"] = "以上候选都不是主要回应对象。"
    return {"type": "choice", "criteria": options,
            "instructions": f"选择本轮的主要{('用户' if family == 'recipient' else '消息')}回应对象；无法确定时选 none。"}, positives[0] if positives else "none"


def build_case(rows: list[dict], *, tokenizer=None, max_state_tokens=256,
               max_len=512) -> tuple[dict, bool]:
    if not rows:
        raise ValueError("empty_request")
    for row in rows:
        _snapshot_quality(row)
    metadata = rows[0]["metadata"]
    expected = metadata.get("request_question_count")
    if type(expected) is not int or expected != len(rows):
        raise ValueError("incomplete_request")
    source_state = _state(rows[0])
    state = _teacher_state(rows[0], source_state)
    canonical_source = json.dumps(source_state, ensure_ascii=False, sort_keys=True)
    canonical_teacher = json.dumps(state, ensure_ascii=False, sort_keys=True)
    packed_state, current_prefix = _pack_state(state)
    question_ids = [row["metadata"].get("question_id") for row in rows]
    if len(set(question_ids)) != len(rows) or any(not isinstance(q, str) for q in question_ids):
        raise ValueError("duplicate_question")
    for row in rows:
        if row["session_id"] != rows[0]["session_id"] or row["metadata"].get("request_id") != metadata.get("request_id"):
            raise ValueError("request_mismatch")
        row_source = _state(row)
        if json.dumps(row_source, ensure_ascii=False, sort_keys=True) != canonical_source:
            raise ValueError("state_mismatch")
        if json.dumps(_teacher_state(row, row_source), ensure_ascii=False, sort_keys=True) != canonical_teacher:
            raise ValueError("teacher_state_mismatch")
        if row["teacher_model"] != rows[0]["teacher_model"] or row["metadata"]["teacher_prompt_version"] != metadata["teacher_prompt_version"]:
            raise ValueError("teacher_mismatch")
    by_family = defaultdict(list)
    for row in rows:
        qid = row["metadata"]["question_id"]
        if qid == "recipient" and row["candidates"].get("type") == "noul":
            continue
        family = "recipient" if qid == "recipient_choice" else qid.split(".")[0]
        if family in SUPPORTED:
            by_family[family].append(row)
    if len(by_family["join"]) != 1 or len(by_family["action"]) != 1:
        raise ValueError("core_questions_missing")
    questions = []
    picks = {}
    for family in ("join", "recipient", "target", "action", "reply_length"):
        family_rows = by_family[family]
        if family in {"recipient", "reply_length"} and picks.get("join") == "false":
            continue
        if not family_rows:
            continue
        if family in {"recipient", "target"} and any("." in r["metadata"]["question_id"] for r in family_rows):
            spec, pick = _grouped_choice(family_rows, family, state)
        elif len(family_rows) == 1:
            spec, pick = family_rows[0]["candidates"], _label(family_rows[0])
        else:
            raise ValueError(f"{family}_ambiguous")
        kind = spec.get("type")
        if family in {"recipient", "target"} and kind != "choice":
            raise ValueError(f"{family}_choice_required")
        if family == "recipient":
            criteria = spec.get("criteria")
            conversation = state["conversation"]
            authors = {m.get("author") for m in (conversation.get("messages") or []) + (conversation.get("background") or [])
                       if isinstance(m, dict) and isinstance(m.get("text"), str) and m["text"].strip()}
            if isinstance(conversation.get("text"), str) and conversation["text"].strip():
                authors.add(conversation.get("author"))
            if (not isinstance(criteria, dict) or "none" not in criteria
                    or any(key != "none" and (key not in authors or not isinstance(description, str) or not description.strip())
                           for key, description in criteria.items())):
                raise ValueError("recipient_candidate_body_missing")
        if family == "reply_length" and (kind != "choice" or set(spec.get("criteria", {})) != set(LENGTH_OPTIONS)):
            raise ValueError("reply_length_rubric_mismatch")
        if kind == "noul":
            options = ["否：本轮不参与。", "是：本轮参与。"]
            target = "true" if pick == "true" else "false"
            distribution = [float(target == "false"), float(target == "true")]
        elif kind == "choice" and isinstance(spec.get("criteria"), dict):
            options = [f"{key}: {desc}" for key, desc in spec["criteria"].items()]
            distribution = [float(key == pick) for key in spec["criteria"]]
            target = pick
        else:
            raise ValueError(f"{family}_unsupported_question")
        if sum(distribution) != 1:
            raise ValueError(f"{family}_invalid_label")
        question_text = spec.get("instructions")
        if isinstance(question_text, dict):
            question_text = " ".join(str(v) for v in question_text.values())
        if not isinstance(question_text, str) or not question_text.strip():
            raise ValueError(f"{family}_question_missing")
        questions.append({"id": family, "text": question_text, "candidates": options,
                          "gold": {"distribution": distribution}, "supervision": "teacher",
                          "ordinal": False, "weight": 1.0})
        picks[family] = target
    if picks["join"] == "false" and picks["action"] != "ignore":
        raise ValueError("join_action_conflict")
    if picks["join"] == "true" and picks["action"] == "ignore":
        raise ValueError("join_action_conflict")
    if picks["join"] == "true" and picks.get("target") in {None, "none"}:
        raise ValueError("affirmative_without_target")
    if picks["join"] == "true" and "recipient" not in picks:
        raise ValueError("recipient_choice_missing")
    if picks["join"] == "true" and "reply_length" not in picks:
        raise ValueError("reply_length_missing")
    if tokenizer is not None:
        _validate_agentjev_budget(tokenizer, current_prefix, questions,
                                  max_len=max_len, max_state_tokens=max_state_tokens)
    request_id = metadata.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id_missing")
    # The model sees only state/question text, never teacher labels or IDs.
    result = {"id": request_id, "source": "chat_dynamics_v2", "state": packed_state,
              "questions": questions}
    return result, picks["join"] == "true"


def prepare(rows: list[dict], *, seed: int = 42, min_cases: int = 1000,
            min_positive_per_split: int = 10, tokenizer=None,
            max_state_tokens: int = 256, max_len: int = 512) -> tuple[dict[str, list[dict]], dict]:
    if min_cases < 3 or min_positive_per_split < 0:
        raise ValueError("invalid training gates")
    if type(max_state_tokens) is not int or max_state_tokens < 32:
        raise ValueError("invalid state token budget")
    if type(max_len) is not int or max_len < 128 or max_state_tokens > max_len:
        raise ValueError("invalid model sequence budget")
    requests = defaultdict(list)
    reasons = Counter()
    for row in rows:
        metadata = row.get("metadata") or {}
        request_id = metadata.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            reasons["request_id_missing"] += 1
            continue
        requests[(row.get("session_id"), request_id)].append(row)
    accepted = []
    positives = set()
    teacher_models = set()
    for group in requests.values():
        try:
            case, positive = build_case(group, tokenizer=tokenizer,
                                        max_state_tokens=max_state_tokens, max_len=max_len)
        except (KeyError, TypeError, ValueError) as error:
            reasons[str(error) if isinstance(error, ValueError) and str(error) else "malformed"] += 1
            continue
        accepted.append({"id": case["id"], "session_id": group[0]["session_id"],
                         "created_at": min(row["created_at"] for row in group),
                         "state": case["state"], "case": case})
        teacher_models.add(group[0]["teacher_model"])
        if positive:
            positives.add(case["id"])
    if len(teacher_models) > 1:
        raise ValueError(f"mixed teacher models in accepted cases: {sorted(teacher_models)}")
    if len(accepted) < min_cases:
        raise ValueError(f"need at least {min_cases} complete independent cases; found {len(accepted)}; rejected={dict(reasons)}")
    splits = _dataset.split_samples(accepted)
    if any(not splits[name] for name in ("train", "calibration", "test")):
        raise ValueError("need independent train/calibration/test groups")
    for name in ("train", "calibration", "test"):
        yes_count = sum(row["id"] in positives for row in splits[name])
        no_count = len(splits[name]) - yes_count
        if min(yes_count, no_count) < min_positive_per_split:
            raise ValueError(f"{name} needs at least {min_positive_per_split} join positives and negatives; found {yes_count} and {no_count}")
    output = {name: [row["case"] for row in subset] for name, subset in splits.items()}
    rng = random.Random(seed)
    natural_train = list(splits["train"])
    yes = [row for row in natural_train if row["id"] in positives]
    no = [row for row in natural_train if row["id"] not in positives]
    sampled = natural_train.copy()
    if yes and no:
        desired_yes = min(len(yes) * 3, round(len(no) * 0.35 / 0.65))
        extra_pool = yes * 2
        rng.shuffle(extra_pool)
        extra = extra_pool[:max(0, desired_yes - len(yes))]
        sampled.extend(extra)
        # Keep every natural negative; the report records when the 35% target is unreachable.
        rng.shuffle(sampled)
    output["train_sampled"] = [row["case"] for row in sampled]
    report = {"accepted_cases": len(accepted), "rejected_requests": dict(reasons),
              "splits": {name: {"cases": len(splits[name]),
                                "join_true": sum(row["id"] in positives for row in splits[name])}
                         for name in ("train", "calibration", "test")},
              "sampled_train_cases": len(sampled),
              "sampled_train_join_true": sum(row["id"] in positives for row in sampled),
              "seed": seed, "tokenizer_checked": tokenizer is not None,
              "max_state_tokens": max_state_tokens, "max_len": max_len}
    return output, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="v2 per-question JSONL export")
    parser.add_argument("output", type=Path, help="private output directory")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-cases", type=int, default=1000)
    parser.add_argument("--min-positive-per-split", type=int, default=10)
    parser.add_argument("--tokenizer", required=True,
                        help="AgentJev tokenizer path or model identifier; must match training")
    parser.add_argument("--max-state-tokens", type=int, default=256,
                        help="Same max_state_tokens value used by AgentJev training and inference")
    parser.add_argument("--max-len", type=int, default=512,
                        help="Same max_len value used by AgentJev training and inference")
    args = parser.parse_args()
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    partitions, report = prepare(rows, seed=args.seed, min_cases=args.min_cases,
                                 min_positive_per_split=args.min_positive_per_split,
                                 tokenizer=tokenizer, max_state_tokens=args.max_state_tokens,
                                 max_len=args.max_len)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, cases in partitions.items():
        (args.output / f"{name}.jsonl").write_text(
            "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
