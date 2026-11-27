from __future__ import annotations

import asyncio
import sqlite3

import pytest

import series_panels
from core.provider_budget import ProviderBudget, ProviderBudgetBusyError, context_budget


class _DummyChatDynamics:
    def __init__(self, profile: str = "auto", groups: list[str] | None = None) -> None:
        self._profile = profile
        self._groups = groups or []

    def get_series_shared_config(self) -> dict:
        return {
            "takeover_all_groups": False,
            "takeover_groups": list(self._groups),
            "exclude_groups": [],
            "bot_names": [],
            "private_shared_group_sources": [],
            "resource_profile": self._profile,
        }


class _DummyStar:
    def __init__(self, owner) -> None:
        self.name = "astrbot_plugin_chat_dynamics"
        self.activated = True
        self.star_cls = owner


class _DummyContext:
    def __init__(self, profile: str = "auto", groups: list[str] | None = None) -> None:
        self._star = _DummyStar(_DummyChatDynamics(profile, groups))

    def get_all_stars(self):
        return [self._star]


def test_resource_profile_auto_and_hysteresis(monkeypatch: pytest.MonkeyPatch) -> None:
    series_panels._HYSTERESIS_STATE.clear()
    series_panels._WATERMARK_CACHE.clear()

    # > 2.5 GiB -> standard
    monkeypatch.setenv("ASTRBOT_RESOURCE_MEMORY_MB", "4096")
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "300")
    ctx_hi = _DummyContext("auto", ["g1", "g2"])
    assert series_panels.resolve_series_resource_profile(ctx_hi) == "standard"

    # 2 GiB + small group -> low_memory
    monkeypatch.setenv("ASTRBOT_RESOURCE_MEMORY_MB", "2048")
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "420")
    ctx_2g = _DummyContext("auto", ["g1", "g2"])
    assert series_panels.resolve_series_resource_profile(ctx_2g) == "low_memory"

    # Sustained multi-group (>=4 groups) triggers hysteresis upgrade on 3rd check
    ctx_multi = _DummyContext("auto", ["g1", "g2", "g3", "g4"])
    assert series_panels.resolve_series_resource_profile(ctx_multi) == "low_memory"
    assert series_panels.resolve_series_resource_profile(ctx_multi) == "low_memory"
    state = series_panels.get_series_resource_state(ctx_multi)
    assert state["effective"] == "low_memory_aggressive"
    assert any(r["code"] == "dynamics_memory_builtin_tokenizer" for r in state["recommendations"])

    # Explicit override always wins
    ctx_exp = _DummyContext("low_memory", ["g1", "g2", "g3", "g4", "g5", "g6"])
    assert series_panels.resolve_series_resource_profile(ctx_exp) == "low_memory"


def test_apply_series_sqlite_pragmas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRBOT_RESOURCE_MEMORY_MB", "2048")
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "300")
    ctx = _DummyContext("low_memory")
    conn = sqlite3.connect(":memory:")
    try:
        limits = series_panels.apply_series_sqlite_pragmas(conn, ctx)
        assert limits["sqlite_cache_kb"] == 1024
        cache_sz = conn.execute("PRAGMA cache_size").fetchone()[0]
        assert cache_sz == -1024
        temp_store = conn.execute("PRAGMA temp_store").fetchone()[0]
        assert temp_store == 1  # FILE
    finally:
        conn.close()


def test_heavy_subprocess_gate_serializes_in_low_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRBOT_RESOURCE_MEMORY_MB", "2048")
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "300")
    ctx = _DummyContext("low_memory")

    async def _run() -> None:
        active = 0
        max_active = 0

        async def worker(idx: int) -> None:
            nonlocal active, max_active
            async with series_panels.heavy_subprocess_slot(
                ctx, f"job-{idx}", plugin_id="test", timeout=5.0
            ):
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.03)
                active -= 1

        await asyncio.gather(worker(1), worker(2), worker(3))
        assert max_active == 1
        snap = series_panels.get_heavy_subprocess_snapshot(ctx)
        assert snap["active_count"] == 0
        assert snap["total_acquired"] == 3

    asyncio.run(_run())


def test_provider_budget_sheds_background_on_hard_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRBOT_RESOURCE_MEMORY_MB", "2048")
    monkeypatch.setenv("ASTRBOT_TEST_RSS_MB", "900")
    ctx = _DummyContext("low_memory")
    budget = context_budget(ctx)
    assert isinstance(budget, ProviderBudget)

    async def _run() -> None:
        with pytest.raises(ProviderBudgetBusyError) as exc_info:
            async with budget.slot("p1", "background_analysis", plugin_id="test"):
                pass
        assert exc_info.value.reason == "memory_pressure"

        # Priority reply lane is still allowed!
        async with budget.slot("p1", "reply", plugin_id="test"):
            pass

    asyncio.run(_run())
