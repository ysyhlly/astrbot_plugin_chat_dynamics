"""v1.3 unit tests: manners, occasion, mood forget, reminder one-shot, selflearning degrade."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate
from astrbot_plugin_chat_dynamics.core.group_memory import GroupMemoryNotebook
from astrbot_plugin_chat_dynamics.core.mood_memory import MoodMemoryStore
from astrbot_plugin_chat_dynamics.core.occasion_skin import OccasionClassifier, OccasionKind
from astrbot_plugin_chat_dynamics.core.selflearning_bridge import SelfLearningBridge, SelfLearningStatus
from astrbot_plugin_chat_dynamics.core.social_manners import SocialMannersGate
from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode


def test_config_v13_defaults_are_conservative():
    cfg, warnings = parse_runtime_config({})
    assert cfg.presence_knob == "sensible"
    assert cfg.social_manners_enabled is True
    assert cfg.relay_baton_enabled is True
    assert cfg.private_field_enabled is True
    assert cfg.hyped_quota_enabled is True
    assert cfg.mood_memory_enabled is False
    assert cfg.slang_trial_enabled is False
    assert cfg.group_memory_enabled is True
    assert cfg.selflearning_integration is True
    assert not any("presence_knob" in w for w in warnings)


def test_presence_knob_invalid_falls_back():
    cfg, warnings = parse_runtime_config({"presence_knob": "chaotic"})
    assert cfg.presence_knob == "sensible"
    assert warnings


def _node(msg_id, user_id, reply_to="", mentions=None):
    return SimpleNamespace(
        msg_id=msg_id,
        user_id=user_id,
        reply_to_id=reply_to,
        mentioned_users=list(mentions or []),
        text="",
    )


def test_relay_baton_silences_human_exchange():
    gate = SocialMannersGate()
    nodes = [
        _node("1", "A"),
        _node("2", "B", reply_to="1"),
        _node("3", "A", reply_to="2"),
    ]
    verdict = gate.evaluate(
        session_id="g1",
        user_id="A",
        text="嗯继续说",
        recent_nodes=nodes,
        bot_id="bot",
        explicit=False,
        occasion_kind="neutral",
    )
    assert verdict.allow is False
    assert verdict.reason_code == "relay_baton"


def test_private_field_conservative():
    gate = SocialMannersGate()
    nodes = [
        _node("1", "A"),
        _node("2", "B", reply_to="1"),
        _node("3", "A", reply_to="2"),
        _node("4", "B", reply_to="3"),
    ]
    verdict = gate.evaluate(
        session_id="g1",
        user_id="B",
        text="晚点再说细节",
        recent_nodes=nodes,
        bot_id="bot",
        explicit=False,
        occasion_kind="neutral",
        relay_baton_enabled=False,
        private_field_enabled=True,
    )
    assert verdict.allow is False
    assert verdict.reason_code == "private_field"


def test_hyped_quota_one_join_per_banter_segment():
    gate = SocialMannersGate()
    now = time.time()
    first = gate.evaluate(
        session_id="g1",
        user_id="u1",
        text="哈哈笑死",
        recent_nodes=[],
        bot_id="bot",
        explicit=False,
        occasion_kind="banter",
        now=now,
    )
    assert first.allow is True
    gate.note_intervene("g1", occasion_kind="banter", hyped=True, now=now)
    second = gate.evaluate(
        session_id="g1",
        user_id="u2",
        text="绝了",
        recent_nodes=[],
        bot_id="bot",
        explicit=False,
        occasion_kind="banter",
        now=now + 1,
    )
    assert second.allow is False
    assert second.reason_code == "hyped_quota"
    # @ may still reply
    named = gate.evaluate(
        session_id="g1",
        user_id="u2",
        text="@bot 你觉得呢",
        recent_nodes=[],
        bot_id="bot",
        explicit=True,
        occasion_kind="banter",
        now=now + 2,
    )
    assert named.allow is True


def test_occasion_conflict_prefers_silence():
    clf = OccasionClassifier()
    skin = clf.classify(session_id="g1", text="别吵了你们有病吧", vibe_mode=GroupChatMode.FAST_BANTER)
    assert skin.kind == OccasionKind.CONFLICT
    assert skin.silence_bias >= 0.85

    gate = DynamicsDecisionGate()
    cfg = parse_runtime_config({})[0]
    result = gate.evaluate(
        session_id="g1",
        user_id="u1",
        text="别吵了你们有病吧",
        vibe_mode=GroupChatMode.FAST_BANTER,
        explicit=False,
        willingness=0.9,
        cfg=cfg,
    )
    assert result.should_speak is False
    assert result.reason_code in {"conflict_silence", "occasion_silence"}


def test_cool_command_lowers_presence_immediately():
    clf = OccasionClassifier()
    assert clf.is_cool_command("今天别闹了好不好")
    skin = clf.classify(session_id="g1", text="今天别闹", presence_knob="lively", now=1000.0)
    assert skin.cool_command is True
    assert clf.cool_remaining("g1", now=1001.0) > 0


def test_memory_files_strip_colon_and_land_on_disk(tmp_path: Path):
    umo = "aiocqhttp:GroupMessage:123"
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.remember(umo, "peerA", ["加班"], now=1000.0)
    nb = GroupMemoryNotebook(tmp_path)
    nb.configure(enabled=True)
    nb.add_reminder(umo, text="记得交周报", due_at=1000.0)
    files = {path.name for path in tmp_path.iterdir() if path.is_file()}
    assert files
    assert all(":" not in name for name in files)
    assert any(name.startswith("mood_") and name.endswith(".json") for name in files)
    assert any(name.startswith("notebook_") and name.endswith(".json") for name in files)
    store2 = MoodMemoryStore(tmp_path)
    store2.configure(enabled=True)
    assert store2.recall(umo, "peerA", now=1001.0)
    nb2 = GroupMemoryNotebook(tmp_path)
    assert nb2.list_all(umo)["reminders"]


def test_mood_forget_stops_recall(tmp_path: Path):
    store = MoodMemoryStore(tmp_path)
    store.configure(enabled=True)
    store.remember("umo:1", "peerA", ["加班", "疲惫"], now=1000.0)
    assert store.recall("umo:1", "peerA", now=1001.0)
    store.forget("umo:1", "peerA")
    assert store.recall("umo:1", "peerA", now=1002.0) == []


def test_reminder_one_shot_then_expire(tmp_path: Path):
    nb = GroupMemoryNotebook(tmp_path)
    nb.configure(enabled=True)
    nb.add_reminder("umo:1", text="记得交周报", due_at=1000.0)
    due = nb.pop_due_reminders("umo:1", now=1001.0)
    assert len(due) == 1
    assert due[0]["text"] == "记得交周报"
    again = nb.pop_due_reminders("umo:1", now=1002.0)
    assert again == []


def test_selflearning_missing_degrades_quietly():
    bridge = SelfLearningBridge(context=None, enabled=True)
    status = bridge.refresh()
    assert status in {SelfLearningStatus.MISSING, SelfLearningStatus.DEGRADED}
    assert bridge.fetch_approved_memories(umo="x") == []
    assert bridge.fetch_slang_candidates(umo="x") == []
    snap = bridge.snapshot()
    assert snap["status"] in {"missing", "degraded"}
    assert snap["weakened"]


def test_selflearning_disabled_is_missing():
    bridge = SelfLearningBridge(context=object(), enabled=False)
    assert bridge.refresh() == SelfLearningStatus.MISSING
    assert bridge.snapshot()["lamp"] == "已关闭"


def test_ghost_presence_blocks_ambient():
    gate = DynamicsDecisionGate()
    cfg = parse_runtime_config({"presence_knob": "ghost"})[0]
    result = gate.evaluate(
        session_id="g1",
        user_id="u1",
        text="今天天气不错",
        explicit=False,
        willingness=0.8,
        cfg=cfg,
    )
    assert result.should_speak is False
    assert result.reason_code == "presence_ghost"


def test_scene_track_records_speak_and_silent_without_plaintext():
    from astrbot_plugin_chat_dynamics.core.dashboard import merge_replay_blocks, scene_replay_snapshot
    from astrbot_plugin_chat_dynamics.core.occasion_skin import OccasionSkin

    gate = DynamicsDecisionGate()
    now = 1_800_000_000.0
    quiet = gate.evaluate(
        session_id="replay-g",
        user_id="u1",
        text="今天天气不错",
        explicit=False,
        willingness=0.8,
        cfg=parse_runtime_config({"presence_knob": "ghost"})[0],
        now=now,
    )
    assert quiet.should_speak is False
    skin = OccasionSkin(
        kind=OccasionKind.BANTER,
        silence_bias=0.2,
        length_hint="brief",
        force_scale=1.0,
        reason_zh="整活场合接了一句",
    )
    gate.note_spoke("replay-g", skin=skin, now=now + 30)
    track = gate.manners.scene_track("replay-g")
    actions = [row["action"] for row in track]
    assert "silent" in actions and "speak" in actions
    assert all("text" not in row for row in track)
    blocks = merge_replay_blocks(track)
    assert blocks
    assert {block["action"] for block in blocks} >= {"silent", "speak"}

    class _P:
        presence_knob = "sensible"
        decision_gate = gate

        def _resolve_session_key(self, sid):
            return sid

    data = scene_replay_snapshot(_P(), session_key="replay-g")
    assert data["content_redacted"] is True
    assert data["speak_count"] >= 1
    assert data["silent_count"] >= 1
    assert data["empty"] is False
    assert all("text" not in event for event in data["events"])


def test_overview_includes_read_air_and_partner_lamp():
    from astrbot_plugin_chat_dynamics.core.dashboard import snapshot_overview
    from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate
    from astrbot_plugin_chat_dynamics.core.selflearning_bridge import SelfLearningBridge

    class _Vibe:
        def get_telemetrics(self, *_a, **_k):
            return SimpleNamespace(
                mpm=0, token_density=0, emoji_ratio=0, punctuation_formality=0,
                sample_size=0, scene_tags=(), emotion_tags=(), unique_speakers=0,
                unicode_emoji_ratio=0, media_ratio=0,
            )
        def peek_mode(self, *_a, **_k):
            return GroupChatMode.CHILL_FADE
        def mode_source(self, *_a):
            return "telemetrics"
        def llm_snapshot_count(self, *_a):
            return 0
        def has_llm_snapshot(self, *_a):
            return False

    class _Tele:
        def known_session_ids(self):
            return []
        def get_rate_series(self, *_a, **_k):
            return [0] * 12

    class _Arb:
        def cooling_map(self):
            return {}
        def cooling_remaining(self, *_a, **_k):
            return 0.0
        def last_decision(self, *_a, **_k):
            return None

    class _Time:
        def time(self):
            return 0.0

    plugin = SimpleNamespace(
        enabled=True,
        takeover_all=False,
        takeover_groups=set(),
        exclude_groups=set(),
        bot_names=[],
        provider_id="",
        reply_provider_id="",
        vibe_provider_id="",
        pipeline_mode="filter",
        decision_mode="legacy",
        ambient_intervention=False,
        vibe_llm_enabled=False,
        shadow_mode=False,
        console_show_message_content=False,
        presence_knob="sensible",
        social_manners_enabled=True,
        relay_baton_enabled=True,
        private_field_enabled=True,
        hyped_quota_enabled=True,
        mood_memory_enabled=False,
        slang_trial_enabled=False,
        group_memory_enabled=True,
        selflearning_integration=True,
        selflearning=SelfLearningBridge(enabled=True),
        decision_gate=DynamicsDecisionGate(),
        _sessions={},
        dags={},
        _last_bot_nodes={},
        _vibe_msg_counts={},
        _vibe_llm_tasks_by_session={},
        _background_tasks=set(),
        _vibe_llm_tasks=set(),
        _hook_tasks_by_session={},
        _metrics={},
        _shadow_decisions=[],
        _config_warnings_seen=set(),
        _runtime_config=parse_runtime_config({})[0],
        telemetrics=_Tele(),
        vibe_analyzer=_Vibe(),
        arbiter=_Arb(),
        time_service=_Time(),
        embeddings=SimpleNamespace(enabled=False, provider_id="", last_backend="hashed", cache_len=0),
        debounce=SimpleNamespace(get_pending_count=lambda **_k: 0),
        addressivity_router=SimpleNamespace(bot_id=""),
        persona_engine=None,
        llm=SimpleNamespace(),
        is_group_takeover_enabled=lambda _gid: False,
    )
    data = snapshot_overview(plugin)
    assert "read_air" in data
    assert "selflearning" in data
    assert data["presence_knob"] == "sensible"
    assert data["social_manners"]["relay_baton"] is True
    assert data["selflearning"]["status"] in {"missing", "degraded", "connected"}
