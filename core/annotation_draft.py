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
import math
import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

from .graph import ConversationNode

DRAFT_SCHEMA_VERSION = 1
DRAFT_PROMPT_VERSION = 2

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
                limit: int = DEFAULT_DRAFTS,
                bot_id: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The window in order, and which of its messages are up for drafting.

    `/nodes/` is the host DAG recent nodes (oldest first is what the reader wants
    to read). Every message is included for context; only the ones that are not
    the bot own and have no label yet are draftable, and only the newest
    `limit` of those are asked for.

    Ownership is `bot_id` against the node's author. That is the only rule: nothing
    in the plugin writes an `is_bot` node field, and the metadata check that used to
    stand beside it left every bot reply in the draftable set, so the model was asked
    whether the bot should have answered its own message.
    """
    bot_owner = str(bot_id or "")
    window: list[dict[str, Any]] = []
    draftable: list[str] = []
    cap = max(1, min(MAX_MESSAGES, int(limit) * 3 if limit else DEFAULT_DRAFTS * 3))
    selected = list(nodes)[-cap:]
    identities: dict[str, str] = {}
    def identity(value: Any) -> str | None:
        raw = str(value or "")
        if not raw:
            return None
        if bot_owner and raw == bot_owner:
            return "BOT"
        if raw not in identities:
            identities[raw] = "U" + str(len(identities) + 1)
        return identities[raw]

    authors = {str(getattr(n, "msg_id", "")): getattr(n, "user_id", "") for n in nodes}
    for n in selected:
        identity(getattr(n, "user_id", ""))
    for order, node in enumerate(selected):
        msg_id = _text(getattr(node, "msg_id", ""), 128)
        text = _text(getattr(node, "text", ""), MAX_TEXT)
        if not msg_id or not text:
            continue
        metadata = getattr(node, "metadata", None)
        metadata = metadata if isinstance(metadata, Mapping) else {}
        raw_user = str(getattr(node, "user_id", "") or "")
        mentions = [str(v) for v in (getattr(node, "mentioned_users", None) or []) if v]
        quoted_id = str(getattr(node, "reply_to_id", "") or "")
        quoted_author = metadata.get("quoted_author_id") or authors.get(quoted_id)
        window.append({
            "msg_id": msg_id,
            "order": order,
            "text": text,
            "mentions_bot": (bot_owner in mentions) if bot_owner else None,
            "mentions_anyone": bool(mentions),
            "mentioned_users": [identity(v) for v in mentions],
            "quoted_author": identity(quoted_author),
            "quotes": _text(getattr(node, "reply_to_id", ""), 64),
            "from_bot": bool(bot_owner) and raw_user == bot_owner,
            "user": identity(raw_user),
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


def build_batches(nodes: Sequence[Any], annotated: Mapping[str, Any], *,
                  limit: int = DEFAULT_DRAFTS, bot_id: str = "",
                  max_prompt_chars: int = 8000) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    """Split targets before serialization; identities remain stable across batches.

    Each target occurs exactly once. A target and its available quoted message
    take priority over nearest chronological context. No serialized JSON is cut.
    The budget includes both system and user prompts.
    """
    window, stats = build_batch(nodes, annotated, limit=limit, bot_id=bot_id)
    def fits(rows):
        return sum(map(len, build_prompt(rows))) <= max_prompt_chars
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    def required(targets):
        ids = {row["msg_id"] for row in targets}
        quotes = {row["quotes"] for row in targets}
        return [dict(row, draft_this=row["msg_id"] in ids) for row in window
                if row["msg_id"] in ids | quotes]
    for row in window:
        if not row["draft_this"]:
            continue
        candidate = current + [row]
        if current and not fits(required(candidate)):
            groups.append(current)
            current = []
        if not fits(required([row])):
            raise ValueError("prompt budget cannot fit target and quoted context")
        current.append(row)
    if current:
        groups.append(current)
    batches = []
    for targets in groups:
        rows = required(targets)
        included = {row["msg_id"] for row in rows}
        target_orders = [row["order"] for row in targets]
        neighbors = sorted(window, key=lambda row: (
            min(abs(row["order"] - order) for order in target_orders), row["order"]))
        for row in neighbors:
            if row["msg_id"] in included:
                continue
            candidate = sorted(rows + [dict(row, draft_this=False)], key=lambda r: r["order"])
            if fits(candidate):
                rows = candidate
                included.add(row["msg_id"])
        batches.append(rows)
    return batches, dict(stats, batches=len(batches))


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


def parse_drafts(text: str, batch: Sequence[Mapping[str, Any]], *, diagnostics: bool = False) -> dict[str, Any] | None:
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
    duplicate: list[str] = []
    invalid_confidence: list[str] = []
    seen: set[str] = set()
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    for raw in rows:
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
        if not msg_id:
            continue
        if msg_id in seen:
            duplicate.append(msg_id)
            continue
        seen.add(msg_id)
        if msg_id not in asked:
            if msg_id not in invented and len(invented) < MAX_INVENTED:
                invented.append(msg_id[:64])
            continue
        reply = _bool(raw.get("expected_reply"))
        targeted = _bool(raw.get("bot_targeted"))
        if reply is None and targeted is None:
            undecided.append(msg_id)
            continue
        confidence = raw.get("confidence")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            invalid_confidence.append(msg_id)
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
    if not drafts and not diagnostics:
        return None
    return {
        "draft_schema_version": DRAFT_SCHEMA_VERSION,
        "prompt_version": DRAFT_PROMPT_VERSION,
        "drafts": drafts,
        "drafted": len(drafts),
        "undecided": undecided[:MAX_INVENTED * 2],
        "invented": invented,
        "out_of_window": invented,
        "missing": [msg_id for msg_id in asked if msg_id not in seen],
        "duplicate": duplicate,
        "invalid_confidence": invalid_confidence,
    }


# ---- what the draft keeps of the message it was written about ------------
#
# A draft used to be only a proposal. The message it was about lived in the
# session graph, and once that graph no longer held it -- a restart, or the
# retention window moving past it -- the row stayed in the list as something
# nobody could ever accept: the draft survived, the label it was for did not.
#
# So a draft now carries the message it is about. Everything a label is written
# from is copied here while the message is still in memory: the routing block
# the record quotes, the frozen decision trace, the wall-clock stamp the page
# renders, and the text under the same switch that lets the page show it. The
# trace is the part that cannot be reconstructed afterwards -- nothing rebuilds
# a decision from live routing, which is exactly why a reroute must not rewrite
# history.

CONTEXT_KEY = "context"
CONTEXT_SCHEMA_VERSION = 1
MAX_CONTEXT_TEXT = 2000
CONTEXT_ROUTING_FIELDS = ("topic_id", "topic_confidence", "ambiguous", "topic_ambiguous",
                          "topic_status", "candidates", "topic_candidates",
                          "topic_candidate_evidence", "boundary_score", "evidence")
# Node metadata keys that stay outside the trace and can be written after the
# draft was generated (a delivery outcome lands when the reply is sent), kept
# under the same names the annotation record reads on a live node.
CONTEXT_META_FIELDS = ("outcome", "shadow_decision")


def build_context(node: Any, *, show_content: bool, wall_ts: float) -> dict[str, Any]:
    """What the message was, taken while it is still here.

    The stamp is a calendar stamp rather than the node's own clock because the
    graph stamps nodes with a monotonic clock: that number means nothing after
    the process that produced it is gone, and this one outlives it.
    """
    metadata = getattr(node, "metadata", None)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    routing = metadata.get("routing")
    routing = routing if isinstance(routing, Mapping) else {}
    trace = metadata.get("decision_trace") or metadata.get("trace_inputs") or {}
    text = str(getattr(node, "text", "") or "")[:MAX_CONTEXT_TEXT]
    context: dict[str, Any] = {
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "wall_ts": float(wall_ts or 0.0),
        # Text follows the same switch that lets the approval page show it: with
        # the switch off the snapshot holds no body for anything to leak.
        "text": text if show_content else "",
        "routing": {key: deepcopy(routing[key]) for key in CONTEXT_ROUTING_FIELDS if key in routing},
    }
    if isinstance(trace, Mapping) and trace:
        context["trace"] = deepcopy(dict(trace))
    for key in CONTEXT_META_FIELDS:
        if metadata.get(key) is not None:
            context[key] = deepcopy(metadata[key])
    return context


def context_node(msg_id: str, context: Any) -> ConversationNode | None:
    """Rebuild the message a recovered label is written from, or None.

    The result is not a live node and nothing may treat it as one: it is the
    snapshot above re-shaped into what TopicAnnotations.save() reads. The trace
    goes back under trace_inputs for the same reason the runtime snapshot keeps
    it there -- that is the half an annotation rebuilds from after a restart, and
    a label written from a snapshot should be the record a restart would have
    produced.

    A snapshot that is only a body and a stamp still counts. A message the
    routing pipeline never saw has no routing and no trace on the node either,
    so the record built from the snapshot says exactly what the record built from
    the live node would have said. `None` is reserved for the draft that kept
    nothing at all.
    """
    if not isinstance(context, Mapping):
        return None
    metadata: dict[str, Any] = {}
    routing = context.get("routing")
    if isinstance(routing, Mapping) and routing:
        metadata["routing"] = deepcopy(dict(routing))
    trace = context.get("trace")
    if isinstance(trace, Mapping) and trace:
        metadata["trace_inputs"] = deepcopy(dict(trace))
    for key in CONTEXT_META_FIELDS:
        if context.get(key) is not None:
            metadata[key] = deepcopy(context[key])
    return ConversationNode(
        str(msg_id), "", str(context.get("text") or "")[:MAX_CONTEXT_TEXT],
        float(context.get("wall_ts") or 0.0), metadata=metadata)


def public_draft(draft: Any) -> dict[str, Any]:
    """A draft as the pages read it: the proposal, without the snapshot.

    The context is what keeps a draft acceptable long after its message left the
    graph. It is not something a page renders, and a frozen decision trace per
    row would grow every list response by orders of magnitude.
    """
    if not isinstance(draft, Mapping):
        return {}
    return {key: value for key, value in draft.items() if key != CONTEXT_KEY}


__all__ = [
    "CONTEXT_KEY", "CONTEXT_META_FIELDS", "CONTEXT_ROUTING_FIELDS", "CONTEXT_SCHEMA_VERSION",
    "DEFAULT_DRAFTS", "DRAFT_PROMPT_VERSION", "DRAFT_SCHEMA_VERSION", "MAX_CONTEXT_TEXT",
    "MAX_MESSAGES", "SYSTEM_PROMPT", "build_batch", "build_batches", "build_context",
    "build_prompt", "context_node", "parse_drafts", "public_draft",
]
