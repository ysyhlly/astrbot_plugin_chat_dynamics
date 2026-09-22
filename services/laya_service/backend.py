"""Adapter for the pinned upstream Agent; no generated text or act-head gating."""

import hashlib
import json
from pathlib import Path

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

    if isinstance(state, str):
        try:
            parsed = json.loads(state)
            if isinstance(parsed, dict):
                state = parsed
        except (ValueError, TypeError):
            pass
    if isinstance(state, dict):
        critical_keys = (
            "current_message",
            "message",
            "text",
            "author",
            "explicit",
            "messages",
            "mentions",
            "reply_to",
            "persona",
            "character_card",
            "targets",
            "candidates",
            "topic_candidates",
        )
        critical = {k: state[k] for k in critical_keys if k in state}
        for nested_key in ("routing_semantics", "turn_context", "conversation"):
            nested = state.get(nested_key)
            if isinstance(nested, dict):
                critical[nested_key] = {k: nested[k] for k in critical_keys if k in nested}
        prefix = canonical(critical) if critical else ""
        remaining = {k: v for k, v in state.items() if k not in critical}
        for nested_key in ("routing_semantics", "turn_context", "conversation"):
            nested = state.get(nested_key)
            if isinstance(nested, dict):
                remainder = {k: v for k, v in nested.items() if k not in critical.get(nested_key, {})}
                if remainder:
                    remaining[nested_key] = remainder
        suffix = canonical(remaining)
        if len(encode(prefix)) > budget:
            raise ValueError("critical context exceeds tokenizer budget")
        room = budget - len(encode(prefix + "\n"))
        suffix_ids = encode(suffix)
        text = (
            prefix + "\n" + tokenizer.decode(suffix_ids[-max(0, room) :] if room > 0 else [], skip_special_tokens=True)
        )
    else:
        text = state if isinstance(state, str) else canonical(state)
        ids = encode(text)
        if len(ids) > budget:
            raise ValueError("unstructured state exceeds tokenizer budget; send a structured snapshot")
    # Decode/re-encode is not guaranteed to preserve token count for every tokenizer.
    while len(encode(text)) > budget:
        text = text[:-1]
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
