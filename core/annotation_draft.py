"""Draft reply labels for a window of messages, for a human to accept or correct.

Why the drafts are not labels: the learning layer treats human annotation as
ground truth for one specific reason — the reply decision is itself a model
judgement, so a label produced by the same kind of judgement would be graded by
the thing it is supposed to measure. A draft is useful precisely because a human
still has to accept it: drafts live in their own key, they never look like labels
to anything downstream, and when the accepted values came from a draft the record
says so (`accepted_from: ai`).

The draft covers the two labels a human cannot read off the trace — "should the
bot have replied" and "was this addressed to the bot" — and deliberately not the
topic label, which the replay page already assigns with one click from the
candidate list. A draft that also guessed the topic would be answering the easy
half.

What the model is shown is the window in order, with the bot messages included as
context but excluded from drafting, and **without** what the host decided: a
draft that has been shown the answer is not a draft, it is a rubber stamp.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

DRAFT_SCHEMA_VERSION = 1
DRAFT_PROMPT_VERSION = 1

MAX_MESSAGES = 60
DEFAULT_DRAFTS = 20
MAX_TEXT = 500
MAX_REASON = 200
MAX_INVENTED = 5

_NUMBER = re.compile("[0-9]+(?:[.][0-9]+)?")
FENCE = chr(96) * 3


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


# ---- the batch ----------------------------------------------------------

def build_batch(nodes: Sequence[Any], annotated: Mapping[str, Any], *,
                limit: int = DEFAULT_DRAFTS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The window in order, and which of its messages are up for drafting.

    `/nodes/` is the host DAG recent nodes (oldest first is what the reader wants
    to read). Every message is included for context; only the ones that are not
    the bot own and have no label yet are draftable, and only the newest
    `limit` of those are asked for.
    """
    window: list[dict[str, Any]] = []
    draftable: list[str] = []
    cap = max(1, min(MAX_MESSAGES, int(limit) * 3 if limit else DEFAULT_DRAFTS * 3))
    selected = list(nodes)[-cap:]
    for order, node in enumerate(selected):
        msg_id = _text(getattr(node, "msg_id", ""), 128)
        text = _text(getattr(node, "text", ""), MAX_TEXT)
        if not msg_id or not text:
            continue
        metadata = getattr(node, "metadata", None)
        metadata = metadata if isinstance(metadata, Mapping) else {}
        user_id = _text(getattr(node, "user_id", ""), 64)
        window.append({
            "msg_id": msg_id,
            "order": order,
            "text": text,
            "mentions_bot": bool(getattr(node, "mentioned_users", None)),
            "quotes": _text(getattr(node, "reply_to_id", ""), 64),
            "from_bot": bool(metadata.get("is_bot")),
            "user": user_id[:8],
        })
    for item in window:
        if item["from_bot"] or item["msg_id"] in annotated:
            continue
        draftable.append(item["msg_id"])
    wanted = set(draftable[-max(1, min(MAX_MESSAGES, int(limit or DEFAULT_DRAFTS))):])
    batch = []
    for item in window:
        row = dict(item)
        row["draft_this"] = item["msg_id"] in wanted
        batch.append(row)
    stats = {"window": len(window), "draftable": len(draftable), "asked": len(wanted)}
    return batch, stats


# ---- the prompt ---------------------------------------------------------

SYSTEM_PROMPT = """你在帮 ChatDynamics 的运营给群聊消息做**预标注**。你的输出是草稿，会由人逐条复核后决定是否采纳。

对每一条标记了 draft_this=true 的消息，回答两个问题：
1. expected_reply：机器人当时**应该回复**这条消息吗？
2. bot_targeted：这条消息是**在对机器人说话**吗（点名、@、回复机器人、直接提问机器人）？

硬性规则：
1. 只处理 draft_this=true 的条目，msg_id 原样引用；不要新增、不要漏，其他条目一律不要出现在结果里。
2. 你看到的是按时间顺序排列的窗口片段，不是完整对话；上下文不足时把 confidence 压低，并在 reason 里说清缺什么。
3. 不要猜「标注者想要什么」：给独立判断。你没有看到机器人当时是怎么判的，也不要去推测。
4. expected_reply 与 bot_targeted 必须是布尔值；confidence 是 0~1 的数；reason 一句话，不要复述规则。
5. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。

输出结构：
{"rows": [{"msg_id": "原样引用", "expected_reply": true, "bot_targeted": false,
           "confidence": 0.7, "reason": "一句话依据"}]}"""


def build_prompt(batch: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for one window."""
    body = json.dumps({"messages": list(batch)}, ensure_ascii=False, indent=1)
    return SYSTEM_PROMPT, "消息窗口（按时间顺序）：\n" + body + "\n\n只输出 JSON 对象。"


# ---- reading the reply --------------------------------------------------

def _extract_json(text: str) -> Any:
    """The first JSON object in a reply, fenced or bare; None when there is none."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    if FENCE in stripped:
        for part in stripped.split(FENCE)[1::2]:
            candidates.append(part.strip().removeprefix("json").strip())
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, round(float(value), 3)))


def parse_drafts(text: str, batch: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Validate a model reply into drafts; None when nothing survives.

    Only the messages the batch asked about can become drafts: an id that was not
    asked for is not a draft, it is the model answering a question nobody put to
    it, and it is reported rather than stored.
    """
    payload = _extract_json(text)
    if not isinstance(payload, Mapping):
        return None
    asked = {str(item.get("msg_id")): item for item in batch if item.get("draft_this")}
    drafts: dict[str, dict[str, Any]] = {}
    invented: list[str] = []
    undecided: list[str] = []
    for raw in payload.get("rows") or []:
        if not isinstance(raw, Mapping):
            continue
        msg_id = raw.get("msg_id")
        if isinstance(msg_id, str):
            msg_id = msg_id.strip()
        elif isinstance(msg_id, int) and not isinstance(msg_id, bool):
            # Chat platforms often use numeric ids; a model that drops the
            # quotes is still answering the question it was asked.
            msg_id = str(msg_id)
        else:
            msg_id = ""
        if not msg_id or msg_id in drafts:
            continue
        if msg_id not in asked:
            if msg_id not in invented and len(invented) < MAX_INVENTED:
                invented.append(msg_id[:64])
            continue
        reply = _bool(raw.get("expected_reply"))
        targeted = _bool(raw.get("bot_targeted"))
        if reply is None and targeted is None:
            undecided.append(msg_id)
            continue
        draft: dict[str, Any] = {
            "confidence": _confidence(raw.get("confidence")),
            "reason": _text(raw.get("reason"), MAX_REASON),
            "msg_id": msg_id,
        }
        if reply is not None:
            draft["expected_reply"] = reply
        if targeted is not None:
            draft["bot_targeted"] = targeted
        drafts[msg_id] = draft
    if not drafts:
        return None
    return {
        "draft_schema_version": DRAFT_SCHEMA_VERSION,
        "prompt_version": DRAFT_PROMPT_VERSION,
        "drafts": drafts,
        "drafted": len(drafts),
        "undecided": undecided[:MAX_INVENTED * 2],
        "invented": invented,
    }


__all__ = [
    "DEFAULT_DRAFTS", "DRAFT_PROMPT_VERSION", "DRAFT_SCHEMA_VERSION", "MAX_MESSAGES",
    "SYSTEM_PROMPT", "build_batch", "build_prompt", "parse_drafts",
]
