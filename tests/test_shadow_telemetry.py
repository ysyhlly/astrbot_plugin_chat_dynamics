from astrbot_plugin_chat_dynamics.core.learning_policy import shadow_decision
from astrbot_plugin_chat_dynamics.core.shadow_telemetry import ShadowTelemetry
import asyncio
from types import SimpleNamespace

import pytest


def comparison(now=100):
    return shadow_decision(score=.6, level="weak", evidence_codes=[], has_prior_bot=True,
                           baseline_threshold=.7, params={"strong_addressivity_threshold": .5},
                           policy_id="p", now=now)


def test_dedup_survives_restore_and_sessions_remain_isolated():
    store = ShadowTelemetry()
    assert store.record(comparison(), session="group secret", message_id="1", host_version="v1")
    restored = ShadowTelemetry()
    restored.salt = store.salt
    restored.restore(store.export(100), 100)
    assert not restored.record(comparison(), session="group secret", message_id="1", host_version="v1")
    assert restored.record(comparison(), session="other", message_id="1", host_version="v1")
    assert "group secret" not in str(restored.export(100))
    assert len(restored.rows) == 2


def test_a_backwards_clock_step_does_not_wipe_the_observations():
    """墙钟回拨（NTP/快照恢复）不能把整个 A/B 观测集删光。"""
    store = ShadowTelemetry()
    store.record(comparison(1000), session="s", message_id="1", host_version="v1")
    assert len(store.rows) == 1

    store.prune(880)  # 时钟倒退两分钟

    assert len(store.rows) == 1


def test_limits_expiry_and_unlabelled_real_comparisons():
    store = ShadowTelemetry(retention_seconds=10, max_records=2)
    for i in range(3):
        store.record(comparison(100+i), session="s", message_id=str(i), host_version="v1")
    rows = store.export(102)["observations"]
    assert len(rows) == 2
    assert all(row["changed"] and not row["baseline_reply"] and row["shadow_reply"] for row in rows)
    assert not any("label" in row or "text" in row for row in rows)
    assert store.export(113)["observations"] == []


def test_restore_discards_extra_content_and_invalid_timestamp():
    store = ShadowTelemetry()
    store.record(comparison(), session="s", message_id="1", host_version="v1")
    payload = store.export(100)
    payload["observations"][0]["text"] = "private"
    restored = ShadowTelemetry()
    restored.restore(payload, 100)
    assert "private" not in str(restored.export(100))


@pytest.mark.asyncio
async def test_plugin_kv_save_restore_and_failed_save_is_retryable():
    from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
    from astrbot_plugin_chat_dynamics.core.shadow_telemetry import KV_KEY, SALT_KEY

    db = {}
    metrics = []

    async def read(key, default):
        return db.get(key, default)

    async def write(key, value):
        db[key] = value

    def plugin():
        instance = ChatDynamicsPlugin.__new__(ChatDynamicsPlugin)
        instance.shadow_telemetry = ShadowTelemetry()
        instance._shadow_persist_lock = asyncio.Lock()
        instance._time_service = SimpleNamespace(wall_time=lambda: 100)
        instance.get_kv_data = read
        instance.put_kv_data = write
        instance._metric = metrics.append
        return instance

    first = plugin()
    await first._load_shadow_telemetry()
    first.shadow_telemetry.record(comparison(), session="s", message_id="1", host_version="v1")
    await first._save_shadow_telemetry()
    assert len(db[KV_KEY]["observations"]) == 1

    assert SALT_KEY in db and "salt" not in db[KV_KEY]
    second = plugin()
    await second._load_shadow_telemetry()
    assert not second.shadow_telemetry.record(comparison(), session="s", message_id="1", host_version="v1")
    second.put_kv_data = lambda *_: False
    await second._save_shadow_telemetry()
    assert metrics == ["shadow_telemetry_persist_failed"]
    second.put_kv_data = write
    await second._save_shadow_telemetry()
    assert len(db[KV_KEY]["observations"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("shadow", 1), ("off", 0)])
async def test_message_pipeline_records_only_real_shadow_comparison(mode, expected):
    from astrbot_plugin_chat_dynamics.core.learning_policy import Decision
    from astrbot_plugin_chat_dynamics.core.time_service import VirtualClock
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key

    clock = VirtualClock(initial_time=1000)
    plugin = _plugin({"learning_policy_mode": mode, "debounce_base_cooldown": .1}, clock=clock)
    plugin.learning_policy.consumer.decision = Decision(
        mode=mode, status="shadow", policy_id="runtime-policy",
        overrides={"strong_addressivity_threshold": .5})
    plugin.learning_policy.last_read_at = clock.wall_time()
    event = MockEvent("小助手在吗", group_id="shadow_group", message_id="shadow-message",
                      is_at_or_wake_command=True)
    try:
        await plugin.on_group_message(event)
        await clock.advance(2)
        rows = plugin.shadow_telemetry.export(clock.wall_time())["observations"]
        assert len(rows) == expected
        node = plugin._sessions[_session_key("shadow_group")].dag.get_node("shadow-message")
        assert node is not None
        if expected:
            assert rows[0]["policy_id"] == "runtime-policy"
            assert rows[0]["baseline_reply"] == node.metadata["shadow_decision"]["baseline_reply"]
            assert rows[0]["changed"] == node.metadata["shadow_decision"]["changed"]
        else:
            assert "shadow_decision" not in node.metadata
    finally:
        await plugin.terminate()
