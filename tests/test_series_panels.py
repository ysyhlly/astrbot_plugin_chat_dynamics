"""Unit tests for series_panels defensive context handling and read-only defaults."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from astrbot_plugin_chat_dynamics.series_panels import (
    get_series_defaults,
    inherit_provider,
    inherit_umos,
    presence,
)


def test_get_series_defaults_handles_mock_and_non_iterable_contexts(tmp_path):
    for ctx in (
        None,
        Mock(),
        MagicMock(),
        SimpleNamespace(get_all_stars=lambda: 123),
        SimpleNamespace(get_all_stars=lambda: "not-stars"),
        SimpleNamespace(get_all_stars=Mock(side_effect=RuntimeError("boom"))),
    ):
        assert get_series_defaults(ctx) == {}
        assert inherit_provider("", ctx) == ""
        assert inherit_provider("explicit", ctx) == "explicit"
        assert inherit_umos([], ctx) == []
        assert inherit_umos(["qq:GroupMessage:1"], ctx) == ["qq:GroupMessage:1"]

        data = presence(ctx, "astrbot_plugin_chat_dynamics", root=tmp_path)
        assert data["schema_version"] == 1
        assert data["series_defaults"] == {}
        assert isinstance(data["plugins"], list)


def test_get_series_defaults_reads_sanitized_allowlisted_keys():
    class ChatOwner:
        def get_series_shared_config(self):
            return {
                "reply_provider": " p-reply ",
                "decision_provider": " p-decision ",
                "takeover_groups": ["qq:GroupMessage:42", ""],
                "persona_id": "anan",
                "ui_language": "zh-CN",
                "api_key": "must-not-leak",
            }

    ctx = SimpleNamespace(
        get_all_stars=lambda: {
            "chat": SimpleNamespace(
                name="astrbot_plugin_chat_dynamics",
                activated=True,
                star_cls=ChatOwner(),
            )
        }
    )
    defaults = get_series_defaults(ctx)
    assert defaults["reply_provider"] == "p-reply"
    assert defaults["decision_provider"] == "p-decision"
    assert defaults["takeover_groups"] == ["qq:GroupMessage:42"]
    assert defaults["persona_id"] == "anan"
    assert defaults["ui_language"] == "zh-CN"
    assert "api_key" not in defaults
