"""The live gate owns wall time; replay and DAG clocks remain explicit."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot_plugin_chat_dynamics.core.decision_gate import DynamicsDecisionGate


def test_live_bookkeeping_samples_injected_clock_and_keeps_daily_counts():
    clock = Mock(side_effect=[1_800_000_000.0, 1_800_000_001.0, 1_800_000_002.0, 1_800_000_003.0])
    gate = DynamicsDecisionGate(wall_now=clock)
    gate.note_intervene("s")
    gate.note_quiet("s", reason_code="quiet", reason_zh="quiet")
    gate.note_spoke("s", skin=SimpleNamespace(kind=SimpleNamespace(value="neutral")))
    gate.note_arbiter_silence("s", "wts_low")
    stats = gate.manners.today_stats("s")
    assert stats["intervene"] == stats["quiet"] == 2
    assert [row["ts"] for row in stats["why_spoke"]] == [1_800_000_000.0, 1_800_000_002.0]
    assert [row["ts"] for row in stats["why_silent"]] == [1_800_000_001.0, 1_800_000_003.0]
    assert clock.call_count == 4


def test_success_bookkeeping_uses_one_stamp_for_all_components():
    clock = Mock(return_value=1_800_000_000.0)
    gate = DynamicsDecisionGate(wall_now=clock)
    gate.manners.note_intervene = Mock()
    gate.useful.note_proactive = Mock()
    gate.rhythm.note_spoke = Mock()
    gate.note_spoke(
        "s", skin=SimpleNamespace(kind=SimpleNamespace(value="neutral")),
        proactive=SimpleNamespace(proactive=True, reason_code="gap_fill_ok", reason_zh="gap", gap_fingerprint="g"),
        rhythm=SimpleNamespace(action="goodnight_reply"),
    )
    clock.assert_called_once_with()
    for method in (gate.manners.note_intervene, gate.useful.note_proactive, gate.rhythm.note_spoke):
        assert method.call_args.kwargs["now"] == 1_800_000_000.0


@pytest.mark.parametrize("method, kwargs", [
    ("note_quiet", {"reason_code": "quiet", "reason_zh": "quiet"}),
    ("note_intervene", {}),
    ("note_spoke", {"skin": None}),
    ("note_arbiter_silence", {"reason": "wts_low"}),
])
def test_live_bookkeeping_rejects_caller_timestamp(method, kwargs):
    with pytest.raises(TypeError):
        getattr(DynamicsDecisionGate(), method)("s", now=1234.0, **kwargs)


def test_evaluate_keeps_explicit_historical_wall_time():
    clock = Mock(return_value=1_800_000_000.0)
    gate = DynamicsDecisionGate(wall_now=clock)
    gate.rhythm.evaluate = Mock(wraps=gate.rhythm.evaluate)
    gate.useful.evaluate = Mock(wraps=gate.useful.evaluate)
    gate.evaluate(session_id="s", user_id="u", text="hello", explicit=True, now=946684800.0, node_now=1234.0)
    for method in (gate.rhythm.evaluate, gate.useful.evaluate):
        assert method.call_args.kwargs["now"] == 946684800.0
        assert method.call_args.kwargs["node_now"] == 1234.0
    clock.assert_not_called()
    gate.evaluate(session_id="s", user_id="u", text="hello", explicit=True, node_now=1234.0)
    clock.assert_called_once_with()
