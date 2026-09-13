"""Regressions for the from-scratch review fixes (clock plumbing, gates, storage).

Each test here pins a defect that was reproduced before the fix, so a future
change that reintroduces it fails loudly instead of silently degrading chat
behaviour (the unit suite missed all of them because VirtualClock reports the
same value for the civil and the monotonic clock).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core import daily_rhythm as rhythm_mod
from astrbot_plugin_chat_dynamics.core import topic_formation
from astrbot_plugin_chat_dynamics.core.agent_bridge import ExecutionHooks
from astrbot_plugin_chat_dynamics.core.daily_rhythm import DailyRhythmGate
from astrbot_plugin_chat_dynamics.core.dialogue_continuity import evaluate, time_decay
from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.style_shaper import StyleShaper
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter, TopicResolver
from astrbot_plugin_chat_dynamics.core.topic_annotations import TopicAnnotations
from astrbot_plugin_chat_dynamics.core.useful_proactive import UsefulProactiveGate

WALL = 1_789_000_000.0
MONO = 272_000.0


# --- dialogue continuity: the decay curve must stay total ---------------------

def test_time_decay_saturates_instead_of_overflowing():
    for seconds in (0.0, 60.0, 600.0, 2899.0, 2900.0, 3600.0, 10 ** 6):
        value = time_decay(seconds)
        assert math.isfinite(value) and 0.0 <= value <= 1.0
    assert time_decay(60.0) == pytest.approx(0.5)
    long_span = [time_decay(s) for s in (1.0, 30.0, 60.0, 120.0, 3600.0, 10 ** 6)]
    assert long_span == sorted(long_span, reverse=True)


def test_dialogue_beyond_the_old_overflow_window_rejects_quietly():
    dialogue = SimpleNamespace(user_id="alice", updated_at=1000.0, last_bot_was_question=True)
    candidate = SimpleNamespace(msg_id="m", user_id="alice", text="好的",
                                timestamp=1000.0 + 3600.0, metadata={})
    result = evaluate(dialogue, candidate, [])
    assert result.accepted is False
    assert result.score < result.threshold


# --- style shaper: prose must survive the sign-off filter ---------------------

@pytest.mark.parametrize(
    "text,survives",
    [
        ("我希望明天别下雨，希望你能来", "我希望明天别下雨，希望你能来"),
        ("今天真开心，希望明天也是好天气", "今天真开心，希望明天也是好天气"),
        ("希望你好好休息", "希望你好好休息"),
    ],
)
def test_signoff_filter_keeps_ordinary_hope(text, survives):
    assert StyleShaper().clean_robotic_signoffs(text) == survives


def test_signoff_filter_still_strips_real_closing_lines():
    shaped = StyleShaper().clean_robotic_signoffs("希望对您有所帮助。祝您生活愉快！")
    assert "希望对您有所帮助" not in shaped
    assert "祝您生活愉快" not in shaped


# --- clock plumbing: node windows use the monotonic clock --------------------

def test_rhythm_node_windows_use_the_node_clock(monkeypatch):
    seen = {}
    monkeypatch.setattr(rhythm_mod, "_local_hour", lambda stamp, timezone="": (seen.setdefault("tz", []).append(timezone), 23)[1])
    monkeypatch.setattr(rhythm_mod, "_chat_heat", lambda tele, nodes, *, stamp: seen.update(heat=stamp) or "cold")
    monkeypatch.setattr(rhythm_mod, "_unique_speakers", lambda nodes, *, since, bot_id="": seen.update(since=since) or 5)
    gate = DailyRhythmGate()
    gate.evaluate(session_id="s", user_id="u", text="随便聊聊", now=WALL, node_now=MONO,
                  cfg=SimpleNamespace(rhythm_timezone="Asia/Shanghai", daily_rhythm_enabled=True,
                                      rhythm_morning_hi_enabled=False, rhythm_day_share_slots=0))
    assert seen["heat"] == MONO
    assert seen["since"] == MONO - 40 * 60
    assert set(seen["tz"]) == {"Asia/Shanghai"}


def test_gap_detection_uses_the_node_clock():
    gate = UsefulProactiveGate()
    question = SimpleNamespace(msg_id="q", user_id="u1", text="这个怎么弄？", timestamp=MONO - 1.0,
                               metadata={})
    common = dict(sid="s", text="嗯", recent_nodes=[question], bot_id="bot",
                  group_memory=None, public_memory_snippet="", presence="sensible",
                  cold_on=False, telemetrics=None)
    # A question asked one second ago is not a hanging gap on the node clock.
    assert gate._detect_gap(stamp=WALL, node_stamp=MONO, **common) is None
    # Reading it against the civil clock is the defect that was fixed.
    fallback = gate._detect_gap(stamp=WALL, **common)
    assert fallback is not None and fallback[0] == "hanging_question"


def test_decision_gate_forwards_both_clocks():
    gate = DynamicsDecisionGate()
    captured = {}

    def make(name):
        def fake(**kwargs):
            captured[name] = kwargs
            return SimpleNamespace(allow=True, reason_code="ok", reason_zh="ok", action="",
                                   force_scale=1.0, length_hint="normal", delay_scale=1.0,
                                   proactive=False, proactive_blocked=False, gap_fingerprint="",
                                   gap_kind="", quota_used=0, quota_cap=0, as_dict=lambda: {})
        return fake

    gate.rhythm.evaluate = make("rhythm")
    gate.useful.evaluate = make("useful")
    gate.evaluate(session_id="s", user_id="u", text="随便聊聊", now=WALL, node_now=MONO)
    assert captured["rhythm"]["now"] == WALL and captured["rhythm"]["node_now"] == MONO
    assert captured["useful"]["now"] == WALL and captured["useful"]["node_now"] == MONO


# --- storage bounds ----------------------------------------------------------

def test_mute_duration_is_bounded_even_with_infinity(tmp_path):
    notebook = GroupMemoryNotebook(tmp_path)
    until = notebook.mute_tonight("room", hours=float("inf"))
    assert math.isfinite(until) and until - time.time() <= 721 * 3600


def test_notebook_number_rejects_non_finite_and_out_of_range():
    from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin

    with pytest.raises(ValueError):
        ChatDynamicsPlugin._notebook_number({"hours": float("inf")}, "hours",
                                            default=10.0, minimum=1.0, maximum=720.0)
    with pytest.raises(ValueError):
        ChatDynamicsPlugin._notebook_number({"hours": 5000}, "hours", minimum=1.0, maximum=720.0)
    assert ChatDynamicsPlugin._notebook_number({"hours": 12}, "hours") == 12.0


def test_memory_caches_stay_bounded(tmp_path):
    notebook = GroupMemoryNotebook(tmp_path)
    mood = MoodMemoryStore(tmp_path)
    for index in range(1000):
        notebook.list_all("room-%d" % index)
        mood.recall("room-%d" % index, "peer")
    assert len(notebook._cache) <= 256
    assert len(mood._cache) <= 256


def test_annotations_read_skips_malformed_rows():
    rows = [
        {"msg_id": "m1", "error_type": "correct", "predicted_topic": "a", "expected_topic": "a",
         "annotation_schema_version": 2},
        {"msg_id": "m2", "predicted_topic": "a"},  # legacy/truncated row
        "not-a-row",
    ]

    class Plugin:
        async def get_kv_data(self, key, default):
            return rows

    import asyncio

    data = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        TopicAnnotations(Plugin()).read("s")
    )
    assert [row["msg_id"] for row in data["records"]] == ["m1"]
    assert data["metrics"]["total"] == 1


def test_read_air_counts_are_scoped_to_the_named_session():
    from astrbot_plugin_chat_dynamics.core.dashboard import _read_air_summary

    class Manners:
        def today_stats(self, session_id=""):
            if session_id:
                return {"intervene": 1, "quiet": 1, "why_silent": [{"reason_code": "local"}],
                        "why_spoke": []}
            return {"intervene": 9, "quiet": 9, "why_silent": [{"reason_code": "global"}],
                    "why_spoke": []}

    plugin = SimpleNamespace(decision_gate=SimpleNamespace(manners=Manners(), useful=None, rhythm=None),
                             presence_knob="sensible", _metrics={}, daily_rhythm_enabled=False)
    sessions = [{"session_key": "room-a", "group_id": "room-a", "mpm": 1.0, "dag_nodes": 2}]

    scoped = _read_air_summary(plugin, sessions, session_key="room-a")
    assert (scoped["intervene_count"], scoped["quiet_count"]) == (1, 1)
    assert scoped["why_silent"][0]["reason_code"] == "local"

    unknown = _read_air_summary(plugin, sessions, session_key="room-z")
    assert (unknown["intervene_count"], unknown["quiet_count"]) == (0, 0)
    assert unknown["why_silent"] == []

    everything = _read_air_summary(plugin, sessions)
    assert (everything["intervene_count"], everything["quiet_count"]) == (9, 9)


@pytest.mark.asyncio
async def test_hub_self_cancellation_does_not_abort_a_reply(monkeypatch):
    """A Hub reconfigure cancels its own in-flight IO; the reply must survive."""
    from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent

    async def cancelled(**_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(native_request, "prepare_request", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_adapter, "collect_media_urls", AsyncMock(return_value=([], [])))
    loop = AsyncMock(return_value=SimpleNamespace(completion_text="reply"))
    client = llm_adapter.LLMAdapter(SimpleNamespace(tool_loop_agent=loop),
                                    integrations=SimpleNamespace(context_for_request=cancelled))
    client.resolve_provider_id = AsyncMock(return_value="provider")
    assert await client.run_native_agent(MockEvent("hello"), "raw query") == "reply"


@pytest.mark.asyncio
async def test_real_cancellation_still_propagates(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import llm_adapter, native_request
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent

    entered = asyncio.Event()

    async def blocked(**_kwargs):
        entered.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(native_request, "prepare_request", AsyncMock(return_value=None))
    monkeypatch.setattr(llm_adapter, "collect_media_urls", AsyncMock(return_value=([], [])))
    loop = AsyncMock(return_value=SimpleNamespace(completion_text="reply"))
    client = llm_adapter.LLMAdapter(SimpleNamespace(tool_loop_agent=loop),
                                    integrations=SimpleNamespace(context_for_request=blocked))
    client.resolve_provider_id = AsyncMock(return_value="provider")
    task = asyncio.create_task(client.run_native_agent(MockEvent("hello"), "raw query"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- hub credential handling --------------------------------------------------

def test_hub_refuses_cleartext_credentials_off_loopback():
    from astrbot_plugin_chat_dynamics.core.integrations.selflearning import SelfLearningHubClient

    remote_http = SelfLearningHubClient("http://hub.example.com", "secret")
    assert remote_http.snapshot()["status"] == "degraded"
    assert remote_http.snapshot()["error_code"] == "insecure_cleartext"
    assert remote_http._url == ""

    local_http = SelfLearningHubClient("http://127.0.0.1:8080", "secret")
    assert local_http.snapshot()["status"] == "configured"

    remote_https = SelfLearningHubClient("https://hub.example.com", "secret")
    assert remote_https.snapshot()["status"] == "configured"

    keyless_http = SelfLearningHubClient("http://hub.example.com", "")
    assert keyless_http.snapshot()["status"] == "configured"


def test_closed_hub_client_does_not_report_configured():
    import asyncio as _asyncio
    from astrbot_plugin_chat_dynamics.core.integrations.selflearning import SelfLearningHubClient

    client = SelfLearningHubClient("https://hub.example.com", "secret")
    _asyncio.get_event_loop_policy().new_event_loop().run_until_complete(client.close())
    client.configure("https://hub.example.com", "secret")
    assert client.snapshot()["status"] == "missing"


def test_hub_key_env_must_look_like_a_hub_credential():
    from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config

    ok, warnings = parse_runtime_config({"selflearning_hub_key_env": "MY_HUB_TOKEN"})
    assert ok.selflearning_hub_key_env == "MY_HUB_TOKEN" and not warnings

    rejected, warnings = parse_runtime_config({"selflearning_hub_key_env": "DEEPSEEK_API_KEY"})
    assert rejected.selflearning_hub_key_env == "SELFLEARNING_HUB_API_KEY"
    assert any("hub_key_env" in warning for warning in warnings)

    malformed, warnings = parse_runtime_config({"selflearning_hub_key_env": "bad name"})
    assert malformed.selflearning_hub_key_env == "SELFLEARNING_HUB_API_KEY" and warnings


# --- embedding boundary -------------------------------------------------------

def test_zero_vector_is_kept_by_normalize_but_rejected_as_embedding():
    from astrbot_plugin_chat_dynamics.core.embedding_adapter import (
        EmbeddingAdapter,
        _extract_vector,
        _normalize_vec,
    )

    # The shape contract is untouched (locked by test_v12_matrix).
    assert _normalize_vec([0.0, 0.0]) == (0.0, 0.0)
    # ... but a directionless vector never becomes a neural embedding.
    assert _extract_vector([0.0, 0.0]) is None
    assert _extract_vector([0.0, 1.0])[-1] == pytest.approx(1.0)

    adapter = EmbeddingAdapter(cache_size=8)
    assert adapter.remember("room", [0.0, 0.0]) == ()
    assert adapter.cache_len == 0


# --- snapshot budget ----------------------------------------------------------

def test_runtime_snapshot_stays_within_the_byte_budget():
    from astrbot_plugin_chat_dynamics.core import runtime_persistence as codec
    from astrbot_plugin_chat_dynamics.core.runtime_persistence import export_runtime_state
    from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRegistry
    from astrbot_plugin_chat_dynamics.core.telemetrics import TelemetricsTracker

    clock = SimpleNamespace(time=lambda: 600.0)
    plugin = SimpleNamespace(
        time_service=clock,
        _registry=SessionRegistry(clock),
        telemetrics=TelemetricsTracker(time_service=clock),
        _metrics={},
        _shadow_decisions=deque(maxlen=50),
    )
    for index in range(40):
        key = "room-%02d" % index
        runtime = plugin._registry.get_or_create(key, group_id=key, umo=key)
        runtime.touch(100.0 + index)  # distinct recency, oldest first
        for node_index in range(500):
            runtime.dag.add_message("%s-%d" % (key, node_index), "user", "x" * 4000,
                                    timestamp=100 + node_index)
    payload = export_runtime_state(plugin)
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert size <= codec.MAX_TOTAL_BYTES
    # Oldest sessions are dropped first, the newest one survives.
    keys = [row["session_key"] for row in payload["sessions"]]
    assert keys[-1] == "room-39" and keys[0] != "room-00"


# --- mood store write seam ----------------------------------------------------

def test_mood_remember_round_trips(tmp_path):
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.remember("room", "peer", tags=["开心"], now=1000.0)
    rows = store.recall("room", "peer", now=1000.0, limit=3, remote_rows=[])
    assert any("开心" in json.dumps(row, ensure_ascii=False) for row in rows)


# --- review-round-2 fixes -----------------------------------------------------

def test_loopback_detection_rejects_lookalike_names():
    from astrbot_plugin_chat_dynamics.core.integrations.selflearning import (
        SelfLearningHubClient,
        _is_loopback_host,
    )

    assert _is_loopback_host("127.0.0.1") and _is_loopback_host("localhost")
    assert _is_loopback_host("::1") and _is_loopback_host("0.0.0.0")
    # A DNS name is not an address: "127.example.com" points wherever its owner
    # says, so it must not inherit the cleartext exemption.
    assert not _is_loopback_host("127.example.com")
    assert not _is_loopback_host("example.com")
    assert not _is_loopback_host("")

    lookalike = SelfLearningHubClient("http://127.example.com", "secret")
    assert lookalike.snapshot()["error_code"] == "insecure_cleartext"


def test_media_gate_failure_listens_instead_of_speaking():
    gate = DynamicsDecisionGate()

    def boom(**_kwargs):
        raise RuntimeError("media gate exploded")

    gate.media.evaluate = boom
    verdict = gate.evaluate(session_id="s", user_id="u", text="帮我看这张图", has_media=True)
    assert verdict.should_speak is False
    assert verdict.reason_code == "media_gate_error"
    assert verdict.media is not None and verdict.media.has_image is True

    # No media in the turn: the fallback must stay out of the way.
    plain = gate.evaluate(session_id="s", user_id="u", text="随便聊聊")
    assert plain.media is None


# --- routing -----------------------------------------------------------------

class _Resolver(TopicResolver):
    def resolve(self, node, dag, state, matches, explicit_parent=None):
        if node.msg_id == "pending":
            return "a", 0.55, True, ["topic_ambiguous"], "b", [(0.55, "a"), (0.53, "b")]
        return node.msg_id, 0.9, False, [], None, []


def _pending_runtime():
    runtime = SessionRuntime("one", "g", "one", dag=ConversationDAG())
    router = ThreadRouter(topic_resolver=_Resolver(), require_intense_dialogue=False)
    for mid in ("a", "b", "pending"):
        node = runtime.dag.add_message(mid, mid, "substantive topic " + mid,
                                       timestamp=len(runtime.dag.nodes) + 1)
        router.route(runtime, node)
    return runtime, router


@pytest.mark.asyncio
async def test_failed_rerank_is_retried_with_backoff():
    runtime, router = _pending_runtime()

    class Reranker:
        calls = 0

        async def rerank(self, **kwargs):
            Reranker.calls += 1
            return SimpleNamespace(choice="B", topic_id="unknown-topic")

    node = runtime.dag.nodes["pending"]
    await router.rerank_pending(runtime, node, Reranker())
    assert Reranker.calls == 1
    snapshot = node.metadata["routing"]
    assert snapshot["rerank_attempts"] == 1
    assert snapshot["rerank_retry_at"] > time.monotonic()

    # Inside the backoff window the same node is not asked again.
    await router.rerank_pending(runtime, node, Reranker())
    assert Reranker.calls == 1

    # Once the window passes, the node gets another chance.
    snapshot["rerank_retry_at"] = time.monotonic() - 1.0
    await router.rerank_pending(runtime, node, Reranker())
    assert Reranker.calls == 2


def test_burst_scan_memoises_pair_scores():
    class Match:
        def __init__(self, score):
            self.score = score
            self.embedding_cosine = 0.0

    class Node:
        def __init__(self, index, stamp):
            self.msg_id = "m%d" % index
            self.user_id = "u%d" % (index % 3)
            self.text = "topic-%d shared words here" % index
            self.timestamp = stamp
            self.reply_to_id = None
            self.metadata = {}

    class Dag:
        def __init__(self, nodes):
            self.nodes = {n.msg_id: n for n in nodes}
            self.order = [n.msg_id for n in nodes]
            self.calls = 0

        def get_recent_nodes(self, limit):
            return [self.nodes[mid] for mid in self.order[-limit:]]

        def semantic_match_fn(self, left, right):
            self.calls += 1
            first = int(re.search(r"topic-(\d+)", left).group(1))
            second = int(re.search(r"topic-(\d+)", right).group(1))
            return Match(0.9 if abs(first - second) <= 1 else 0.1)

    nodes = [Node(index, 1000.0 + index * 0.5) for index in range(80)]
    dag = Dag(nodes)
    burst = topic_formation.discussion_burst(dag, nodes[-1], "bot")
    assert len(burst) == 80
    # The naive walk needed ~85k calls for the same answer.
    assert dag.calls <= 80 * 79 // 2


# --- integration hygiene ------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_receipts_hold_no_tool_output():
    journal = []
    seen = {}

    class Delegate:
        async def on_tool_end(self, run_context, tool, tool_args, tool_result):
            seen["result"] = tool_result

    hooks = ExecutionHooks(Delegate(), journal)
    await hooks.on_tool_end(None, SimpleNamespace(name="fetch"), {"url": "x"}, "SECRET BODY")
    assert journal == [{"tool": "fetch", "signature": journal[0]["signature"], "status": "completed"}]
    assert "SECRET BODY" not in str(journal)
    assert seen["result"] == "SECRET BODY"


def test_session_reset_clears_tool_receipts():
    runtime = SessionRuntime("one", "g", "one", dag=ConversationDAG())
    runtime.tool_executions.append({"tool": "fetch", "status": "completed"})
    runtime.reset_conversation_state()
    assert list(runtime.tool_executions) == []
