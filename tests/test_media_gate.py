"""v1.3.1 media air-gate: L1 speak/force, privacy, L2 off, multimodal degrade."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate
from astrbot_plugin_chat_dynamics.core.media_gate import MediaAirGate, classify_image_label, classify_voice_label


def test_config_media_defaults():
    cfg, warnings = parse_runtime_config({})
    assert cfg.media_image_gate_enabled is True
    assert cfg.media_voice_gate_enabled is True
    assert cfg.media_understand_reply_enabled is False
    assert cfg.media_privacy_strict is True
    assert not any("media_" in w for w in warnings)


def test_meme_mutual_less_speak():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="哈哈",
        has_media=True,
        media_component_types=["Image"],
        explicit=False,
    )
    assert v.allow_speak is False
    assert v.force_scale < 0.5
    assert "meme" in ",".join(v.labels) or v.reason_code in {"media_meme_listen", "media_listen"}


def test_help_screenshot_at_allows():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="帮我看下这个报错",
        has_media=True,
        media_component_types=["image"],
        explicit=True,
        quoted_bot=True,
    )
    assert v.allow_speak is True
    assert any("help" in lab for lab in v.labels)
    # @/引用 bot 时即使 L2 关也要把图交给模型
    assert v.request_understand is True


def test_short_voice_ack_silence():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="嗯",
        has_media=True,
        media_component_types=["Record"],
        explicit=False,
    )
    assert v.allow_speak is False
    assert v.reason_code == "voice_low_info"
    assert "语音·信息量低" in v.reason_zh


def test_private_field_images_silence():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="[图片]",
        has_media=True,
        media_component_types=["image"],
        explicit=False,
        private_field_hint=True,
    )
    assert v.allow_speak is False
    assert v.reason_code == "media_others_field"
    assert "别人的场" in v.reason_zh


def test_privacy_skip_no_understand_no_memory():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="这是我身份证正面",
        has_media=True,
        media_component_types=["image"],
        explicit=False,
        media_privacy_strict=True,
    )
    assert v.allow_speak is False
    assert v.privacy_hit is True
    assert v.skip_memory is True
    assert v.request_understand is False
    assert "隐私" in v.reason_zh


def test_privacy_addressed_ack_without_l2():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="身份证帮我看下能不能过",
        has_media=True,
        media_component_types=["image"],
        explicit=True,
        media_privacy_strict=True,
        media_understand_reply_enabled=True,
    )
    assert v.allow_speak is True
    assert v.skip_memory is True
    assert v.request_understand is False  # never full multimodal describe for privacy


def test_l2_off_gate_only_even_when_help():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="看这个报错怎么弄",
        has_media=True,
        media_component_types=["image"],
        explicit=False,
        media_understand_reply_enabled=False,
    )
    assert v.allow_speak is True
    assert v.request_understand is False


def test_addressed_image_requests_understand_even_when_l2_off():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="[图片]",
        has_media=True,
        media_component_types=["Image"],
        explicit=True,
        media_understand_reply_enabled=False,
    )
    assert v.allow_speak is True
    assert v.request_understand is True


def test_l2_on_strong_relevance_requests_understand():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="帮我看下这个报错",
        has_media=True,
        media_component_types=["image"],
        explicit=True,
        media_understand_reply_enabled=True,
        multimodal_available=True,
    )
    assert v.allow_speak is True
    assert v.request_understand is True


def test_missing_multimodal_degrades_l2():
    gate = MediaAirGate()
    gate.set_multimodal_available(False)
    v = gate.evaluate(
        text="帮我看下这个报错",
        has_media=True,
        media_component_types=["image"],
        explicit=True,
        media_understand_reply_enabled=True,
        multimodal_available=False,
    )
    assert v.allow_speak is True
    assert v.request_understand is False
    assert v.multimodal_degraded is True


def test_poke_component_does_not_open_media_gate():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="[戳一戳] 有人戳了你",
        has_media=False,
        media_component_types=["Poke"],
        explicit=True,
    )
    assert v.reason_code == "no_media"
    assert v.has_image is False
    assert v.request_understand is False


def test_uncertain_listens():
    gate = MediaAirGate()
    v = gate.evaluate(
        text="随便发一张",
        has_media=True,
        media_component_types=["File"],
        explicit=False,
    )
    assert v.allow_speak is False
    assert v.reason_code in {"media_listen", "media_meme_listen"}


def test_goodnight_sticker_still_enters_rhythm():
    gate = DynamicsDecisionGate()
    cfg = parse_runtime_config({"rhythm_timezone": "Asia/Shanghai"})[0]
    result = gate.evaluate(
        session_id="gn-sticker",
        now=datetime(2026, 9, 11, 15, tzinfo=timezone.utc).timestamp(),
        user_id="u1",
        text="晚安～",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        has_media=True,
        media_component_types=["image"],
        telemetrics=type("T", (), {"mpm": 1.0, "scene_tags": (), "emotion_tags": ()})(),
        recent_nodes=[],
    )
    assert result.rhythm is not None
    assert result.rhythm.state == "winding_down"
    assert result.should_speak is True
    assert result.rhythm.action == "goodnight_reply"


def test_privacy_image_still_blocks_goodnight_caption():
    gate = DynamicsDecisionGate()
    cfg = parse_runtime_config({})[0]
    result = gate.evaluate(
        session_id="gn-id",
        user_id="u1",
        text="晚安，这是身份证",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        has_media=True,
        media_component_types=["image"],
    )
    assert result.should_speak is False
    assert result.reason_code == "image_privacy_skip"


def test_decision_gate_wires_media_silence():
    gate = DynamicsDecisionGate()
    cfg = parse_runtime_config({})[0]
    result = gate.evaluate(
        session_id="g1",
        user_id="u1",
        text="嗯",
        explicit=False,
        willingness=0.9,
        cfg=cfg,
        has_media=True,
        media_component_types=["record"],
    )
    assert result.should_speak is False
    assert result.reason_code == "voice_low_info"
    assert result.media is not None


def test_decision_gate_help_at_allows_with_media():
    gate = DynamicsDecisionGate()
    cfg = parse_runtime_config({"media_understand_reply_enabled": False})[0]
    result = gate.evaluate(
        session_id="g1",
        user_id="u1",
        text="帮我看看这个截图怎么弄",
        explicit=True,
        willingness=1.0,
        cfg=cfg,
        has_media=True,
        media_component_types=["image"],
        quoted_bot=True,
    )
    assert result.should_speak is True
    assert result.request_understand is True
    assert result.media is not None
    assert result.media.allow_speak is True


def test_media_gate_exception_degrades_quietly():
    gate = MediaAirGate()

    def boom(*_a, **_k):
        raise RuntimeError("vision down")

    # Force failure inside by monkeypatching detect via broken types object
    class BadTypes:
        def __iter__(self):
            raise RuntimeError("bad")

    v = gate.evaluate(has_media=True, media_component_types=BadTypes())  # type: ignore[arg-type]
    assert v.allow_speak is True
    assert v.multimodal_degraded is True or v.reason_code == "media_degrade"


def test_classify_helpers():
    assert classify_image_label(text="哈哈表情包") == "meme_banter"
    assert classify_image_label(text="帮我看报错截图") == "help_screenshot"
    assert classify_image_label(text="身份证复印件", privacy_strict=True) == "private_or_id_sensitive"
    assert classify_voice_label(text="嗯") == "short_ack"
    assert classify_voice_label(text="今天好崩溃想吐槽一下工作") == "long_vent"


def test_overview_includes_media_gate_block():
    from astrbot_plugin_chat_dynamics.core.dashboard import snapshot_overview
    from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate
    from astrbot_plugin_chat_dynamics.core.selflearning_bridge import SelfLearningBridge
    from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode

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
        media_image_gate_enabled=True,
        media_voice_gate_enabled=True,
        media_understand_reply_enabled=False,
        media_privacy_strict=True,
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
    assert "media_gate" in data
    assert data["media_gate"]["image"] is True
    assert data["media_gate"]["understand_reply"] is False
    assert "multimodal" in data["media_gate"]
    assert "media_why_silent" in data["read_air"]
