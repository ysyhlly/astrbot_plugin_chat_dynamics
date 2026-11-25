"""Resolve context-dependent praise inside the decision layer, using cited outcomes."""

import asyncio
import re
import time

from . import member_boundary, tone_context
from .native_decision import selected_decision_client


# These cues only nominate a turn for review. Neither the cue nor a bad
# outcome alone determines whether the speaker meant praise or irony.
_APPRAISAL = re.compile(
    r"(?:真|挺|太|很|够)(?:是|的|够|有)?(?:行(?:[啊呀呢哇哦诶，。！？!?.\s]|$)|棒|强|"
    r"贴心|周到|靠谱|专业|及时|水平|效率|能耐|本事)|"
    r"(?:干|做|办|做事)得(?:真|挺|可真)?(?:好|漂亮)|厉害|了不起|神操作|有两下子|天才|"
    r"\b(?:nice work|well done|great job|brilliant)\b", re.I
)
_QUOTATION = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"\n]*\"")
_PLATFORM_QUOTE = re.compile(r"^\s*\[引用消息\(.*?\)\]\s*", re.S)
_EXPLICIT = re.compile(r"我(?:说|是)真的|我是认真的|不是(?:讽刺|反话|在挖苦)|我真心(?:谢谢|佩服|感谢)|"
                       r"我(?:最|特别|尤其)喜欢|(?:谢谢|感谢|多谢)你(?:还愿意|一直耐心|特意来)")
_FIT = {
    "type": "choice",
    "instructions": "比较末句表面的褒义与它所指的具体结果，两者的关系是什么？务必读实际反馈。",
    "criteria": {
        "match": "事情做成了，字面的肯定有事实支持",
        "conflict": "对方把事情搞砸或添麻烦，字面夸奖和结果相反",
        "unknown": "缺乏相关结果，事情无关，或没有表面褒义",
    },
}
_SOURCE_QUESTION = "哪一条前文直接报告了评价对象所做这件事的真实结果？仅有指示、计划、转述无关事件均选 none。"
_MEANING = {
    "type": "choice",
    "instructions": "When the final speaker evaluates the other person, does a literal positive reading make sense "
                    "given what actually happened to the same task? A report about a different person, an intention, "
                    "or merely quoted words is not evidence.",
    "criteria": {"sincere": "The last speaker gives deserved approval after a helpful result.",
                 "ironic": "The last speaker uses approval to point out a concrete failure or inconvenience.",
                 "uncertain": "No evidence for either interpretation, or not this kind of utterance."},
}
_CONSEQUENCE = {
    "type": "choice", "instructions": "只按这件事最后给说话人带来的具体影响来判断。不清楚选 unknown。",
    "criteria": {"help": "可核验的结果有用、顺利、解决了原问题", "harm": "可核验的结果添乱、失败或造成了损失",
                 "unknown": "没有报告实际后果、无关或相互矛盾"},
}
_VERDICTS = {"match": ("sincere_praise", "literal", "praise"),
             "conflict": ("ironic_praise", "sarcasm", "criticizes_action")}


def _answer(raw, choices):
    if not isinstance(raw, dict) or raw.get("type") != "choice":
        return None, 0.0
    key, score = raw.get("choice"), raw.get("confidence")
    if not isinstance(key, str) or key not in choices or type(score) not in (float, int) or not 0 <= score <= 1:
        return None, 0.0
    return key, float(score)


def _candidate(words, stance):
    own = _QUOTATION.sub("", words).strip()
    if (not own or len(words) > 180 or _EXPLICIT.search(own)
            or (own != words.strip() and not _APPRAISAL.search(own))):
        return False
    if stance[0] in {"reports", "defense", "personal_attack", "venting"} and stance[1] >= 0.8:
        return False
    # A directly negative utterance and a complaint can also sound sarcastic.
    # This particular check is only about words that can read as praise.
    return bool(_APPRAISAL.search(own) or (len(own) <= 48 and stance[0] == "praise" and stance[1] >= 0.4))


def _linked(row, current, anchors):
    recipients = row.get("mentioned_user_ids", ())
    return (row.get("basis") in {"quoted", "quote_chain", "dialogue"}
            or row["author"] == current["speaker_id"]
            or (row["author"] in current.get("mentions", ()) and not recipients)
            or current["speaker_id"] in row.get("mentioned_user_ids", ())
            or row.get("reply_to") in anchors)


def _one_line(text):
    return " ".join(str(text).split())


def _transcript(rows, turn, current):
    # The prompt schema and question alternatives remain trusted. Quoted user
    # content is confined to one data line per message, bound to its real ID.
    def clean_name(value):
        return _one_line(value).replace("：", "·").replace(":", "·")[:32]
    names = {row["author"]: clean_name(row.get("author_name", "")) for row in rows}
    names[turn.author] = clean_name(turn.messages[-1].author_name)
    display = {}
    for index, (uid, name) in enumerate(names.items(), 1):
        display[uid] = (f"〔{index}〕{name or '成员'}" if not name or name.startswith("〔")
                        or list(names.values()).count(name) > 1 else name)
    history = [f"（{i}）{display[row['author']]}：{_one_line(row['text'])}" for i, row in enumerate(rows, 1)]
    return "上文：\n" + "\n".join(history) + "\n当前要理解的发言：\n" + display[turn.author] + "：" + _one_line(current["words"])


def _current_allowed(host, event, turn, current):
    if not turn.messages or any(m.author != turn.author for m in turn.messages):
        return False
    uids = [turn.author]
    for msg in turn.messages:
        quoted = getattr(msg.semantics, "quoted_author_id", "")
        if msg.reply_to and not quoted:
            return False
        uids.extend(uid for uid in (quoted, *msg.mentioned_users) if uid)
    frame = {"frame_id": current["message_id"], "author_uid": turn.author, "source_uids": list(dict.fromkeys(uids)),
             "provenance_complete": bool(turn.author), "kind": "event", "text": current["words"]}
    allowed, boundary = member_boundary.filter_frames(host, event, [frame], purpose="role_input")
    if not boundary.available and (member_boundary.fail_closed(boundary) or boundary.service is not None):
        return False
    return any(row.get("frame_id") == frame["frame_id"] and row.get("text") == frame["text"]
               for row in allowed if isinstance(row, dict))


def _result(reason, calls, mode, stance):
    return {"choice": "unclear", "confidence": 0.0, "reason": reason, "calls": calls,
            "primary_mode": mode[0], "primary_mode_confidence": mode[1],
            "primary_stance": stance[0], "primary_stance_confidence": stance[1]}


async def review(host, event, turn, state, answers, client, stamp, deadline, floor):
    """Return a decision and its source, or abstain without changing participation."""
    from .conversation_intent import INTENT_CRITERIA
    exchange = state.get("tone_exchange") or {}
    current = exchange.get("current") or {}
    if (not isinstance(answers, dict) or not current or current.get("excerpt")
            or not turn.messages or current.get("speaker_id") != turn.author
            or current.get("message_id") != turn.messages[-1].message_id or current.get("words") != turn.text):
        return None
    mode = _answer(answers.get("expression_mode"), INTENT_CRITERIA["expression_mode"])
    stance = _answer(answers.get("speaker_stance"), INTENT_CRITERIA["speaker_stance"])
    spoken = current["words"]
    if spoken.lstrip().startswith("[引用消息"):
        header = _PLATFORM_QUOTE.match(spoken)
        if not header or not current.get("quotes") or not current.get("quoted_author_id"):
            return None
        spoken = spoken[header.end():]
        # A nested or malformed platform quote does not establish authorship.
        if "[引用消息" in spoken or not _APPRAISAL.search(_QUOTATION.sub("", spoken)):
            return None
    if not _candidate(spoken, stance):
        return None
    result = _result("insufficient_context", 0, mode, stance)
    rows = exchange.get("preceding") or []
    anchors = {r["message_id"] for r in rows if r.get("basis") in {"quoted", "quote_chain", "dialogue"}}
    if not rows or not any(_linked(row, current, anchors) for row in rows):
        return result
    frame = {"session_key": turn.session_key, "speaker_id": turn.author, "trigger_message_id": current["message_id"],
             "tone_sources": tone_context.source_signatures(rows)}

    def still_current():
        try:
            return (stamp is not None and not getattr(host, "_shutting_down", False)
                    and selected_decision_client(host) is client and client.tone_selection() == stamp
                    and _current_allowed(host, event, turn, current)
                    and tone_context.for_reply(turn, frame, host, event)[0] is not None)
        except Exception:
            return False

    if not still_current():
        return {**result, "reason": "source_or_route_changed"}
    timeout = min(4.0, deadline - time.monotonic())
    if timeout < 0.25:
        return {**result, "reason": "time_budget"}
    options = {"none": "前文没说结果、只有打算或条件、事件无关或互相矛盾"}
    evidence = {f"e{i}": row for i, row in enumerate(rows, 1)}
    options.update({key: "前文第" + str(i) + "条" for i, key in enumerate(evidence, 1)})
    questions = {"fit": _FIT, "evidence": {"type": "choice", "instructions": _SOURCE_QUESTION, "criteria": options},
                 "speaker_intent": _MEANING, "consequence": _CONSEQUENCE}
    result["calls"] = 1
    try:
        tone_kwargs = {"group_id": turn.session_key} if getattr(client, "is_systemone_router", False) else {}
        direct = await asyncio.wait_for(
            client.evaluate_tone(state=_transcript(rows, turn, {**current, "words": spoken}), questions=questions,
                                 timeout=timeout, selection=stamp, **tone_kwargs), timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        direct = None
    if not isinstance(direct, dict):
        return {**result, "reason": "review_unavailable"}
    if not still_current():
        return {**result, "reason": "source_or_route_changed"}
    fit, fit_score = _answer(direct.get("fit"), _FIT["criteria"])
    source, source_score = _answer(direct.get("evidence"), options)
    meaning, meaning_score = _answer(direct.get("speaker_intent"), _MEANING["criteria"])
    consequence, consequence_score = _answer(direct.get("consequence"), _CONSEQUENCE["criteria"])
    result.update(fit=fit, fit_confidence=fit_score, evidence_confidence=source_score,
                  meaning=meaning, meaning_confidence=meaning_score,
                  consequence=consequence, consequence_confidence=consequence_score)
    row = evidence.get(source)
    if (not row or not _linked(row, current, anchors) or fit not in _VERDICTS
            or "\n[...]\n" in row["text"] or "[内容已截断]" in row["text"]):
        return {**result, "reason": "no_cited_outcome"}
    threshold = max(0.8, floor)
    expected = ("sincere", "help") if fit == "match" else ("ironic", "harm")
    if ((meaning_score >= threshold and meaning != expected[0])
            or (consequence_score >= threshold and consequence != expected[1])):
        return {**result, "reason": "conflicting_evidence"}
    method = "outcome_fit"
    strength = min(fit_score, source_score)
    if fit_score < threshold and (meaning, consequence) == expected:
        # A difficult wording can still be resolved by a direct pragmatic
        # judgment corroborated by a separately classified, cited outcome.
        method = "meaning_and_consequence"
        strength = min(meaning_score, consequence_score, source_score)
    if strength < threshold:
        return {**result, "reason": "low_confidence"}
    return {**result, "choice": _VERDICTS[fit][0], "confidence": strength, "reason": "cited_outcome",
            "method": method, "source_message_id": row["message_id"]}


def project(answers, result):
    """Project the accepted reading into the existing semantic fields."""
    if result is None:
        return answers
    _, mode, stance = _VERDICTS[result["fit"]] if result["reason"] == "cited_outcome" else ("", "unclear", "unclear")
    return {**answers,
            "expression_mode": {"type": "choice", "choice": mode, "confidence": result["confidence"]},
            "speaker_stance": {"type": "choice", "choice": stance, "confidence": result["confidence"]}}
