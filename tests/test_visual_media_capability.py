from types import SimpleNamespace as NS
import pytest

from astrbot_plugin_chat_dynamics.core.integrations.catalog import companion_snapshot, find_companion


def entries():
    declaration = {
        "schema_version": 1,
        "enabled": True,
        "ready": True,
        "display_name": "视觉工坊",
        "capabilities": {"media": 1},
    }
    visual = NS(
        name="astrbot_plugin_visual_media",
        activated=True,
        version="0.2.0",
        star_cls=NS(get_dynamics_capabilities=lambda: declaration, start_joint_reply_prefetch=lambda event: None),
    )
    old = NS(
        name="astrbot_plugin_stealer",
        activated=False,
        version="3.1",
        star_cls=NS(start_joint_reply_prefetch=lambda event: None),
    )
    return [old, visual], declaration


def test_catalog_and_resolver_select_visual_media_instead_of_disabled_legacy():
    stars, _ = entries()
    context = NS(get_all_stars=lambda: stars)
    row = next(row for row in companion_snapshot(context)["companions"] if row["id"] == "stealer")
    assert row["state"] == "ready"
    assert row["plugin"] == "astrbot_plugin_visual_media"
    assert row["name"] == "视觉工坊 · 视觉表达"
    assert "生图" in row["role"]
    assert find_companion(context, "stealer") is stars[1].star_cls


def test_legacy_fallback_and_conflict_are_explicit():
    stars, declaration = entries()
    context = NS(get_all_stars=lambda: stars)
    stars[0].activated = True
    assert find_companion(context, "stealer") is None
    row = next(row for row in companion_snapshot(context)["companions"] if row["id"] == "stealer")
    assert row["state"] == "conflict"
    stars[1].activated = False
    assert find_companion(context, "stealer") is stars[0].star_cls


@pytest.mark.parametrize(
    "change", [{"ready": False}, {"enabled": False}, {"schema_version": True}, {"capabilities": {"media": True}}]
)
def test_invalid_or_stopping_media_provider_is_not_resolved(change):
    stars, declaration = entries()
    declaration.update(change)
    assert find_companion(NS(get_all_stars=lambda: stars), "stealer") is None


@pytest.mark.parametrize(
    "enabled,internal,allowed",
    [(True, False, True), (False, False, False), (True, True, False), ("true", False, False)],
)
def test_media_authorization_is_scoped_to_admitted_policy_turns(enabled, internal, allowed):
    from astrbot_plugin_chat_dynamics.core.media_reply_policy import media_reply_instructions

    event = NS(get_extra=lambda key, default: enabled)
    result = media_reply_instructions(event, internal=internal)
    assert bool(result) == allowed
    if allowed:
        assert "当前回复会话" in result and "不扩大其他外部工具" in result
        assert "实际调用工具" in result


def test_unrelated_events_do_not_gain_media_tool_permission():
    from astrbot_plugin_chat_dynamics.core.media_reply_policy import media_reply_instructions

    assert media_reply_instructions(object()) == ""
