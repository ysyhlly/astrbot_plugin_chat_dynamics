"""Ordering and sweep regressions without wall-clock performance assertions."""
import pytest

from astrbot_plugin_chat_dynamics.core.debounce import DebounceBuffer
from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from .test_plugin_lifecycle import MockContext
from .test_runtime_persistence import plugin as persistence_plugin
from astrbot_plugin_chat_dynamics.core.runtime_persistence import restore_runtime_state


def test_recent_window_and_capacity_use_timestamp_order():
    dag = ConversationDAG(max_nodes=10, ttl_seconds=0)
    for mid, stamp in [("a", 10), ("b", 30), ("late", 5), ("c", 30)]:
        dag.add_message(mid, "u", mid, timestamp=stamp)
    assert [n.msg_id for n in dag.get_recent_nodes(2)] == ["b", "c"]
    assert dag.last_timestamp() == 30
    dag.add_message("older", "u", "older", timestamp=1)
    # Pruning must sort even when no reader has consumed the dirty order yet.
    assert dag.prune(max_nodes=3, current_time=40) == 2
    assert [n.msg_id for n in dag.get_recent_nodes(0)] == ["a", "b", "c"]
    dag.reset()
    dag.add_message("new", "u", "new", timestamp=2)
    assert dag.last_timestamp() == 2


def test_restored_snapshot_rebuilds_order_before_selecting_recent_window():
    target = persistence_plugin(100)
    restore_runtime_state(target, {
        "version": 1, "saved_wall": 1000, "saved_clock": 100,
        "sessions": [{"session_key": "room", "umo": "room", "nodes": [
            {"msg_id": mid, "user_id": "u", "text": mid, "timestamp": stamp}
            for mid, stamp in [("latest", 99), ("oldest", 1), ("middle", 50)]
        ]}],
    })
    dag = target._registry.get("room").dag
    assert [n.msg_id for n in dag.get_recent_nodes(1)] == ["latest"]
    dag.prune(max_nodes=2, ttl_seconds=0)
    assert [n.msg_id for n in dag.get_recent_nodes(0)] == ["middle", "latest"]


@pytest.mark.asyncio
async def test_debounce_snapshot_tracks_users_and_discard():
    clock = VirtualClock(initial_time=100)
    buffer = DebounceBuffer(time_service=clock, base_cooldown=20)

    async def flush(_result):
        pass

    try:
        await buffer.ingest("a", "u1", "hello", object(), flush)
        await clock.advance(1)
        await buffer.ingest("a", "u2", "hello", object(), flush)
        await buffer.ingest("b", "u1", "hello", object(), flush)
        touched, active = buffer.session_activity_snapshot()
        assert touched == {"a": 101, "b": 101}
        assert active == {"a", "b"}
        await buffer.discard("a")
        assert buffer.session_activity_snapshot()[1] == {"b"}
    finally:
        await buffer.close(flush=False)


@pytest.mark.asyncio
async def test_sweep_uses_one_snapshot_and_keeps_busy_sessions(monkeypatch):
    plugin = ChatDynamicsPlugin(MockContext(), {"enable": True})
    plugin.time_service = VirtualClock(initial_time=100)
    for key in ("old", "busy"):
        plugin._get_or_create_dag(key).add_message(key, "u", "hi", timestamp=10)

    async def flush(_result):
        pass

    await plugin.debounce.ingest("busy", "u", "pending", object(), flush)
    snapshot = plugin.debounce.session_activity_snapshot
    calls = []

    def capture():
        calls.append(True)
        return snapshot()

    def repeated_scan(*_args):
        pytest.fail("the sweep must not scan all debounce slots for each session")

    monkeypatch.setattr(plugin.debounce, "session_activity_snapshot", capture)
    monkeypatch.setattr(plugin.debounce, "last_activity", repeated_scan)
    monkeypatch.setattr(plugin.debounce, "has_active_session", repeated_scan)
    try:
        plugin._prune_idle_sessions(4000)
        assert calls == [True]
        assert "old" not in plugin.dags
        assert "busy" in plugin.dags
        plugin._prune_idle_sessions_after_generation(4010)
        assert len(calls) == 1
        plugin._prune_idle_sessions_after_generation(4030)
        assert len(calls) == 2
        # The periodic/direct sweep remains unconditional.
        plugin._prune_idle_sessions(4031)
        assert len(calls) == 3
    finally:
        await plugin.terminate()
