"""Adapter for the pinned upstream Agent; no generated text or act-head gating."""

import hashlib
import importlib.util
import json
from pathlib import Path

_snapshot_spec = importlib.util.spec_from_file_location(
    "laya_decision_snapshot", Path(__file__).resolve().parents[2] / "core" / "decision_snapshot.py"
)
_snapshot_module = importlib.util.module_from_spec(_snapshot_spec)
_snapshot_spec.loader.exec_module(_snapshot_module)
parse_snapshot = _snapshot_module.parse_snapshot
validate_snapshot = _snapshot_module.validate_snapshot

UPSTREAM_REVISION = "573e5b62696ba441230cd6be71d593331b5d23af"
WEIGHTS_REVISION = "1c5edc17a7acd8701df6fc341c0d179f1c62c982"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def input_digest(state, questions, version):
    return hashlib.sha256(canonical([state, questions, version]).encode()).hexdigest()


def task_name(qid):
    for prefix in ("target", "recipient"):
        if qid.startswith(prefix + "."):
            return prefix
    return qid


def prepare_state(tokenizer, state, questions, max_len=1024, head_max_len=192):
    """Return one tokenizer-bounded text snapshot shared by teacher and student.

    Reserve the entire head budget plus special tokens. Important fields come
    first; history is retained from its recent end. Reject oversized critical
    content instead of silently removing the current message or candidates.
    """
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 128:
        raise ValueError("questions must contain 1..128 entries")
    for q in questions.values():
        if (
            not isinstance(q, dict)
            or q.get("type") not in ("choice", "noul", "score")
            or not isinstance(q.get("instructions"), (str, dict))
            or not q.get("instructions")
        ):
            raise ValueError("invalid typed question")
        if q["type"] != "noul" and (
            not isinstance(q.get("criteria"), (dict, list)) or not 2 <= len(q["criteria"]) <= 24
        ):
            raise ValueError("criteria must contain 2..24 options")
    budget = max_len - head_max_len - 8
    if budget < 16:
        raise ValueError("invalid checkpoint sequence budget")

    def encode(text):
        return tokenizer(text, add_special_tokens=False)["input_ids"]

    validate_snapshot(state, questions)
    parsed = parse_snapshot(state)
    if parsed is None:
        text = state if isinstance(state, str) else canonical(state)
        if len(encode(text)) > budget:
            raise ValueError("unstructured state exceeds tokenizer budget; send a structured snapshot")
        return text

    # Drop whole optional history entries, never JSON tokens or critical evidence.
    # Candidate maps, current messages, reply relationships and timestamps remain
    # untouched. The complete source is separately retained by the collector.
    containers = [parsed] + [parsed[k] for k in ("conversation", "routing_semantics", "turn_context")
                             if isinstance(parsed.get(k), dict)]
    reply_ids = set()
    def find_replies(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("reply_to", "reply_to_id") and isinstance(item, str) and item:
                    reply_ids.add(item)
                find_replies(item)
        elif isinstance(value, list):
            for item in value:
                find_replies(item)
    find_replies(parsed)
    for container in containers:
        for key in ("background", "history", "recent_messages"):
            if len(encode(canonical(parsed))) <= budget:
                break
            value = container.get(key)
            if isinstance(value, list):
                index = 0
                while index < len(value) and len(encode(canonical(parsed))) > budget:
                    if isinstance(value[index], dict) and value[index].get("message_id") in reply_ids:
                        index += 1
                        continue
                    removed = value.pop(index)
                    try:
                        validate_snapshot(parsed, questions)
                    except ValueError:
                        value.insert(index, removed)
                        index += 1
            elif isinstance(value, str):
                # Legacy free-text history can lose its old prefix, but the JSON
                # string is always re-serialized rather than sliced after encoding.
                low, high = 0, len(value)
                while low < high:
                    middle = (low + high + 1) // 2
                    container[key] = value[-middle:] if middle else ""
                    if len(encode(canonical(parsed))) <= budget:
                        low = middle
                    else:
                        high = middle - 1
                container[key] = value[-low:] if low else ""
    text = canonical(parsed)
    if len(encode(text)) > budget:
        raise ValueError("critical context exceeds tokenizer budget")
    validate_snapshot(parsed, questions)
    return text


class LayaBackend:
    def __init__(self, checkpoint, device="cuda"):
        from laya import Agent

        self.path = Path(checkpoint)
        self.agent = Agent(str(self.path), device=device)
        self.version = self.path.name
        calibration = self.path / "calibration.json"
        self.calibration = json.loads(calibration.read_text()) if calibration.exists() else {}

    @property
    def device(self):
        return str(self.agent.device)

    def prepare(self, state, questions):
        from laya.common import render_options

        prepared = prepare_state(
            self.agent.tok,
            state,
            questions,
            self.agent.cfg.get("max_len", 1024),
            self.agent.cfg.get("head_max_len", 192),
        )

        # Upstream clips both question instructions and individual options.
        # Reject such a question rather than giving the teacher a fuller task
        # than the student's actual token sequence.
        for question in questions.values():
            internal = self.agent._to_internal(question)
            encode = self.agent.tok
            mask = self.agent.tok.mask_token
            options = [
                len(encode(" " + option.replace(mask, " "), add_special_tokens=False)["input_ids"])
                for option in render_options(internal)
            ]
            instruction = len(
                encode(f"{internal['t']} question: {internal['ins'].replace(mask, ' ')}", add_special_tokens=False)[
                    "input_ids"
                ]
            )
            head_budget = self.agent.cfg.get("head_max_len", 192)
            if any(length > 48 for length in options) or head_budget - sum(length + 1 for length in options) < max(
                16, instruction
            ):
                raise ValueError("question exceeds tokenizer head budget")
        return prepared

    def logits(self, state, questions):
        import torch
        from laya.common import build_sequence, collate_items, QTYPES, render_options

        items = []
        for q in questions.values():
            internal = self.agent._to_internal(q)
            seq, markers = build_sequence(
                self.agent.tok,
                state,
                internal,
                self.agent.cfg.get("max_len", 1024),
                self.agent.cfg.get("head_max_len", 192),
            )
            if len(markers) != len(render_options(internal)):
                raise ValueError("options exceed checkpoint head budget")
            items.append({"ids": seq, "markers": markers, "qtype": QTYPES[internal["t"]]})
        batch = collate_items([items], self.agent.tok.pad_token_id)
        with (
            torch.inference_mode(),
            torch.autocast(
                device_type=self.agent.device.type, dtype=self.agent.dtype, enabled=self.agent.device.type == "cuda"
            ),
        ):
            logits, _ = self.agent.model(
                *(
                    batch[k].to(self.agent.device)
                    for k in ("input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype")
                )
            )
        return [logits[i, : len(item["markers"])].float().cpu().tolist() for i, item in enumerate(items)]

    def predict(self, state, questions):
        import numpy as np

        answers = {}
        for (qid, q), z in zip(questions.items(), self.logits(state, questions)):
            bucket = self.calibration.get(f"{task_name(qid)}:{len(z)}", {})
            z = np.array(z) / float(bucket.get("temperature", 1.0))
            p = np.exp(z - z.max())
            p /= p.sum()
            answer = {"type": q["type"], "confidence": float(p.max()), "calibrated": bool(bucket)}
            if q["type"] == "choice":
                keys = list(q["criteria"])
                answer.update(choice=keys[int(p.argmax())], probabilities=dict(zip(keys, p.tolist())))
            elif q["type"] == "score":
                answer.update(
                    score=float((np.arange(len(p)) * p).sum()),
                    probabilities=dict(zip(map(str, range(len(p))), p.tolist())),
                )
            else:
                answer["noul"] = float(p[1])
            answers[qid] = answer
        return answers
