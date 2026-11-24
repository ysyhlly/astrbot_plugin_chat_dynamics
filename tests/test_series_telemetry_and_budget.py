"""Unit tests for upgraded ProviderBudget, SeriesTelemetryStore, and series_llm_call helpers."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

import astrbot_plugin_chat_dynamics.series_panels as series_panels
from astrbot_plugin_chat_dynamics.core.llm_adapter import LLMAdapter
from astrbot_plugin_chat_dynamics.core.provider_budget import (
    ProviderBudget,
    ProviderBudgetBusyError,
    attach_context_telemetry,
)
from astrbot_plugin_chat_dynamics.core.series_telemetry import (
    SeriesTelemetryStore,
    extract_usage,
)


def test_extract_usage_across_providers_and_missing():
    # OpenAI format (including nested raw_completion + cached_tokens)
    openai_resp = SimpleNamespace(
        raw_completion=SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=200,
                total_tokens=1200,
                prompt_tokens_details=SimpleNamespace(cached_tokens=600),
            )
        )
    )
    u_oai = extract_usage(openai_resp)
    assert u_oai.usage_missing is False
    assert (u_oai.input_tokens, u_oai.output_tokens, u_oai.total_tokens) == (1000, 200, 1200)
    assert u_oai.cached_tokens == 600

    # Anthropic format (input_tokens + cache_read + cache_creation normalized)
    anth_resp = {
        "usage": {
            "input_tokens": 300,
            "output_tokens": 150,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 200,
        }
    }
    u_anth = extract_usage(anth_resp)
    assert u_anth.usage_missing is False
    assert u_anth.input_tokens == 1000
    assert u_anth.output_tokens == 150
    assert u_anth.cached_tokens == 500
    assert u_anth.cache_creation_tokens == 200
    assert u_anth.total_tokens == 1150

    # Gemini format (usage_metadata + camelCase)
    gemini_resp = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=800,
            candidates_token_count=120,
            total_token_count=920,
            cached_content_token_count=400,
        )
    )
    u_gem = extract_usage(gemini_resp)
    assert u_gem.usage_missing is False
    assert (u_gem.input_tokens, u_gem.output_tokens, u_gem.cached_tokens) == (800, 120, 400)

    gemini_camel = {
        "usageMetadata": {
            "promptTokenCount": 500,
            "candidatesTokenCount": 50,
            "cachedContentTokenCount": 250,
        }
    }
    u_gem2 = extract_usage(gemini_camel)
    assert (u_gem2.input_tokens, u_gem2.output_tokens, u_gem2.cached_tokens) == (500, 50, 250)

    # DashScope format
    dash_resp = {
        "usage": {
            "input_tokens": 600,
            "output_tokens": 100,
            "total_tokens": 700,
            "prompt_tokens_details": {"cached_tokens": 300},
        }
    }
    u_dash = extract_usage(dash_resp)
    assert (u_dash.input_tokens, u_dash.output_tokens, u_dash.cached_tokens) == (600, 100, 300)

    # Missing usage
    u_miss = extract_usage(SimpleNamespace(completion_text="plain text only"))
    assert u_miss.usage_missing is True
    assert (u_miss.input_tokens, u_miss.output_tokens, u_miss.total_tokens) == (0, 0, 0)

    # Wrapped AgentOutput.llm_response
    wrapped = SimpleNamespace(llm_response=openai_resp)
    assert extract_usage(wrapped).cached_tokens == 600


def test_series_telemetry_sqlite_wal_buckets_and_retention(tmp_path):
    db_path = tmp_path / "series-telemetry.sqlite3"
    now = time.time()
    old_ts = now - (35 * 86400)  # 35 days ago (>30d retention)
    recent_day_ts = now - (2 * 86400)  # 2 days ago

    store = SeriesTelemetryStore(db_path, retention_days=30, flush_threshold=1)
    try:
        # Verify WAL mode
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert str(mode).lower() == "wal"

        resp_oai = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 500},
            }
        }
        # Record old entry (should be pruned), 2-day-old entry, and current entries
        store.record(
            plugin_id="astrbot_plugin_dynamics_world",
            provider_id="p-old",
            purpose="life",
            response=resp_oai,
            queue_seconds=0.01,
            generation_seconds=0.5,
            ok=True,
            now=old_ts,
        )
        store.record(
            plugin_id="astrbot_plugin_dynamics_memory",
            provider_id="p-main",
            purpose="extract",
            response=resp_oai,
            queue_seconds=0.02,
            generation_seconds=0.4,
            ok=True,
            now=recent_day_ts,
        )
        store.record(
            plugin_id="astrbot_plugin_chat_dynamics",
            provider_id="p-main",
            purpose="reply",
            response=resp_oai,
            queue_seconds=0.005,
            generation_seconds=0.25,
            ok=True,
            now=now,
        )
        store.record(
            plugin_id="astrbot_plugin_chat_dynamics",
            provider_id="p-main",
            purpose="reply",
            response=SimpleNamespace(completion_text="no usage"),
            queue_seconds=0.015,
            generation_seconds=0.75,
            ok=False,
            error_type="TimeoutError",
            now=now,
        )
        store.flush(now=now)
        pruned = store.prune_old(now=now)
        assert pruned >= 1

        snap_24h = store.snapshot("24h", now=now)
        assert snap_24h["summary"]["calls"] == 2
        assert snap_24h["summary"]["ok_calls"] == 1
        assert snap_24h["summary"]["error_calls"] == 1
        assert snap_24h["summary"]["error_rate"] == 0.5
        assert snap_24h["summary"]["usage_missing_calls"] == 1
        assert snap_24h["summary"]["input_tokens"] == 1000
        assert snap_24h["summary"]["cached_tokens"] == 500
        assert snap_24h["summary"]["cache_hit_rate"] == 0.5
        assert snap_24h["summary"]["p50_generation_ms"] > 0
        assert snap_24h["timeline"][0]["granularity"] == "hour"

        snap_7d = store.snapshot("7d", now=now)
        assert snap_7d["summary"]["calls"] == 3
        assert snap_7d["timeline"][0]["granularity"] == "day"
        assert {row["plugin_id"] for row in snap_7d["by_plugin"]} == {
            "astrbot_plugin_chat_dynamics",
            "astrbot_plugin_dynamics_memory",
        }
        # Old (>30d) entry was cleaned up
        snap_30d = store.snapshot("30d", now=now)
        assert all(row["provider_id"] != "p-old" for row in snap_30d["by_provider"])
    finally:
        store.close()

    # Re-open from SQLite to confirm persistence survived restart
    reloaded = SeriesTelemetryStore(db_path, retention_days=30)
    try:
        snap = reloaded.snapshot("7d", now=now)
        assert snap["summary"]["calls"] == 3
        assert snap["summary"]["input_tokens"] == 2000
        assert snap["summary"]["cached_tokens"] == 1000
    finally:
        reloaded.close()


@pytest.mark.asyncio
async def test_provider_budget_acquire_timeout_and_queue_full(tmp_path):
    store = SeriesTelemetryStore(tmp_path / "tel.sqlite3", flush_threshold=1)
    budget = ProviderBudget(capacity=2, reply_reserve=1, max_queue=1, telemetry=store)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_bg():
        entered.set()
        await release.wait()
        return {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}

    try:
        # 1 background slot (capacity=2, reply_reserve=1 -> background_capacity=1)
        bg1 = asyncio.create_task(
            budget.run("p1", "memory_extract", slow_bg, plugin_id="astrbot_plugin_dynamics_memory")
        )
        await entered.wait()

        # Reply still runs immediately via reserved slot!
        reply_res = await budget.run(
            "p1",
            "reply",
            lambda: {"usage": {"prompt_tokens": 50, "completion_tokens": 10}},
            plugin_id="astrbot_plugin_chat_dynamics",
        )
        assert reply_res["usage"]["prompt_tokens"] == 50

        # Queue 1 background waiter (fills max_queue=1)
        bg2 = asyncio.create_task(
            budget.run(
                "p1",
                "world_life",
                slow_bg,
                plugin_id="astrbot_plugin_dynamics_world",
                acquire_timeout=0.05,
            )
        )
        await asyncio.sleep(0)

        # Third background task hits queue_full immediately
        with pytest.raises(ProviderBudgetBusyError) as full_exc:
            await budget.run(
                "p1",
                "games_bg",
                lambda: "never",
                plugin_id="astrbot_plugin_dynamics_games",
                acquire_timeout=1.0,
            )
        assert full_exc.value.reason == "queue_full"

        # Second waiter hits acquire_timeout
        with pytest.raises(ProviderBudgetBusyError) as to_exc:
            await bg2
        assert to_exc.value.reason == "acquire_timeout"

        # Release bg1 and verify zero leaked slots
        release.set()
        await bg1
        live = budget.live_snapshot()
        assert live["active"] == 0
        assert live["queued"] == 0

        snap = store.snapshot("1h")
        assert snap["summary"]["calls"] == 4
        assert snap["summary"]["ok_calls"] == 2
        assert snap["summary"]["error_calls"] == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_series_llm_call_delegates_translates_busy_and_falls_back(tmp_path):
    # 1) Standalone mode (no Chat Dynamics budget on context)
    standalone_ctx = SimpleNamespace()
    res = await series_panels.series_llm_call(
        standalone_ctx,
        "prov-x",
        "astrbot_plugin_dynamics_games",
        "truth_eval",
        lambda: asyncio.sleep(0, result="standalone-ok"),
        acquire_timeout=1.0,
    )
    assert res == "standalone-ok"
    assert (
        series_panels.record_series_llm_usage(
            standalone_ctx,
            plugin_id="astrbot_plugin_dynamics_games",
            provider_id="prov-x",
            purpose="truth_eval",
        )
        is False
    )

    # 2) Shared ProviderBudget + Telemetry attached on context
    store = SeriesTelemetryStore(tmp_path / "shared.sqlite3", flush_threshold=1)
    ctx = SimpleNamespace()
    budget = attach_context_telemetry(ctx, store)
    budget.capacity = 2
    budget.background_capacity = 1

    try:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            entered.set()
            await release.wait()
            return {"usage": {"prompt_tokens": 200, "completion_tokens": 40}}

        t1 = asyncio.create_task(
            series_panels.series_llm_call(
                ctx,
                "p-shared",
                "astrbot_plugin_dynamics_world",
                "diary",
                hold,
                acquire_timeout=1.0,
            )
        )
        await entered.wait()

        # Companion catches series_panels.ProviderBudgetBusyError (translated from core.provider_budget)
        with pytest.raises(series_panels.ProviderBudgetBusyError) as exc_info:
            await series_panels.series_llm_call(
                ctx,
                "p-shared",
                "astrbot_plugin_dynamics_horizons",
                "summary",
                lambda: "never",
                acquire_timeout=0.03,
            )
        assert exc_info.value.reason == "acquire_timeout"

        release.set()
        await t1

        # Verify LLMAdapter also shares the same budget and telemetry store
        ctx.llm_generate = lambda **kw: SimpleNamespace(
            completion_text="hello",
            usage={"prompt_tokens": 120, "completion_tokens": 30},
        )
        adapter = LLMAdapter(ctx, configured_provider_id="p-shared")
        assert adapter.provider_budget is budget
        assert await adapter.generate(prompt="hi", umo="u1", system_prompt="sys") == "hello"

        snap = store.snapshot("1h")
        by_plugin = {row["plugin_id"]: row for row in snap["by_plugin"]}
        assert by_plugin["astrbot_plugin_dynamics_world"]["input_tokens"] == 200
        assert by_plugin["astrbot_plugin_dynamics_horizons"]["error_calls"] == 1
        assert by_plugin["astrbot_plugin_chat_dynamics"]["input_tokens"] == 120
    finally:
        store.close()


@pytest.mark.asyncio
async def test_chat_dynamics_on_llm_response_records_native_reply_once(tmp_path):
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import _plugin

    plugin = _plugin({"enable": True, "takeover_all": True, "pipeline_mode": "filter", "shadow_mode": False})
    plugin.series_telemetry.close()
    plugin.series_telemetry = SeriesTelemetryStore(tmp_path / "native.sqlite3", flush_threshold=1)
    try:
        event = SimpleNamespace(
            unified_msg_origin="qq:GroupMessage:777",
            get_group_id=lambda: "777",
        )
        resp = SimpleNamespace(
            completion_text="你好呀",
            model="gpt-5",
            usage={"prompt_tokens": 300, "completion_tokens": 60, "prompt_tokens_details": {"cached_tokens": 150}},
        )
        await plugin.on_llm_response(event, resp)
        # Re-entry on same event must not double count
        await plugin.on_llm_response(event, resp)

        snap = plugin.series_telemetry.snapshot("1h")
        assert snap["summary"]["calls"] == 1
        assert snap["summary"]["input_tokens"] == 300
        assert snap["summary"]["cached_tokens"] == 150
        assert snap["summary"]["cache_hit_rate"] == 0.5
    finally:
        plugin.series_telemetry.close()


def test_series_telemetry_cost_error_model_and_group_attribution(tmp_path):
    store = SeriesTelemetryStore(tmp_path / "ext.sqlite3", flush_threshold=1)
    try:
        store.record(
            plugin_id="astrbot_plugin_chat_dynamics",
            provider_id="p-ds",
            purpose="reply",
            model="deepseek-chat",
            group_id="qq:GroupMessage:10001",
            response={
                "usage": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 100_000,
                    "prompt_tokens_details": {"cached_tokens": 500_000},
                }
            },
            ok=True,
        )
        store.record(
            plugin_id="astrbot_plugin_visual_media",
            provider_id="dashscope#1",
            purpose="image_gen",
            model="wanx",
            image_count=2,
            ok=True,
        )
        store.record(
            plugin_id="astrbot_plugin_dynamics_world",
            provider_id="p-ds",
            purpose="life",
            ok=False,
            error_type="RateLimitError",
        )
        store.flush()
        snap = store.snapshot("24h")
        assert snap["summary"]["image_count"] == 2
        assert snap["summary"]["estimated_cost_usd"] > 0.3
        errs = {row["error_type"]: row["calls"] for row in snap["by_error_type"]}
        assert errs["RateLimitError"] == 1
        models = {row["model"]: row for row in snap["by_model"]}
        assert models["deepseek-chat"]["calls"] == 1
        assert models["wanx"]["image_count"] == 2
        groups = {row["group_id"]: row for row in snap["by_group"]}
        assert groups["qq:GroupMessage:10001"]["input_tokens"] == 1_000_000
        today = store.today_usage_summary()
        assert today["total_tokens"] == 1_100_000
        assert today["estimated_cost_usd"] > 0.3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_provider_budget_daily_cap_and_circuit_breaker(tmp_path):
    store = SeriesTelemetryStore(tmp_path / "guard.sqlite3", flush_threshold=1)
    budget = ProviderBudget(
        capacity=3,
        reply_reserve=1,
        telemetry=store,
        daily_token_hard_limit=500,
        circuit_breaker_threshold=2,
        circuit_breaker_base_cooldown=30.0,
    )
    try:
        # 1) Circuit breaker trips on 429 (weight 2) for background, priority reply still passes
        async def fail_429():
            raise RuntimeError("HTTP 429 rate limit exceeded")

        with pytest.raises(RuntimeError):
            await budget.run("p-429", "background", fail_429)

        with pytest.raises(ProviderBudgetBusyError) as cb_exc:
            await budget.run("p-429", "background", lambda: "never")
        assert cb_exc.value.reason == "circuit_open"

        # Priority reply bypasses circuit breaker and resets it on success
        ok_reply = await budget.run(
            "p-429",
            "reply",
            lambda: {"usage": {"prompt_tokens": 400, "completion_tokens": 200}},
        )
        assert ok_reply["usage"]["prompt_tokens"] == 400
        # 2) Now daily_token_hard_limit (500) is exceeded (600 >= 500)
        budget._budget_cache = (0.0, False, False)
        with pytest.raises(ProviderBudgetBusyError) as cap_exc:
            await budget.run("p-429", "background", lambda: "never")
        assert cap_exc.value.reason == "daily_budget_exceeded"
        # Priority reply still allowed even when background daily cap hit
        assert await budget.run("p-429", "reply", lambda: "priority-ok") == "priority-ok"
    finally:
        store.close()


def test_find_provider_silently_and_presence_epoch():
    called = []
    prov = SimpleNamespace(id="qw")
    ctx_with_pm = SimpleNamespace(
        provider_manager=SimpleNamespace(inst_map={"qw": prov}),
        get_provider_by_id=lambda pid: (called.append(pid), None)[1],
    )
    assert series_panels.find_provider_silently(ctx_with_pm, "qw") is prov
    assert series_panels.find_provider_silently(ctx_with_pm, "missing") is None
    # Must NOT have called noisy get_provider_by_id when provider_manager.inst_map exists
    assert called == []

    # Test-double context without provider_manager falls back safely
    ctx_double = SimpleNamespace(get_provider_by_id=lambda pid: prov if pid == "qw" else None)
    assert series_panels.find_provider_silently(ctx_double, "qw") is prov

    # Cross-plugin presence epoch increments on invalidate_presence_cache
    e0 = series_panels.get_presence_epoch(ctx_with_pm)
    series_panels.invalidate_presence_cache("astrbot_plugin_dynamics_world", context=ctx_with_pm)
    e1 = series_panels.get_presence_epoch(ctx_with_pm)
    assert e1 > e0
    assert ctx_with_pm._chat_dynamics_presence_epoch == e1


def test_merge_round_usage_sums_multi_step_tool_loops():
    from astrbot_plugin_chat_dynamics.core.agent_bridge import _merge_round_usage

    r1 = SimpleNamespace(completion_text="", usage={"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 400}})
    r2 = SimpleNamespace(completion_text="", usage={"prompt_tokens": 1400, "completion_tokens": 60, "prompt_tokens_details": {"cached_tokens": 800}})
    r3 = SimpleNamespace(completion_text="final answer", role="assistant", usage={"prompt_tokens": 1800, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 1200}})
    merged = _merge_round_usage([r1, r2, r3], r3)
    assert merged.completion_text == "final answer"
    assert merged.role == "assistant"
    snap = extract_usage(merged)
    assert snap.input_tokens == 4200
    assert snap.output_tokens == 210
    assert snap.cached_tokens == 2400
    assert snap.total_tokens == 4410


def test_build_series_health_aggregates_plugins_and_env_warnings(tmp_path):
    from astrbot_plugin_chat_dynamics.core.series_hub import build_series_health

    world_owner = SimpleNamespace(
        get_dynamics_capabilities=lambda: {"schema_version": 1, "ready": True, "enabled": True},
        get_series_health=lambda: {
            "ready": True,
            "degraded": False,
            "providers": [{"role": "life", "provider_id": "missing-prov", "status": "configured"}],
            "issues": [],
            "metrics": {"active_groups": 2},
        },
    )
    mem_owner = SimpleNamespace(
        get_dynamics_capabilities=lambda: {"schema_version": 1, "ready": True, "enabled": True},
        get_series_health=lambda: {
            "ready": True,
            "degraded": False,
            "providers": [{"role": "embedding", "provider_id": "qw", "status": "configured"}],
            "issues": [],
            "metrics": {},
        },
    )
    stars = [
        SimpleNamespace(name="astrbot_plugin_dynamics_world", activated=True, version="v0.23.0", star_cls=world_owner),
        SimpleNamespace(name="astrbot_plugin_dynamics_memory", activated=True, version="0.2.0", star_cls=mem_owner),
        SimpleNamespace(name="astrbot_plugin_self_learning", activated=True, version="4.2.7", star_cls=SimpleNamespace()),
        SimpleNamespace(name="astrbot_plugin_dynamics_learning", activated=True, version="v2.0.0", star_cls=SimpleNamespace()),
    ]
    ctx = SimpleNamespace(
        get_all_stars=lambda: stars,
        provider_manager=SimpleNamespace(inst_map={"qw": SimpleNamespace(id="qw")}),
        get_config=lambda: {"agent_runner": {"config": {"model": {"provider_id": ""}}}},
    )
    plugin = SimpleNamespace(
        context=ctx,
        enabled=True,
        _shutting_down=False,
        _runtime_config=SimpleNamespace(
            decision_provider_id="",
            reply_provider_id="qw",
            embedding_provider_id="",
            discussion_rerank_provider_id="",
        ),
        get_series_shared_config=lambda: {"reply_provider": "qw", "rhythm_timezone": "Asia/Shanghai"},
        get_series_health=lambda: {"ready": True, "degraded": False, "providers": [{"role": "reply", "provider_id": "qw", "status": "ok"}], "issues": [], "metrics": {}},
    )
    report = build_series_health(plugin)
    assert report["schema_version"] == 1
    assert report["status"] == "degraded"
    by_id = {p["id"]: p for p in report["plugins"]}
    assert by_id["astrbot_plugin_dynamics_world"]["degraded"] is True
    assert by_id["astrbot_plugin_dynamics_world"]["providers"][0]["status"] == "missing"
    assert by_id["astrbot_plugin_dynamics_memory"]["providers"][0]["status"] == "ok"
    warn_codes = {w["code"] for w in report["environment_warnings"]}
    assert "host_default_provider_warn" in warn_codes
    assert "legacy_plugin_conflict" in warn_codes



