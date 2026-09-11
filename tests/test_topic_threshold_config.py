"""Configuration compatibility and bounds for independent topic thresholds."""

import json
from pathlib import Path

import pytest

from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config


def test_defaults_preserve_legacy_topic_threshold_mapping():
    config, warnings = parse_runtime_config({})
    assert config.topic_join_threshold == 0.48
    assert config.topic_commit_threshold == 0
    assert config.topic_ambiguity_threshold == 0
    assert config.topic_margin_threshold == 0.06
    assert not warnings


def test_explicit_thresholds_are_independent_of_legacy_setting():
    config, warnings = parse_runtime_config({
        "topic_join_threshold": 0.52,
        "topic_commit_threshold": 0.75,
        "topic_ambiguity_threshold": 0.41,
        "topic_margin_threshold": 0.12,
    })
    assert config.topic_join_threshold == 0.52
    assert config.topic_commit_threshold == 0.75
    assert config.topic_ambiguity_threshold == 0.41
    assert config.topic_margin_threshold == 0.12
    assert not warnings


@pytest.mark.parametrize("field", ["topic_commit_threshold", "topic_ambiguity_threshold"])
@pytest.mark.parametrize("value", [0, 0.30, 0.95, "0.65"])
def test_commit_and_ambiguity_accept_sentinel_and_bounds(field, value):
    config, warnings = parse_runtime_config({field: value})
    assert getattr(config, field) == float(value)
    assert not warnings


@pytest.mark.parametrize("field", ["topic_commit_threshold", "topic_ambiguity_threshold"])
@pytest.mark.parametrize("value", [-0.01, 0.01, 0.29, 0.951, float("nan"), float("inf")])
def test_commit_and_ambiguity_reject_out_of_range(field, value):
    config, warnings = parse_runtime_config({field: value})
    assert getattr(config, field) == 0
    assert any(field in warning for warning in warnings)


@pytest.mark.parametrize("value", [-0.01, 0.501, float("nan"), float("inf")])
def test_margin_rejects_invalid_values(value):
    config, warnings = parse_runtime_config({"topic_margin_threshold": value})
    assert config.topic_margin_threshold == 0.06
    assert any("topic_margin_threshold" in warning for warning in warnings)


@pytest.mark.parametrize("value", [0, 0.5])
def test_margin_accepts_bounds(value):
    config, warnings = parse_runtime_config({"topic_margin_threshold": value})
    assert config.topic_margin_threshold == value
    assert not warnings


def test_schema_and_runtime_defaults_agree():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    config, _ = parse_runtime_config({})
    for field in ("topic_join_threshold", "topic_commit_threshold", "topic_ambiguity_threshold", "topic_margin_threshold"):
        assert schema[field]["default"] == getattr(config, field)


def test_live_plugin_applies_independent_thresholds_and_legacy_fallback():
    from .test_plugin_lifecycle import _plugin

    plugin = _plugin({"topic_join_threshold": 0.52})
    resolver = plugin.thread_router.topic_resolver
    assert (resolver.join_threshold, resolver.ambiguity_threshold, resolver.margin_threshold) == (0.62, 0.52, 0.06)
    plugin.config.update(topic_commit_threshold=0.75, topic_ambiguity_threshold=0.41, topic_margin_threshold=0.12)
    plugin._sync_runtime_from_config()
    assert (resolver.join_threshold, resolver.ambiguity_threshold, resolver.margin_threshold) == (0.75, 0.41, 0.12)
    plugin.config.update(topic_commit_threshold=0, topic_ambiguity_threshold=0, topic_join_threshold=0.65)
    plugin._sync_runtime_from_config()
    assert (resolver.join_threshold, resolver.ambiguity_threshold) == (0.65, 0.65)
