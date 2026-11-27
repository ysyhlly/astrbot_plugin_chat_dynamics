"""Wave 1 regression & contract tests for astrbot_plugin_chat_dynamics."""

from __future__ import annotations

import asyncio
from dataclasses import replace as dc_replace
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import astrbot_plugin_chat_dynamics.series_panels as series_panels
from astrbot_plugin_chat_dynamics.core.dashboard import replay_topic_blocks
from astrbot_plugin_chat_dynamics.core.integrations import group_ecosystem
from astrbot_plugin_chat_dynamics.core.jev_decision import build_questions, decision_from_answers
from astrbot_plugin_chat_dynamics.core.outcome_recorder import read_outcome
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
from astrbot_plugin_chat_dynamics.core.scene_context import scene_context
from astrbot_plugin_chat_dynamics.core.series_hub import build_series_health
from astrbot_plugin_chat_dynamics.core.series_privacy import SeriesPrivacyRegistry
from astrbot_plugin_chat_dynamics.core.setup_wizard import activate_setup_companions
from astrbot_plugin_chat_dynamics.core.turn_latency import degrade, record_stage, start_turn
from .test_jev_decision_layer import JevDouble, answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import At, MockEvent, _plugin

ROOT = Path(__file__).resolve().parents[1]


def _make_star(identity: str, owner: object, *, activated: bool = True, version: str = "2.1.0"):
    return SimpleNamespace(name=identity, activated=activated, version=version, star_cls=owner)


# ---------------------------------------------------------------------------
# 1. Smart follow-up merge (member_revision_changed governance)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smart_followup_merge_regenerates_once_and_delivers(jev_plugin):
    p, bridge = jev_plugin
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    calls = {"n": 0}
    seen_texts: list[str] = []

    orig_generate = bridge.generate

    async def tracked_generate(event, events, *args, **kwargs):
        calls["n"] += 1
        seen_texts.append(" | ".join(e.message_str for e in events))
        if calls["n"] == 1:
            entered_first.set()
            await release_first.wait()
        return await orig_generate(event, events, *args, **kwargs)

    bridge.generate = tracked_generate
    ev1 = MockEvent("帮我看看这个配置", message_id="fm1", sender_id="u1", components=[At("bot_42")])
    ev2 = MockEvent("另外加上超时参数", message_id="fm2", sender_id="u1")
    try:
        await p.on_group_message(ev1)
        await flush(p, ev1)
        await asyncio.wait_for(entered_first.wait(), 2.0)

        # Same user sends a substantive follow-up before first sentence is sent
        await p.on_group_message(ev2)
        release_first.set()
        await drain(p)

        assert calls["n"] == 2
        assert "帮我看看这个配置" in seen_texts[1] and "另外加上超时参数" in seen_texts[1]
        assert len(ev1.replies_sent) + len(ev2.replies_sent) >= 1
    finally:
        release_first.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_unrelated_member_message_does_not_drop_in_flight_reply(jev_plugin):
    p, bridge = jev_plugin
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    bridge.before_reply = pause
    ev1 = MockEvent("问个问题", message_id="ur1", sender_id="u1", components=[At("bot_42")])
    ev_other = MockEvent("哈哈路过", message_id="ur2", sender_id="u2")
    try:
        await p.on_group_message(ev1)
        await flush(p, ev1)
        await asyncio.wait_for(entered.wait(), 2.0)

        await p.on_group_message(ev_other)
        release.set()
        await drain(p)

        assert ev1.replies_sent, "Unrelated member message must not discard in-flight reply"
    finally:
        release.set()
        await p.terminate()


@pytest.mark.asyncio
async def test_followup_after_merge_cap_does_not_discard_reply(jev_plugin):
    p, bridge = jev_plugin
    entered_second = asyncio.Event()
    release_second = asyncio.Event()
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    calls = {"n": 0}
    orig_generate = bridge.generate

    async def tracked_generate(event, events, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            entered_first.set()
            await release_first.wait()
        elif calls["n"] == 2:
            entered_second.set()
            await release_second.wait()
        return await orig_generate(event, events, *args, **kwargs)

    bridge.generate = tracked_generate
    ev1 = MockEvent("第一句问题", message_id="cap1", sender_id="u1", components=[At("bot_42")])
    ev2 = MockEvent("补充一点细节", message_id="cap2", sender_id="u1")
    ev3 = MockEvent("再补充第二条", message_id="cap3", sender_id="u1")
    try:
        await p.on_group_message(ev1)
        await flush(p, ev1)
        await asyncio.wait_for(entered_first.wait(), 2.0)

        await p.on_group_message(ev2)
        release_first.set()
        await asyncio.wait_for(entered_second.wait(), 2.0)

        # Second follow-up exceeds max 1 merge regen -> must NOT drop the generated reply
        await p.on_group_message(ev3)
        release_second.set()
        await drain(p)

        assert calls["n"] == 2
        assert len(ev1.replies_sent) + len(ev2.replies_sent) >= 1
    finally:
        release_first.set()
        release_second.set()
        await p.terminate()


# ---------------------------------------------------------------------------
# 2. Full-chain recall interception
# ---------------------------------------------------------------------------


def _make_recall_event(umo: str, message_id: str, group_id: str = "group_1"):
    return SimpleNamespace(
        unified_msg_origin=umo,
        message_obj=SimpleNamespace(
            raw_message={
                "post_type": "notice",
                "notice_type": "group_recall",
                "message_id": message_id,
                "group_id": group_id,
            }
        ),
    )


@pytest.mark.asyncio
async def test_recall_removes_buffered_message_from_debounce(jev_plugin):
    p, _bridge = jev_plugin
    ev = MockEvent("马上要撤回的消息", message_id="rec-buf-1", sender_id="u1", components=[At("bot_42")])
    try:
        await p.on_group_message(ev)
        assert p.debounce.get_pending_count() == 1
        await p.on_platform_recall_notice(_make_recall_event(ev.unified_msg_origin, "rec-buf-1", ev.group_id))
        assert p.debounce.get_pending_count() == 0
        runtime = p._sessions[ev.unified_msg_origin]
        assert runtime.is_recalled("rec-buf-1") is True
    finally:
        await p.terminate()


@pytest.mark.asyncio
async def test_recall_during_generation_silently_aborts_delivery(jev_plugin):
    p, bridge = jev_plugin
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    bridge.before_reply = pause
    ev = MockEvent("说错话马上撤回", message_id="rec-gen-1", sender_id="u1", components=[At("bot_42")])
    try:
        await p.on_group_message(ev)
        await flush(p, ev)
        await asyncio.wait_for(entered.wait(), 2.0)

        await p.on_platform_recall_notice(_make_recall_event(ev.unified_msg_origin, ev.message_id, ev.group_id))
        release.set()
        await drain(p)

        assert not ev.replies_sent
        runtime = p._sessions[ev.unified_msg_origin]
        outcome = read_outcome(runtime.dag.get_node(ev.message_id))
        assert outcome["suppression_reason"] == "message_recalled"
    finally:
        release.set()
        await p.terminate()


# ---------------------------------------------------------------------------
# 3. Eliminate 60s wait silent black hole (12s soft timeout + force_complete)
# ---------------------------------------------------------------------------


def test_jev_force_complete_skips_completion_question_and_clarifies():
    turn = TurnContext(
        session_key="qq:GroupMessage:g1",
        author="u1",
        text="小安，那个",
        messages=(MessageSnapshot(message_id="m1", author="u1", text="小安，那个", author_name="Alice"),),
        background=(),
        epoch=1,
        revision=1,
        started_at=time.time(),
        explicit=False,
        wake_kind="name",
        bot_id="bot_42",
    )
    questions = build_questions(turn, force_complete=True)
    assert "completion" not in questions
    answers = {
        "completion": SimpleNamespace(choice="wait", probability=0.9, confidence=0.9, probabilities={"wait": 0.9, "complete": 0.1}),
        "recipient": SimpleNamespace(choice="bot", probability=0.9, confidence=0.9, probabilities={"bot": 0.9}),
        "action": SimpleNamespace(
            choice="ignore",
            probability=0.45,
            confidence=0.45,
            probabilities={"ignore": 0.45, "reply": 0.35, "clarify": 0.2},
        ),
        "state": SimpleNamespace(choice="casual", probability=0.8, confidence=0.8, probabilities={"casual": 0.8}),
        "length": SimpleNamespace(choice="brief", probability=0.8, confidence=0.8, probabilities={"brief": 0.8}),
        "reason": SimpleNamespace(choice="addressed_question", probability=0.8, confidence=0.8, probabilities={"addressed_question": 0.8}),
    }
    decision = decision_from_answers(turn, answers, force_complete=True)
    assert decision.action != "wait"
    assert decision.reason_code != "waiting_for_completion"


@pytest.mark.asyncio
async def test_wait_soft_timeout_resubmits_with_force_complete(jev_plugin):
    p, bridge = jev_plugin
    p.debounce.pending_input_timeout = 30.0
    p.debounce.pending_input_soft_timeout = 0.05
    p._runtime_config = dc_replace(p._runtime_config, pending_input_soft_timeout=0.05)

    seen_force_flags: list[bool] = []

    class SoftWaitJev(JevDouble):
        async def evaluate(self, *, state, questions, timeout=None):
            force_flag = "completion" not in questions
            seen_force_flags.append(force_flag)
            if not force_flag:
                self.payload = answers(completion={"type": "choice", "choice": "wait", "confidence": 0.95})
            else:
                self.payload = answers(action="reply")
            return await super().evaluate(state=state, questions=questions, timeout=timeout)

    p.jev = SoftWaitJev(answers())
    ev = MockEvent("小安，那个", message_id="soft-wait-1", sender_id="u1", is_at_or_wake_command=True)
    try:
        await p.on_group_message(ev)
        await flush(p, ev)
        # Wait past the 0.05s soft timeout so force_complete triggers
        await asyncio.sleep(0.25)
        await drain(p)
        assert seen_force_flags == [False, True]
        assert ev.replies_sent, "Soft timeout should trigger force_complete reply instead of 60s silence"
    finally:
        await p.terminate()


# ---------------------------------------------------------------------------
# 4. Deep cooling unlockable + config panel gaps + vibe_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uncool_command_and_web_api_preserve_dag(offline_web_responses):
    p = _plugin({"takeover_all": True})
    ev = MockEvent("hello", message_id="uncool-m1", group_id="g100")
    umo = ev.unified_msg_origin
    rt = p._get_or_create_runtime(umo, group_id="g100", umo=umo, bot_id="bot_42")
    rt.dag.add_message("uncool-m1", "u1", "hello", time.time())
    p.arbiter.trigger_cooling(umo, duration_seconds=900)
    assert p.arbiter.cooling_remaining(umo) > 0

    # /dynamics uncool
    await p.cmd_dynamics(ev, action="uncool")
    assert p.arbiter.cooling_remaining(umo) == 0.0
    assert any("解除" in msg for _, msg in p.context.sent_messages) or any("解除" in str(r) for r in ev.replies_sent)
    assert rt.dag.get_node("uncool-m1") is not None

    # /dynamics cool 0
    p.context.sent_messages.clear()
    ev.replies_sent.clear()
    p.arbiter.trigger_cooling(umo, duration_seconds=600)
    await p.cmd_dynamics(ev, action="cool", param="0")
    assert p.arbiter.cooling_remaining(umo) == 0.0
    assert any("解除" in msg for _, msg in p.context.sent_messages) or any("解除" in str(r) for r in ev.replies_sent)
    assert rt.dag.get_node("uncool-m1") is not None

    # Web API POST /uncool
    p.arbiter.trigger_cooling(umo, duration_seconds=1200)
    res = await p._uncool_session_async(umo)
    assert res is True
    assert p.arbiter.cooling_remaining(umo) == 0.0
    assert rt.dag.get_node("uncool-m1") is not None
    await p.terminate()


def test_config_panel_effective_config_and_frontend_groups_complete():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    p = _plugin({})
    effective = p.get_config_panel()["effective"]
    missing_eff = [k for k in schema if k not in effective]
    assert not missing_eff, f"get_effective_config missing schema keys: {missing_eff}"

    cfg_js = (ROOT / "pages" / "app" / "config" / "app.js").read_text(encoding="utf-8")
    assert '"vibe_provider"' in cfg_js
    assert '"light_analysis_provider"' in cfg_js
    assert '"vision_provider"' in cfg_js
    assert '"rerank_provider"' in cfg_js
    for key in schema:
        assert f'"{key}"' in cfg_js, f"pages/app/config/app.js missing schema key: {key}"


# ---------------------------------------------------------------------------
# 5. Shared config v4 persona + Privacy mute registry + Setup activation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_config_v4_persona_and_privacy_registry(tmp_path):
    pm = SimpleNamespace(default_persona_id="astrbot_v4_persona")
    p = _plugin(
        {
            "takeover_groups": ["10001"],
            "reply_provider": "prov-reply",
            "decision_provider": "prov-dec",
            "light_analysis_provider": "prov-light",
            "vision_provider": "prov-vis",
            "rerank_provider": "prov-rerank",
            "private_shared_group_sources": ["10001", "10002"],
        }
    )
    p.context.persona_manager = pm
    p.series_privacy = SeriesPrivacyRegistry(tmp_path / "privacy.json")

    shared = p.get_series_shared_config()
    assert shared["persona_id"] == "astrbot_v4_persona"
    assert shared["light_analysis_provider"] == "prov-light"
    assert shared["vision_provider"] == "prov-vis"
    assert shared["rerank_provider"] == "prov-rerank"
    assert shared["private_shared_group_sources"] == ["10001", "10002"]
    assert isinstance(shared["group_profiles"], dict)

    # Privacy mute registry + persistence + series_panels helper
    ctx = SimpleNamespace(
        get_all_stars=lambda: [_make_star("astrbot_plugin_chat_dynamics", p)]
    )
    assert series_panels.is_member_privacy_muted(ctx, "qq:GroupMessage:10001", "u1", "memory") is False

    res = await p.set_series_privacy_muted(
        "qq:GroupMessage:10001",
        "u1",
        True,
        scopes=["memory", "learning"],
        actor="admin",
        reason="user_request",
    )
    assert res["muted"] is True
    assert set(res["scopes"]) == {"memory", "learning"}
    assert series_panels.is_member_privacy_muted(ctx, "qq:GroupMessage:10001", "u1", "memory") is True
    assert series_panels.is_member_privacy_muted(ctx, "qq:GroupMessage:10001", "u1", "chronicle") is False

    # Reload from disk and verify persistence + audit log
    reloaded = SeriesPrivacyRegistry(tmp_path / "privacy.json")
    assert reloaded.get_muted("qq:GroupMessage:10001", "u1", "memory")["muted"] is True
    assert len(reloaded.audit_log()) == 1

    # Revoke
    revoked = await p.set_series_privacy_muted(
        "qq:GroupMessage:10001", "u1", False, scopes=["memory"], actor="u1", reason="restore"
    )
    assert revoked["muted"] is True  # learning still active
    assert series_panels.is_member_privacy_muted(ctx, "qq:GroupMessage:10001", "u1", "memory") is False
    assert series_panels.is_member_privacy_muted(ctx, "qq:GroupMessage:10001", "u1", "learning") is True
    assert series_panels.inherit_provider("", ctx, role="light_analysis") == "prov-light"
    assert series_panels.inherit_provider("", ctx, role="vision") == "prov-vis"
    assert series_panels.inherit_provider("", ctx, role="rerank") == "prov-rerank"
    await p.terminate()


@pytest.mark.asyncio
async def test_setup_wizard_activate_companions():
    called = {"activate": [], "scope": []}

    class FakeCompanion:
        async def activate_series_runtime(self):
            called["activate"].append("world")
            return {"ok": True}

        async def set_series_scope_enabled(self, umo, enabled, expected_version=None):
            called["scope"].append((umo, enabled))
            return {"ok": True, "enabled": enabled, "version": "v2"}

    p = _plugin({"takeover_groups": ["qq:GroupMessage:20001"]})
    p.context.get_all_stars = lambda: [
        _make_star("astrbot_plugin_chat_dynamics", p),
        _make_star("astrbot_plugin_dynamics_world", FakeCompanion()),
    ]
    report = await activate_setup_companions(p, umos=["qq:GroupMessage:20001"])
    assert report["ok"] is True
    assert any(item["plugin_id"] == "astrbot_plugin_dynamics_world" for item in report["activated"])
    assert called["activate"] == ["world"]
    assert ("qq:GroupMessage:20001", True) in called["scope"]
    await p.terminate()


# ---------------------------------------------------------------------------
# 6. Group ecosystem integration (games + chronicle + world scene_context)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_ecosystem_games_chronicle_and_scene_context():
    class FakeGames:
        def get_active_game_state(self, umo):
            return {
                "active": True,
                "engines": ["blackjack21"],
                "activities": [{"title": "周末共读"}],
                "companion_mode_hint": "群内正在21点对局，降低闲聊插话",
            }

        def get_group_activity_context(self, umo):
            return {"summary": "正在进行 21 点"}

    class FakeChronicle:
        def get_culture_context(self, umo, query="", limit=6):
            return [
                {"term": "夜宵局", "meaning": "每晚十一点集体点烧烤", "status": "approved"},
                {"term": "未审词", "meaning": "不应出现", "status": "pending"},
            ]

    ctx = SimpleNamespace(
        get_all_stars=lambda: [
            _make_star("astrbot_plugin_dynamics_games", FakeGames()),
            _make_star("astrbot_plugin_group_chronicle", FakeChronicle()),
        ]
    )

    game_ctx = await group_ecosystem.read_game_context(ctx, "qq:GroupMessage:30001")
    assert game_ctx is not None and game_ctx["active"] is True
    assert "blackjack21" in game_ctx["engines"]

    culture_ctx = await group_ecosystem.read_culture_context(ctx, "qq:GroupMessage:30001")
    assert culture_ctx is not None
    assert len(culture_ctx["entries"]) == 1
    assert culture_ctx["entries"][0]["term"] == "夜宵局"

    augmented = group_ecosystem.augment_reply_prompt(
        json.dumps({"reply_prompt": "原提示词"}, ensure_ascii=False),
        game_context=game_ctx,
        culture_context=culture_ctx,
    )
    assert "夜宵局" in augmented
    assert "21点" in augmented or "blackjack21" in augmented

    sc = scene_context(
        {
            "scene": {
                "available": True,
                "place_name": "群间小屋",
                "room_name": "客厅",
                "mailbox": {"unread_count": 1, "recent": [{"from": "阿青", "title": "明信片"}]},
                "keepsakes": [{"title": "海边贝壳"}],
                "garden": {"status": "薄荷长势正好"},
                "residents": [{"name": "小安", "activity": "看书"}],
            }
        }
    )
    assert sc is not None
    assert "mailbox" in sc and sc["mailbox"]["recent"][0]["title"] == "明信片"
    assert "keepsakes" in sc and sc["keepsakes"][0]["title"] == "海边贝壳"
    assert "garden" in sc and "薄荷" in sc["garden"]["status"]
    assert "residents" in sc and sc["residents"][0]["name"] == "小安"


# ---------------------------------------------------------------------------
# 7. Shared panel base upgrade: conflict tiers, hash drift, telemetry & replay
# ---------------------------------------------------------------------------


def test_series_panels_conflict_tiers_and_health_hash_drift(tmp_path):
    series_panels.invalidate_presence_cache()
    p = _plugin({})
    dummy_owner = SimpleNamespace(get_dynamics_capabilities=lambda: {"schema_version": 1, "ready": True, "enabled": True})

    # Gentle overlap plugins should NOT trigger legacy_conflict or degraded health
    ctx_overlap = SimpleNamespace(
        get_all_stars=lambda: [
            _make_star("astrbot_plugin_chat_dynamics", p),
            _make_star("astrbot_plugin_group_chronicle", dummy_owner),
            _make_star("astrbot_plugin_dynamics_learning", dummy_owner),
            _make_star("astrbot_plugin_dynamics_memory", dummy_owner),
            _make_star("astrbot_plugin_dynamics_horizons", dummy_owner),
            _make_star("astrbot_plugin_dynamics_world", dummy_owner),
            _make_star("qq_group_daily_analysis", SimpleNamespace()),
            _make_star("group_relation_analyzer", SimpleNamespace()),
            _make_star("portrayal", SimpleNamespace()),
            _make_star("bili_push", SimpleNamespace()),
            _make_star("weather_query", SimpleNamespace()),
        ],
        get_config=lambda: {"agent_runner": {"config": {"model": {"provider_id": "p1"}}}},
        provider_manager=SimpleNamespace(inst_map={"p1": SimpleNamespace(id="p1")}),
    )
    pres = series_panels.presence(ctx_overlap, "astrbot_plugin_chat_dynamics", root=tmp_path)
    assert pres["legacy_conflict"] is False
    assert pres["has_overlap_plugins"] is True
    overlap_ids = {item["id"] for item in pres["overlap_plugins"]}
    assert overlap_ids == {
        "qq_group_daily_analysis",
        "group_relation_analyzer",
        "portrayal",
        "bili_push",
        "weather_query",
    }
    assert all(item["severity"] == "overlap" for item in pres["overlap_plugins"])

    # Health check with only overlap notices stays "ok" (info severity does not degrade)
    p.context = ctx_overlap
    health = build_series_health(p)
    assert "series_panels_sync" in health
    info_codes = [w["code"] for w in health["environment_warnings"] if w["severity"] == "info"]
    assert "plugin_feature_overlap" in info_codes

    # Strong conflict still triggers legacy_conflict=True
    ctx_strong = SimpleNamespace(
        get_all_stars=lambda: [
            _make_star("astrbot_plugin_dynamics_learning", dummy_owner),
            _make_star("astrbot_plugin_self_learning", SimpleNamespace()),
        ]
    )
    pres_strong = series_panels.presence(ctx_strong, "astrbot_plugin_chat_dynamics", root=tmp_path)
    assert pres_strong["legacy_conflict"] is True
    assert pres_strong["legacy_conflicts"][0]["severity"] == "strong"


def test_replay_exports_turn_latency_recalled_rhythm_and_frontend_contracts():
    p = _plugin({"takeover_all": True})
    umo = "qq:GroupMessage:40001"
    rt = p._get_or_create_runtime(umo, group_id="40001", umo=umo, bot_id="bot_42")
    node = rt.dag.add_message("rep-m1", "u1", "测试回放字段", time.time())
    t0 = time.perf_counter()
    start_turn(node, started=t0)
    record_stage(node, "decision", t0)
    degrade(node, "vibe_timeout")
    node.metadata["turn_latency"]["first_send_seconds"] = 0.42
    node.metadata["recalled"] = True
    node.metadata["rhythm_trace"] = {"action": "reply", "mode": "fast_banter", "reason": "direct_call"}

    blocks = replay_topic_blocks(p, [], umo)
    all_msgs = [m for b in blocks for m in (b.get("messages") or [])]
    target = next(m for m in all_msgs if m["msg_id"] == "rep-m1")
    assert target["recalled"] is True
    assert target["rhythm_trace"]["action"] == "reply"
    assert target["turn_latency"]["first_send_seconds"] == 0.42
    assert "decision" in target["turn_latency"]["stages"]
    assert "vibe_timeout" in target["turn_latency"]["degraded"]

    # Frontend contracts
    ui_js = (ROOT / "pages" / "shared" / "series-ui.js").read_text(encoding="utf-8")
    ws_js = (ROOT / "pages" / "shared" / "series-workspace.js").read_text(encoding="utf-8")
    console_html = (ROOT / "pages" / "app" / "console" / "index.html").read_text(encoding="utf-8")
    telemetry_js = (ROOT / "pages" / "app" / "console" / "telemetry.js").read_text(encoding="utf-8")
    replay_js = (ROOT / "pages" / "app" / "replay" / "app.js").read_text(encoding="utf-8")
    setup_html = (ROOT / "pages" / "app" / "setup" / "index.html").read_text(encoding="utf-8")

    assert "getSharedPersona" in ui_js and "setSharedPersona" in ui_js and "dynamics-personachange" in ui_js
    assert 'query.has("persona")' in ws_js and "persona: opts.persona" in ws_js
    assert 'id="btnUncool"' in console_html and 'id="telemetryTurnLatencyBody"' in console_html
    assert "renderTurnLatency" in telemetry_js
    assert "data-turn-latency" in replay_js and "data-recalled-badge" in replay_js and "data-rhythm-trace" in replay_js
    assert 'id="btnActivateCompanions"' in setup_html
