"""Wave 2 regression & contract tests for astrbot_plugin_chat_dynamics."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.dashboard import scene_replay_snapshot, snapshot_overview
from astrbot_plugin_chat_dynamics.core.dry_run import simulate_decision_dry_run
from astrbot_plugin_chat_dynamics.core.group_profiles import GroupProfileStore
from astrbot_plugin_chat_dynamics.core.outcome_recorder import mark_suppressed, read_outcome
from astrbot_plugin_chat_dynamics.core.series_hub import batch_toggle_series_matrix
from astrbot_plugin_chat_dynamics.core.series_templates import SeriesTemplateStore
from astrbot_plugin_chat_dynamics.core.turn_latency import record_stage, start_turn
from .test_jev_decision_layer import answers, jev_plugin as jev_plugin
from .test_persona_model import drain, flush
from .test_plugin_lifecycle import MockEvent, Reply, _plugin

ROOT = Path(__file__).resolve().parents[1]


def _make_star(identity: str, owner: object, *, activated: bool = True, version: str = "2.2.0"):
    return SimpleNamespace(name=identity, activated=activated, version=version, star_cls=owner)


# ---------------------------------------------------------------------------
# 1. /群间 ecosystem card & /群间 自检 admin health report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qunjian_card_and_admin_self_check():
    class FakeEnabledCompanion:
        def get_series_scope_status(self, umo):
            return {"umo": umo, "enabled": True, "writable": True, "version": "1"}

        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

    class FakePausedCompanion:
        def get_series_scope_status(self, umo):
            return {"umo": umo, "enabled": False, "writable": True, "version": "1"}

        def get_dynamics_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": False}

    p = _plugin({"takeover_groups": ["g1"]})
    p.context.get_all_stars = lambda: [
        _make_star("astrbot_plugin_chat_dynamics", p),
        _make_star("astrbot_plugin_dynamics_world", FakeEnabledCompanion()),
        _make_star("astrbot_plugin_dynamics_games", FakeEnabledCompanion()),
        _make_star("astrbot_plugin_dynamics_memory", FakePausedCompanion()),
    ]
    try:
        member_ev = MockEvent("/群间", group_id="g1", sender_id="u1", is_admin_user=False)
        await p.cmd_qunjian(member_ev)
        card = member_ev.replies_sent[-1]
        assert "群间生态" in card
        assert "3D 小屋" in card and "/小屋实况" in card
        assert "群间游戏" in card and "/共读" in card
        assert "已安装但本群未启用" in card and "群间记忆" in card

        # Non-admin blocked from /群间 自检
        denied_ev = MockEvent("/群间 自检", group_id="g1", sender_id="u1", is_admin_user=False)
        await p.cmd_qunjian(denied_ev, subcommand="自检")
        assert "仅管理员" in denied_ev.replies_sent[-1]

        # Admin gets health report with hash sync & conflict section
        admin_ev = MockEvent("/群间 自检", group_id="g1", sender_id="admin1", is_admin_user=True)
        await p.cmd_qunjian(admin_ev, subcommand="自检")
        report = admin_ev.replies_sent[-1]
        assert "群间系列自检报告" in report
        assert "共享底座校验" in report
        assert "强冲突" in report
    finally:
        await p.terminate()


# ---------------------------------------------------------------------------
# 2. /dynamics why, on, off, presence, status guide, /dynamics_stop receipts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamics_why_on_off_presence_and_stop_receipts():
    p = _plugin({"takeover_all": False, "takeover_groups": []})
    try:
        # /dynamics status on non-takeover group provides onboarding hint
        ev_st = MockEvent("/dynamics status", group_id="g20", is_admin_user=True)
        await p.cmd_dynamics(ev_st, action="status")
        assert "/dynamics on" in ev_st.replies_sent[-1]

        # /dynamics_stop on non-takeover group replies clearly without creating runtime
        ev_stop_off = MockEvent("/dynamics_stop", group_id="g20", sender_id="u1")
        await p.cmd_dynamics_stop(ev_stop_off)
        assert "尚未开启" in ev_stop_off.replies_sent[-1]
        assert ev_stop_off.unified_msg_origin not in p._sessions

        # /dynamics on enables takeover
        ev_on = MockEvent("/dynamics on", group_id="g20", is_admin_user=True)
        await p.cmd_dynamics(ev_on, action="on")
        assert p.is_group_takeover_enabled("g20") is True
        assert "开启" in ev_on.replies_sent[-1]

        # /dynamics presence sets per-group override and reset clears it
        ev_p = MockEvent("/dynamics presence lively", group_id="g20", is_admin_user=True)
        await p.cmd_dynamics(ev_p, action="presence", param="lively")
        assert p.get_effective_group_profile(ev_p.unified_msg_origin, group_id="g20")["effective"]["presence_knob"] == "lively"
        ev_pr = MockEvent("/dynamics presence reset", group_id="g20", is_admin_user=True)
        await p.cmd_dynamics(ev_pr, action="presence", param="reset")
        assert p.get_effective_group_profile(ev_pr.unified_msg_origin, group_id="g20")["has_overrides"] is False

        # /dynamics why reports recent decision, latency, and guard reason
        rt = p._get_or_create_runtime(ev_on.unified_msg_origin, group_id="g20", umo=ev_on.unified_msg_origin, bot_id="bot_42")
        node = rt.dag.add_message("why-m1", "u1", "今晚打游戏吗", time.time())
        t0 = time.perf_counter()
        start_turn(node, started=t0)
        record_stage(node, "decision", t0)
        mark_suppressed(node, "below_threshold")
        ev_why = MockEvent("/dynamics why 3", group_id="g20", is_admin_user=True)
        await p.cmd_dynamics(ev_why, action="why", param="3")
        why_out = ev_why.replies_sent[-1]
        assert "排障透镜" in why_out
        assert "below_threshold" in why_out
        assert "decision" in why_out

        # /dynamics off disables takeover
        ev_off = MockEvent("/dynamics off", group_id="g20", is_admin_user=True)
        await p.cmd_dynamics(ev_off, action="off")
        assert p.is_group_takeover_enabled("g20") is False
    finally:
        await p.terminate()


# ---------------------------------------------------------------------------
# 3. Dry-Run Decision Simulator (side-by-side baseline vs draft)
# ---------------------------------------------------------------------------


def test_dry_run_simulator_compares_baseline_vs_draft_without_mutation():
    p = _plugin({"reply_probability_threshold": 85.0, "presence_knob": "ghost", "shadow_mode": False})
    res = simulate_decision_dry_run(
        p,
        {
            "text": "大家晚上好呀，推荐个好看的电影？",
            "wake_kind": "ambient",
            "draft_overrides": {
                "presence_knob": "lively",
                "reply_probability_threshold": 45.0,
                "decision_backend": "llm",
            },
        },
    )
    assert res["dry_run"] is True
    assert res["baseline"]["presence_knob"] == "ghost"
    assert res["draft"]["presence_knob"] == "lively"
    assert res["draft"]["decision_backend"] == "llm"
    assert res["draft"]["should_speak"] is True
    assert res["baseline"]["should_speak"] is False
    assert res["outcome_changed"] is True
    # Live config untouched
    assert p._runtime_config.presence_knob == "ghost"
    assert p._runtime_config.reply_probability_threshold == 85.0


# ---------------------------------------------------------------------------
# 4. Per-Group Override Profiles & Template Packaging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_group_override_profiles_persist_and_apply_with_templates(tmp_path):
    p = _plugin({"takeover_all": True, "presence_knob": "sensible", "reply_probability_threshold": 70.0, "shadow_mode": False})
    p.group_profiles_store = GroupProfileStore(tmp_path / "gp.json")
    p._group_profiles = {}
    p.series_templates = SeriesTemplateStore(tmp_path / "tpl.json")

    umo = "qq:GroupMessage:88001"
    saved = p.set_group_profile(
        umo,
        {
            "presence_knob": "ghost",
            "reply_probability_threshold": 88.0,
            "shadow_mode": True,
            "natural_reply_rhythm_enabled": False,
        },
    )
    assert saved["presence_knob"] == "ghost"
    assert p.is_session_shadow_mode(umo) is True
    assert p.get_series_shared_config()["group_profiles"][umo]["reply_probability_threshold"] == 88.0

    # Reload from disk
    reloaded = GroupProfileStore(tmp_path / "gp.json")
    assert reloaded.get(umo)["shadow_mode"] is True

    # Custom template packing group_profile + batch apply
    tpl = p.series_templates.upsert(
        {
            "id": "custom_quiet_tpl",
            "name": "静默陪伴模板",
            "states": {"astrbot_plugin_chat_dynamics": True},
            "group_profile": {"presence_knob": "lively", "reply_probability_threshold": 55.0, "shadow_mode": False},
        }
    )
    assert tpl["group_profile"]["presence_knob"] == "lively"

    status_code, body = await batch_toggle_series_matrix(
        p, umo="qq:GroupMessage:88002", template_id="custom_quiet_tpl"
    )
    assert status_code == 200
    assert body["ok"] is True
    assert body["group_profile_applied"]["presence_knob"] == "lively"
    assert p.get_effective_group_profile("qq:GroupMessage:88002")["effective"]["reply_probability_threshold"] == 55.0
    await p.terminate()


# ---------------------------------------------------------------------------
# 5. Quote-bot generation failure controlled fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quote_bot_generation_failure_sends_controlled_fallback(jev_plugin):
    p, bridge = jev_plugin

    async def fail_generate(*_args, **_kwargs):
        raise RuntimeError("simulated_generation_timeout")

    bridge.generate = fail_generate
    p.jev.payload = answers(action="reply")

    quote = Reply("prev-bot-msg")
    quote.sender_id = "bot_42"
    ev = MockEvent("你刚才那句什么意思？", message_id="qfail-1", sender_id="u1", components=[quote])
    try:
        await p.on_group_message(ev)
        await flush(p, ev)
        await drain(p)
        assert ev.replies_sent, "Quote-bot turn that failed during generation must deliver controlled fallback"
        runtime = p._sessions[ev.unified_msg_origin]
        outcome = read_outcome(runtime.dag.get_node("qfail-1"))
        assert outcome["final_outcome"] == "delivered"
        assert outcome["suppression_reason"] == "quote_reply_fallback"
    finally:
        await p.terminate()


# ---------------------------------------------------------------------------
# 6. Unified 4-tier presets, Shadow Health Checkup, Replay reveal & UI contracts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unified_presets_shadow_health_replay_reveal_and_ui_contracts():
    p = _plugin({"takeover_all": True, "shadow_mode": True, "reply_probability_threshold": 70.0})
    try:
        # 4 unified presets with diff
        catalog = p.preset_catalog()
        assert set(catalog["presets"].keys()) == {"observe", "quiet", "sensible", "lively"}
        assert "diff" in catalog["presets"]["lively"]
        assert any(d["key"] == "shadow_mode" for d in catalog["presets"]["lively"]["diff"])

        # Legacy aliases still work
        res_alias = await p.apply_preset("balanced")
        assert res_alias["name"] == "balanced"
        assert p.shadow_mode is False

        # Shadow health checkup summary
        p.shadow_mode = True
        now = time.time()
        for i in range(6):
            p._shadow_decisions.append(
                {
                    "session_key": "qq:GroupMessage:9001",
                    "action": "reply" if i < 3 else "ignore",
                    "reason": "natural_topic_fit" if i < 3 else "below_threshold",
                    "probability": 0.68 + i * 0.01,
                    "timestamp": now - (6 - i) * 120,
                }
            )
        ov = snapshot_overview(p)
        sh = ov["shadow_health"]
        assert sh["total_samples"] == 6
        assert sh["speak_count"] == 3
        assert sh["quiet_count"] == 3
        assert sh["estimated_per_hour"] > 0
        assert len(sh["borderline_top5"]) == 5
        assert sh["ready_for_go_live"] is True

        # Replay redaction vs temporary reveal
        rt = p._get_or_create_runtime("qq:GroupMessage:9001", group_id="9001", umo="qq:GroupMessage:9001", bot_id="bot_42")
        rt.dag.add_message("rev-1", "u1", "这是敏感测试原文", time.time())
        snap_redacted = scene_replay_snapshot(p, session_key="qq:GroupMessage:9001")
        assert snap_redacted["topic_content_redacted"] is True
        assert snap_redacted["topic_blocks"][0]["messages"][0]["text"] == "消息内容已隐藏"

        snap_reveal = scene_replay_snapshot(p, session_key="qq:GroupMessage:9001", show_content_override=True)
        assert snap_reveal["topic_content_redacted"] is False
        assert snap_reveal["temporary_reveal"] is True
        assert "敏感测试原文" in snap_reveal["topic_blocks"][0]["messages"][0]["text"]

        # Frontend & i18n contracts
        replay_html = (ROOT / "pages" / "app" / "replay" / "index.html").read_text(encoding="utf-8")
        console_html = (ROOT / "pages" / "app" / "console" / "index.html").read_text(encoding="utf-8")
        nav_js = (ROOT / "pages" / "app" / "shared" / "plugin_nav.js").read_text(encoding="utf-8")
        cfg_css = (ROOT / "pages" / "app" / "config" / "style.css").read_text(encoding="utf-8")
        learn_meta = json.loads((ROOT / "pages" / "app" / "learning" / "_page.json").read_text(encoding="utf-8"))

        assert 'id="replayRedactionBanner"' in replay_html
        assert 'id="btnToggleTemporaryReveal"' in replay_html
        assert 'id="dryRunSandbox"' in replay_html
        assert '<details class="annotation-section" id="annotationDetails">' in replay_html
        assert 'id="groupProfilePanel"' in console_html
        assert 'id="shadowHealthCard"' in console_html
        assert 'id="btnShadowGoLive"' in console_html
        assert 'id="presetDiffPreview"' in console_html
        assert "plugin-nav-more" in nav_js and "更多与运维" in nav_js
        assert "position: sticky; bottom: 0;" in cfg_css
        assert learn_meta["title"]["i18n_key"] == "pages.learning.title"
        assert learn_meta["description"]["i18n_key"] == "pages.learning.desc"
    finally:
        await p.terminate()
