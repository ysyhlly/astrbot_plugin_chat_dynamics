"""Validation tests for live plugin configuration."""

from __future__ import annotations

import json
from pathlib import Path

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config


def test_replay_limit_is_bounded_by_stored_history_capacity():
    for value in (80, 250, 500):
        cfg, _ = parse_runtime_config({"replay_message_limit": value})
        assert cfg.replay_message_limit == value
    for value in (0, 79, 501, float("inf"), "invalid"):
        cfg, warnings = parse_runtime_config({"replay_message_limit": value})
        assert cfg.replay_message_limit == 500
        if value != "invalid":
            assert any("replay_message_limit" in warning for warning in warnings)


def test_routing_config_bounds_and_legacy_defaults():
    cfg, warnings = parse_runtime_config({"routing_neural_timeout": float("nan")})
    assert cfg.conversation_router_enabled is True
    assert cfg.routing_neural_timeout == 0.5
    assert cfg.neural_embedding_enabled is False
    assert cfg.strong_addressivity_threshold == 0.7
    assert warnings
    cfg, _ = parse_runtime_config({"conversation_router_enabled": False, "routing_neural_timeout": 0})
    assert not cfg.conversation_router_enabled
    assert cfg.routing_neural_timeout == 0


def test_invalid_numeric_values_fall_back_to_safe_defaults():
    cfg, warnings = parse_runtime_config(
        {
            "chars_per_second": 0,
            "max_fragments": -4,
            "max_fragment_chars": 20,
            "inter_burst_interval": 9,
            "deep_cooling_minutes": float("nan"),
            "safe_hover_threshold": 0.9,
            "strong_addressivity_threshold": 0.2,
        }
    )
    assert cfg.chars_per_second == 25.0
    assert cfg.max_fragments == 3
    assert cfg.max_fragment_chars == 120
    assert cfg.inter_burst_interval == 1.2
    assert cfg.deep_cooling_minutes == 15.0
    assert (cfg.safe_hover_threshold, cfg.strong_addressivity_threshold) == (0.4, 0.7)
    assert warnings


def test_debounce_relationships_are_normalized():
    cfg, _ = parse_runtime_config(
        {
            "debounce_base_cooldown": 5,
            "debounce_extended_cooldown": 2,
            "debounce_max_cap": 1,
        }
    )
    assert cfg.debounce_extended_cooldown == 5
    assert cfg.debounce_max_cap == 5


def test_infinite_and_negative_values_fall_back():
    cfg, warnings = parse_runtime_config(
        {
            "chars_per_second": float("inf"),
            "base_thinking_delay": -1,
            "debounce_base_cooldown": float("-inf"),
            "max_fragments": 0,
        }
    )
    assert cfg.chars_per_second == 25.0
    assert cfg.base_thinking_delay == 0.8
    assert cfg.debounce_base_cooldown == 3.5
    assert cfg.max_fragments == 3
    assert warnings


def test_values_above_schema_limits_fall_back_to_defaults():
    cfg, warnings = parse_runtime_config(
        {
            "debounce_base_cooldown": 30.1,
            "debounce_extended_cooldown": 60.1,
            "debounce_max_cap": 120.1,
            "deep_cooling_minutes": 180.1,
            "chars_per_second": 100.1,
            "base_thinking_delay": 10.1,
            "max_fragments": 11,
        }
    )

    assert cfg.debounce_base_cooldown == 3.5
    assert cfg.debounce_extended_cooldown == 6.5
    assert cfg.debounce_max_cap == 12.0
    assert cfg.deep_cooling_minutes == 15.0
    assert cfg.chars_per_second == 25.0
    assert cfg.base_thinking_delay == 0.8
    assert cfg.max_fragments == 3
    assert len(warnings) == 7


def test_schema_boundary_values_are_accepted():
    cfg, warnings = parse_runtime_config(
        {
            "debounce_base_cooldown": 30,
            "debounce_extended_cooldown": 60,
            "debounce_max_cap": 120,
            "deep_cooling_minutes": 180,
            "chars_per_second": 100,
            "base_thinking_delay": 10,
            "max_fragments": 3,
        }
    )

    assert cfg.debounce_base_cooldown == 30
    assert cfg.debounce_extended_cooldown == 60
    assert cfg.debounce_max_cap == 120
    assert cfg.deep_cooling_minutes == 180
    assert cfg.chars_per_second == 100
    assert cfg.base_thinking_delay == 10
    assert cfg.max_fragments == 3
    assert warnings == ()


def test_fractional_fragment_count_is_rejected_instead_of_truncated():
    cfg, warnings = parse_runtime_config({"max_fragments": 2.9})
    assert cfg.max_fragments == 3
    assert any("max_fragments" in warning for warning in warnings)


def test_runtime_defaults_match_schema_defaults():
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    cfg, warnings = parse_runtime_config({})

    assert warnings == ()
    assert cfg.debounce_base_cooldown == schema["debounce_base_cooldown"]["default"]
    assert cfg.debounce_extended_cooldown == schema["debounce_extended_cooldown"]["default"]
    assert cfg.debounce_max_cap == schema["debounce_max_cap"]["default"]
    assert cfg.deep_cooling_minutes == schema["deep_cooling_minutes"]["default"]
    assert cfg.chars_per_second == schema["chars_per_second"]["default"]
    assert cfg.base_thinking_delay == schema["base_thinking_delay"]["default"]
    assert cfg.max_fragments == schema["max_fragments"]["default"]
    assert cfg.max_fragment_chars == schema["max_fragment_chars"]["default"]
    assert cfg.inter_burst_interval == schema["inter_burst_interval"]["default"]
    assert cfg.casual_emoji_enabled is schema["casual_emoji_enabled"]["default"]
    assert cfg.vibe_llm_enabled is schema["vibe_llm_enabled"]["default"]
    assert cfg.reply_provider_id == schema["reply_provider"]["default"]
    assert cfg.vibe_provider_id == schema["vibe_provider"]["default"]
    assert cfg.shadow_mode is schema["shadow_mode"]["default"]
    assert cfg.console_show_message_content is schema["console_show_message_content"]["default"]
    assert cfg.telemetrics_window_seconds == schema["telemetrics_window_seconds"]["default"]
    assert cfg.fast_banter_enter_mpm == schema["fast_banter_enter_mpm"]["default"]
    assert cfg.chill_fade_enter_mpm == schema["chill_fade_enter_mpm"]["default"]
    assert cfg.wts_topic_weight == schema["wts_topic_weight"]["default"]
    assert cfg.wts_professionalism_weight == schema["wts_professionalism_weight"]["default"]
    assert cfg.wts_question_weight == schema["wts_question_weight"]["default"]
    assert cfg.wts_participation_weight == schema["wts_participation_weight"]["default"]
    assert cfg.wts_fatigue_weight == schema["wts_fatigue_weight"]["default"]
    assert cfg.neural_embedding_enabled is schema["neural_embedding_enabled"]["default"]
    assert cfg.embedding_provider == schema["embedding_provider"]["default"]
    assert cfg.neural_link_threshold == schema["neural_link_threshold"]["default"]
    assert cfg.embedding_cache_size == schema["embedding_cache_size"]["default"]
    assert cfg.pipeline_mode == schema["pipeline_mode"]["default"]
    assert cfg.ambient_intervention is schema["ambient_intervention"]["default"]
    assert cfg.vibe_llm_enabled is schema["vibe_llm_enabled"]["default"]
    assert cfg.pipeline_mode == "filter"
    assert cfg.ambient_intervention is False
    assert cfg.vibe_llm_enabled is False
    assert cfg.decision_mode == schema["decision_mode"]["default"] == "legacy"
    assert cfg.decision_provider_id == schema["decision_provider"]["default"]
    assert cfg.decision_timeout == schema["decision_timeout"]["default"]
