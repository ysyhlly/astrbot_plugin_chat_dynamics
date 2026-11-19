"""Focused unit tests for evaluative reading (irony/praise) decision review."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core import evaluative_reading as reading
from astrbot_plugin_chat_dynamics.core import member_boundary, tone_context
from astrbot_plugin_chat_dynamics.core.context_retrieval import ContextEvidence
from astrbot_plugin_chat_dynamics.core.message_semantics import MessageSemantics
from astrbot_plugin_chat_dynamics.core.native_decision import NativeDecisionUnavailable
from astrbot_plugin_chat_dynamics.core.systemone.client import SystemOneError
from astrbot_plugin_chat_dynamics.core.systemone.routing import RoutedSystemOneClient
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext

BOT, USER, OTHER, PEER = "1001", "1002", "1003", "1004"
NAMES = {BOT: "安安", USER: "小满", OTHER: "阿澈", PEER: "小麦"}


def _message(mid, who, words, when, target="", parent=None):
    mentions = (target,) if target else ()
    sem = MessageSemantics(
        who,
        who == BOT,
        mentions,
        "platform" if target else "unknown",
        "confirmed" if target else "unknown",
        mid,
        mentions,
        (),
        (),
        "unknown",
        quoted_message_id=parent.message_id if parent else "",
        quoted_author_id=parent.author if parent else "",
        bot_is_addressee=target == BOT,
    )
    return MessageSnapshot(
        mid,
        who,
        words,
        parent.message_id if parent else "",
        semantics=sem,
        source_text=words,
        timestamp=when,
        mentioned_users=mentions,
        author_name=NAMES[who],
        platform="fixture",
    )


def _case(name):
    text = "你可真行"
    prior = []
    quoted = None
    if name in ("door_good", "door_bad", "pending", "off_topic"):
        bad = name == "door_bad"
        first = _message("m1", USER, "说明牌写着蓝柄开门，红柄会清空记录。你看该拉哪个？", 1, BOT)
        quoted = _message("m2", BOT, "拉红柄。" if bad else "拉蓝柄。", 2, USER, first)
        outcome = {
            "door_good": "蓝柄一拉，门开了，通关记录也在。",
            "door_bad": "我照着拉了红柄，门没开，进度还真的全清了。",
            "pending": "我还没拉，等小麦到了再试，现在不知道结果。",
            "off_topic": "隔壁服又崩了，我在别的游戏里丢了存档。",
        }[name]
        prior = [first, quoted, _message("m3", OTHER if name == "off_topic" else USER, outcome, 3)]
    elif name == "parcel_bad":
        quoted = _message("p7", BOT, "邀请函我已经填好地址，寄去新展馆了。", 1, USER)
        reported = _message(
            "p9", USER, "刚收到退件，你把整箱都寄去了旧展厅，十二封邀请函全耽误了。", 2
        )
        prior = [quoted, reported, _message("p11", OTHER, "我先去倒杯水。", 3)]
    elif name == "peer_only":
        prior = [
            _message("m1", OTHER, "我把你的压缩包删了。", 1, PEER),
            _message("m2", PEER, "删错了，那个包里全是原图。", 2, OTHER),
        ]
    elif name != "empty":
        raise KeyError(name)
    current = _message("m4", USER, text, 4, BOT, quoted)
    background = []
    nearby = []
    for row in prior:
        if quoted and row.message_id == quoted.message_id:
            background.append(
                replace(row, context_evidence=(ContextEvidence("reply", "m4", 1, ("platform_quote",)),))
            )
        elif quoted and row.message_id == quoted.reply_to:
            background.append(row)
        else:
            nearby.append(row)
    return TurnContext(
        "fixture:GroupMessage:room",
        USER,
        text,
        (current,),
        tuple(background),
        0,
        0,
        4,
        True,
        wake_kind="at",
        bot_id=BOT,
        snapshot_at=4,
        nearby_context=tuple(nearby),
    )


def choice(name, score):
    return {"type": "choice", "choice": name, "confidence": score}


def paired(
    fit="conflict",
    score=0.94,
    evidence="e3",
    evidence_score=0.97,
    intent="ironic",
    intent_score=0.88,
    consequence="harm",
    consequence_score=0.91,
):
    return {
        "fit": choice(fit, score),
        "evidence": choice(evidence, evidence_score),
        "speaker_intent": choice(intent, intent_score),
        "consequence": choice(consequence, consequence_score),
    }


class NativeReview:
    def __init__(self, response):
        self.response, self.calls, self.stamp, self.during = response, [], 1, None

    def tone_selection(self):
        return self.stamp

    async def evaluate_tone(self, **kwargs):
        self.calls.append(kwargs)
        if self.during:
            self.during()
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _fixture(name, response):
    t = _case(name)
    client = NativeReview(response)
    host = NS(_runtime_config=NS(decision_backend="jev", autonomous_blocking_required=False), jev=client)
    rows = tone_context.tone_context(t, host, None)
    state = {"tone_exchange": tone_context.exchange(t.payload(), rows)}
    answers = {
        "expression_mode": choice("literal", 0.96),
        "speaker_stance": choice("praise", 0.89),
        "join": {"type": "noul", "noul": 0.44},
        "action": choice("ignore", 0.91),
        "recipient": choice("other", 0.92),
    }
    return t, client, host, state, answers


async def _run(parts, seconds=10):
    t, client, host, state, answers = parts
    return await reading.review(
        host, None, t, state, answers, client, client.tone_selection(), time.monotonic() + seconds, 0.4
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,model,expected,mid",
    [
        ("door_bad", paired(), "ironic_praise", "m3"),
        ("door_good", paired("match", 0.9, "e3", 0.98, "sincere", 0.91, "help", 0.95), "sincere_praise", "m3"),
        ("parcel_bad", paired(evidence="e2"), "ironic_praise", "p9"),
        ("door_bad", paired(score=0.45, intent_score=0.85, consequence_score=0.92), "ironic_praise", "m3"),
    ],
)
async def test_native_verdict_cites_scoped_source_without_changing_action(name, model, expected, mid):
    parts = _fixture(name, model)
    result = await _run(parts)
    assert result["choice"] == expected and result["source_message_id"] == mid
    assert result["confidence"] >= 0.8 and len(parts[1].calls) == 1
    projected = reading.project(parts[-1], result)
    assert (projected["join"], projected["action"], projected["recipient"]) == (
        parts[-1]["join"],
        parts[-1]["action"],
        parts[-1]["recipient"],
    )
    assert projected["expression_mode"]["choice"] == ("literal" if expected == "sincere_praise" else "sarcasm")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,model,reason",
    [
        ("empty", paired(), "insufficient_context"),
        ("peer_only", paired(), "insufficient_context"),
        ("off_topic", paired(), "no_cited_outcome"),
        ("door_bad", paired(evidence="none"), "no_cited_outcome"),
        ("door_bad", paired(evidence="e9"), "no_cited_outcome"),
        ("door_bad", paired(score=0.56, intent_score=0.4), "low_confidence"),
        ("door_bad", paired(evidence_score=0.79), "low_confidence"),
        ("door_bad", paired(intent="sincere", intent_score=0.95), "conflicting_evidence"),
        ("door_bad", paired(consequence="help", consequence_score=0.95), "conflicting_evidence"),
        ("door_bad", RuntimeError("unavailable"), "review_unavailable"),
        ("door_bad", {}, "no_cited_outcome"),
    ],
)
async def test_abstains_without_inventing_evidence(name, model, reason):
    parts = _fixture(name, model)
    result = await _run(parts)
    assert result["reason"] == reason and result["choice"] == "unclear" and "source_message_id" not in result
    assert result["calls"] == (0 if name in ("empty", "peer_only") else 1)
    projected = reading.project(parts[-1], result)
    assert {projected[k]["choice"] for k in ("expression_mode", "speaker_stance")} == {"unclear"}


@pytest.mark.asyncio
async def test_late_uncertainty_cannot_erase_participation_or_cross_member_boundary(monkeypatch):
    parts = _fixture("door_bad", paired())
    t, client, host, state, answers = parts
    assert (await _run(parts, 0))["reason"] == "time_budget" and not client.calls
    client.during = lambda: setattr(client, "stamp", 2)
    assert (await _run(parts))["reason"] == "source_or_route_changed"
    client.during = lambda: monkeypatch.setattr(
        member_boundary,
        "filter_frames",
        lambda *a, **k: ([], member_boundary.BoundaryState(required=True)),
    )
    assert (await _run(parts))["reason"] == "source_or_route_changed"
    assert len(client.calls) == 2
    assert (await _run(parts))["reason"] == "source_or_route_changed" and len(client.calls) == 2
    assert answers["action"] == choice("ignore", 0.91)


@pytest.mark.asyncio
async def test_current_row_mismatch_and_cancellation():
    parts = _fixture("door_good", asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _run(parts)
    parts[3]["tone_exchange"]["current"]["speaker_id"] = "other"
    assert await _run(parts) is None and len(parts[1].calls) == 1


@pytest.mark.asyncio
async def test_rendered_platform_quote_is_not_speaker_speech():
    parts = list(_fixture("door_bad", paired()))
    current = parts[3]["tone_exchange"]["current"]
    current["words"] = "[引用消息(群友: 你可真行。)] 收到"
    parts[0] = replace(parts[0], text=current["words"])
    assert await _run(parts) is None and not parts[1].calls
    current["words"] = "[引用消息(群友: 引文独有字样。)] 你可真行"
    parts[0] = replace(parts[0], text=current["words"])
    assert (await _run(parts))["choice"] == "ironic_praise"
    assert "引文独有字样" not in parts[1].calls[0]["state"]


@pytest.mark.parametrize(
    "text,stance",
    [
        ("他说的“你可真行”是什么意思？", "reports"),
        ("你真是个废物。", "personal_attack"),
        ("这破游戏真是气死我了。", "venting"),
        ("这句话我不同意，别骂她。", "defense"),
        ("谢谢你一直耐心陪我试。", "praise"),
        ("我特别喜欢你画的云。", "praise"),
    ],
)
def test_reports_attacks_and_clear_gratitude_are_not_reviewed(text, stance):
    assert not reading._candidate(text, (stance, 0.98))


@pytest.mark.asyncio
async def test_auxiliary_diagnostics_preserve_primary_window_and_health():
    class Provider:
        is_systemone_provider = True
        context_window_tokens = 65536

        def get_model(self):
            return "decision-model-preview"

        async def systemone_evaluate(self, *, state, questions, timeout):
            if state == "failure":
                raise SystemOneError("synthetic", code="timeout")
            self.last_decision_usage = {"input_tokens": 65061 if state == "primary" else 140}
            return {"answer": choice("yes", 0.95)}

    p = Provider()
    host = NS(
        _runtime_config=NS(decision_backend="jev", decision_provider_id="current"),
        context=NS(get_provider_by_id=lambda _: p),
    )
    router = RoutedSystemOneClient(host, NativeDecisionUnavailable())
    q = {"answer": {"type": "choice", "instructions": "Choose", "criteria": {"yes": "yes", "no": "no"}}}
    await router.evaluate(state="primary", questions=q, timeout=1)
    original = router.snapshot()
    sel = router.tone_selection()
    assert await router.evaluate_tone(state="small", questions=q, timeout=1, selection=sel)
    assert not await router.evaluate_tone(state="failure", questions=q, timeout=1, selection=sel)
    observed = router.snapshot()
    for key in (
        "status",
        "available",
        "context_budget",
        "usage",
        "last_latency_ms",
        "model",
        "context_window_tokens",
    ):
        assert observed[key] == original[key]
    assert observed["calls"] == 3 and observed["tone_review_calls"] == 2 and observed["tone_review_failures"] == 1
    router.configure()
    assert not await router.evaluate_tone(state="stale", questions=q, timeout=1, selection=sel)
