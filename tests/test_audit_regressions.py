"""Behavior regressions for the verified September audit findings."""
import asyncio
import logging
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from astrbot_plugin_chat_dynamics.core.arbiter import ArbitrationResult
from .test_plugin_lifecycle import MockContext
from .test_thread_router import setup, add


@pytest.fixture
def plugin():
    return ChatDynamicsPlugin(MockContext(), {"decision_mode": "legacy", "enable": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "raise", "cancel"])
async def test_failed_save_restores_values_and_blocks_early_application(plugin, failure):
    before = dict(plugin.config)
    started, release = asyncio.Event(), asyncio.Event()

    async def save():
        started.set()
        await release.wait()
        if failure == "raise":
            raise OSError("disk unavailable")
        return False

    plugin.save_config = save
    task = asyncio.create_task(plugin.save_config_values({"enable": False, "bot_names": ["new"]}))
    await started.wait()
    plugin._sync_runtime_from_config()
    assert plugin._runtime_config.enabled is True
    if failure == "cancel":
        task.cancel()
    else:
        release.set()
    with pytest.raises((RuntimeError, OSError, asyncio.CancelledError)):
        await task
    assert plugin.config == before
    plugin._sync_runtime_from_config()
    assert plugin._runtime_config.enabled is True
    assert not plugin._config_save_in_progress


@pytest.mark.asyncio
async def test_partial_config_assignment_is_rolled_back(plugin):
    class Config(dict):
        def __setitem__(self, key, value):
            if key == "bot_names":
                raise TypeError("read only field")
            super().__setitem__(key, value)

    plugin.config = Config(plugin.config)
    before = dict(plugin.config)
    plugin.save_config = AsyncMock()
    with pytest.raises(RuntimeError, match="failed to set bot_names"):
        await plugin.save_config_values({"enable": False, "bot_names": ["new"]})
    assert plugin.config == before
    plugin.save_config.assert_not_called()


def gate(allow=True, code="proactive", proactive=True):
    verdict = SimpleNamespace(proactive=proactive, as_dict=lambda: {"proactive": proactive})
    return SimpleNamespace(should_speak=allow, reason_code=code, reason_zh="reason",
                           proactive=verdict, rhythm=None, skin=object(),
                           length_hint="short", delay_scale=0.5)


@dataclass
class ExtendedResult(ArbitrationResult):
    extra_diagnostic: str = "preserved"


@pytest.mark.parametrize("threshold", [0.55, 0.65, 0.75])
def test_override_score_matches_threshold_and_preserves_future_fields(plugin, threshold):
    original = ExtendedResult(False, 0.1, threshold, "threshold")
    runtime = SimpleNamespace()
    result = plugin._resolve_gate_result(runtime, "session", original, gate(), 1.0)
    assert result.should_speak and result.willingness_score >= threshold
    assert result.extra_diagnostic == "preserved"
    assert original.should_speak is False
    assert plugin.arbiter.last_decision("session") == result
    assert runtime._pending_gate_skin is not None


@pytest.mark.parametrize("flag", ["in_deep_cooling", "is_energy_asymmetric", "private_topic"])
def test_proactive_cannot_override_hard_block(plugin, flag):
    original = replace(ArbitrationResult(False, 0.1, 0.75, "blocked"), **{flag: True})
    runtime = SimpleNamespace()
    result = plugin._resolve_gate_result(runtime, "session", original, gate(), 1.0)
    assert result is original
    assert not hasattr(runtime, "_pending_gate_skin")


@pytest.mark.parametrize("code,expected", [("no_gap", True), ("newcomer_caution", True), ("media", False)])
def test_gate_veto_priority_preserved(plugin, code, expected):
    original = ExtendedResult(True, 0.8, 0.75, "allowed")
    result = plugin._resolve_gate_result(SimpleNamespace(), "session", original, gate(False, code), 1.0)
    assert result.should_speak is expected
    assert result.extra_diagnostic == "preserved"


def test_router_logs_no_content_or_identifiers_even_at_debug(caplog):
    runtime, router = setup()
    with caplog.at_level(logging.DEBUG):
        add(runtime, router, "SECRET_MESSAGE_ID", "SECRET_USER_ID", "SECRET_CHAT_BODY", 1)
    records = [r for r in caplog.records if "[Router]" in r.getMessage()]
    assert records
    assert all(r.levelno == logging.DEBUG for r in records)
    assert "SECRET_" not in " ".join(r.getMessage() for r in records)
