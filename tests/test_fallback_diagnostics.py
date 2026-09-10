from types import SimpleNamespace as NS
from unittest.mock import Mock

from astrbot_plugin_chat_dynamics.core import dashboard
from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate


def _failure():
    return Mock(side_effect=RuntimeError("private-body secret-session-id"))


def _assert_private_diagnostics(caplog, operations):
    for operation in operations:
        assert f"{operation} failed: RuntimeError" in caplog.text
    assert "private-body" not in caplog.text
    assert "secret-session-id" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_dashboard_read_air_logs_failures_and_keeps_fallbacks(caplog):
    gate = NS(
        manners=NS(today_stats=Mock(return_value={"intervene": 0, "quiet": 0, "why_silent": []})),
        useful=NS(quota_status=_failure()),
        rhythm=NS(status=_failure(), why_silent_rows=_failure()),
    )
    plugin = NS(decision_gate=gate, presence_knob="sensible", daily_rhythm_enabled=True, _metrics={})
    result = dashboard._read_air_summary(plugin, [], session_key="secret-session-id")
    assert result["proactive_used"] == 0
    assert result["daily_rhythm"]["state"] == "awake"
    _assert_private_diagnostics(caplog, ["quota_status", "rhythm.status", "rhythm.why_silent_rows"])


def test_dashboard_replay_logs_failures_and_keeps_snapshot(caplog, monkeypatch):
    monkeypatch.setattr(dashboard, "snapshot_sessions", _failure())
    plugin = NS(decision_gate=NS(manners=NS(scene_track=Mock(return_value=[])),
                                rhythm=NS(why_silent_rows=_failure())))
    result = dashboard.scene_replay_snapshot(plugin, session_key="secret-session-id")
    assert result["events"] == []
    assert result["sessions"] == []
    _assert_private_diagnostics(caplog, ["replay.why_silent_rows", "replay.snapshot_sessions"])


def test_gate_best_effort_reset_and_sent_bookkeeping_log_failures(caplog):
    gate = DynamicsDecisionGate()
    gate.occasion.reset_session = _failure()
    gate.rhythm.note_spoke = _failure()
    gate.manners.note_intervene = Mock()
    gate.reset_session("secret-session-id")
    gate.note_spoke("secret-session-id", skin=NS(kind=NS(value="neutral")), rhythm=NS(action=""))
    gate.manners.note_intervene.assert_called_once()
    _assert_private_diagnostics(caplog, ["occasion.reset_session", "rhythm.note_spoke"])
