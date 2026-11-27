"""Series-wide LLM token, cache, latency, and error telemetry store."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .series_hub import CORE_SERIES_PLUGINS

_PLUGIN_LABELS: dict[str, str] = {pid: label for pid, label, _mark, _page in CORE_SERIES_PLUGINS}

VALID_WINDOWS = frozenset({"1h", "24h", "7d", "30d"})
_WINDOW_CONFIG: dict[str, tuple[str, int]] = {
    "1h": ("hour", 1),
    "24h": ("hour", 24),
    "7d": ("day", 7),
    "30d": ("day", 30),
}
_DEFAULT_RETENTION_DAYS = 30
_ROLLING_SAMPLE_LIMIT = 512
_FLUSH_INTERVAL_SECONDS = 2.0
_FLUSH_PENDING_THRESHOLD = 32
_PRUNE_INTERVAL_SECONDS = 3600.0
_MAX_DIM_GROUPS = 256

# Approximate per-1M-token USD pricing tuples: (input, cached_read, cache_creation, output).
# Used for cost visibility & budget guardrails; callers may override via set_pricing().
DEFAULT_MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "deepseek-chat": (0.27, 0.07, 0.27, 1.10),
    "deepseek-reasoner": (0.55, 0.14, 0.55, 2.19),
    "deepseek": (0.27, 0.07, 0.27, 1.10),
    "gemini-2.5-flash": (0.15, 0.0375, 0.15, 0.60),
    "gemini-2.5-pro": (1.25, 0.3125, 1.25, 10.00),
    "gemini-3": (0.30, 0.075, 0.30, 2.50),
    "gemini": (0.30, 0.075, 0.30, 1.50),
    "claude-3-5-haiku": (0.80, 0.08, 1.00, 4.00),
    "claude-sonnet": (3.00, 0.30, 3.75, 15.00),
    "claude-opus": (15.00, 1.50, 18.75, 75.00),
    "gpt-4o-mini": (0.15, 0.075, 0.15, 0.60),
    "gpt-4o": (2.50, 1.25, 2.50, 10.00),
    "gpt-4.1-mini": (0.40, 0.10, 0.40, 1.60),
    "gpt-4.1": (2.00, 0.50, 2.00, 8.00),
    "qwen-plus": (0.40, 0.10, 0.40, 1.20),
    "qwen-turbo": (0.05, 0.02, 0.05, 0.20),
    "qwen": (0.30, 0.08, 0.30, 0.90),
}
DEFAULT_IMAGE_COST_USD = 0.04


def estimate_cost_usd(
    usage: UsageSnapshot,
    *,
    model: str = "",
    provider_id: str = "",
    image_count: int = 0,
    pricing: dict[str, tuple[float, float, float, float]] | None = None,
    image_unit_cost: float = DEFAULT_IMAGE_COST_USD,
) -> float:
    """Estimate USD cost from token usage + generated image count using substring pricing lookup."""
    cost = max(0, int(image_count or 0)) * max(0.0, float(image_unit_cost))
    if usage.usage_missing and usage.total_tokens <= 0:
        return round(cost, 6)
    table = pricing if pricing is not None else DEFAULT_MODEL_PRICING
    haystack = f"{model} {provider_id}".strip().lower()
    rates: tuple[float, float, float, float] | None = None
    if haystack:
        for key in sorted(table, key=len, reverse=True):
            if key.lower() in haystack:
                rates = table[key]
                break
    if rates is not None:
        in_rate, cache_read_rate, cache_write_rate, out_rate = rates
        cached = min(usage.input_tokens, usage.cached_tokens)
        creation = min(max(0, usage.input_tokens - cached), usage.cache_creation_tokens)
        fresh_in = max(0, usage.input_tokens - cached - creation)
        cost += (
            fresh_in * in_rate
            + cached * cache_read_rate
            + creation * cache_write_rate
            + usage.output_tokens * out_rate
        ) / 1_000_000.0
    return round(max(0.0, cost), 6)


def _in_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@dataclass(frozen=True)
class UsageSnapshot:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    usage_missing: bool = True


def _get_field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    return None


def _first_int(obj: Any, *names: str) -> int | None:
    for name in names:
        val = _as_nonneg_int(_get_field(obj, name))
        if val is not None:
            return val
    return None


def _extract_from_usage_obj(usage: Any) -> UsageSnapshot | None:
    if usage is None or isinstance(usage, (str, bytes, int, float, bool)):
        return None

    # OpenAI / Anthropic / DashScope / Gemini field names
    input_tokens = _first_int(
        usage,
        "prompt_tokens",
        "input_tokens",
        "prompt_token_count",
        "promptTokenCount",
        "input_token_count",
        "inputTokenCount",
    )
    output_tokens = _first_int(
        usage,
        "completion_tokens",
        "output_tokens",
        "candidates_token_count",
        "candidatesTokenCount",
        "output_token_count",
        "outputTokenCount",
    )
    total_tokens = _first_int(
        usage,
        "total_tokens",
        "total_token_count",
        "totalTokenCount",
    )

    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None

    in_tok = input_tokens or 0
    out_tok = output_tokens or 0
    tot_tok = total_tokens if total_tokens is not None else (in_tok + out_tok)

    # Cached tokens across OpenAI, Anthropic, Gemini, DashScope
    prompt_details = _get_field(usage, "prompt_tokens_details") or _get_field(
        usage, "input_tokens_details"
    )
    cached_tokens = _first_int(prompt_details, "cached_tokens", "cache_read_tokens")
    if cached_tokens is None:
        cached_tokens = _first_int(
            usage,
            "cache_read_input_tokens",
            "cached_content_token_count",
            "cachedContentTokenCount",
            "cached_tokens",
            "cache_read_tokens",
            "prompt_cache_hit_tokens",
        )
    cache_creation = _first_int(
        usage,
        "cache_creation_input_tokens",
        "cache_creation_tokens",
    )
    if cache_creation is None:
        cache_creation = _first_int(prompt_details, "cache_creation_tokens")

    cached_val = cached_tokens or 0
    creation_val = cache_creation or 0
    # Anthropic reports input_tokens excluding cache_read/cache_creation; normalize so
    # input_tokens represents total prompt input for consistent cache_hit_rate <= 1.0.
    is_anthropic_split = (
        _as_nonneg_int(_get_field(usage, "cache_read_input_tokens")) is not None
        or _as_nonneg_int(_get_field(usage, "cache_creation_input_tokens")) is not None
    ) and _as_nonneg_int(_get_field(usage, "prompt_tokens")) is None
    if is_anthropic_split:
        in_tok = in_tok + cached_val + creation_val
        if total_tokens is None:
            tot_tok = in_tok + out_tok

    if in_tok > 0 and cached_val > in_tok:
        cached_val = in_tok

    return UsageSnapshot(
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok,
        cached_tokens=cached_val,
        cache_creation_tokens=creation_val,
        usage_missing=False,
    )


def extract_usage(response: Any) -> UsageSnapshot:
    """Extract normalized token/cache usage across OpenAI, Anthropic, Gemini, and DashScope."""
    if response is None:
        return UsageSnapshot(usage_missing=True)

    # Breadth-first probe over known response wrapper attributes (bounded depth).
    queue: deque[tuple[Any, int]] = deque([(response, 0)])
    visited: set[int] = set()
    while queue:
        current, depth = queue.popleft()
        if current is None or isinstance(current, (str, bytes, int, float, bool)):
            continue
        obj_id = id(current)
        if obj_id in visited:
            continue
        visited.add(obj_id)

        direct = _extract_from_usage_obj(current)
        if direct is not None:
            return direct

        if depth >= 3:
            continue

        for attr in (
            "usage",
            "usage_metadata",
            "usageMetadata",
            "raw_completion",
            "llm_response",
            "raw_response",
            "_raw_response",
            "response",
            "metadata",
        ):
            child = _get_field(current, attr)
            if child is not None and not isinstance(child, (str, bytes, int, float, bool)):
                queue.append((child, depth + 1))

    return UsageSnapshot(usage_missing=True)


def _utc_dt(now: float | None = None) -> datetime:
    stamp = time.time() if now is None else float(now)
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def _hour_bucket(dt: datetime) -> str:
    return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")


def _day_bucket(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * pct)
    return round(ordered[idx], 2)


def _safe_rate(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return round(max(0.0, min(1.0, num / den)), 4)


@dataclass
class _BucketDelta:
    calls: int = 0
    ok_calls: int = 0
    error_calls: int = 0
    usage_missing_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    queue_ms_sum: float = 0.0
    generation_ms_sum: float = 0.0
    image_count: int = 0
    estimated_cost_usd: float = 0.0

    def add(
        self,
        *,
        usage: UsageSnapshot,
        queue_ms: float,
        generation_ms: float,
        ok: bool,
        image_count: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        self.calls += 1
        if ok:
            self.ok_calls += 1
        else:
            self.error_calls += 1
        if usage.usage_missing:
            self.usage_missing_calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.total_tokens += usage.total_tokens
        self.cached_tokens += usage.cached_tokens
        self.cache_creation_tokens += usage.cache_creation_tokens
        self.queue_ms_sum += queue_ms
        self.generation_ms_sum += generation_ms
        self.image_count += max(0, int(image_count or 0))
        self.estimated_cost_usd += max(0.0, float(estimated_cost_usd or 0.0))


class SeriesTelemetryStore:
    """Rolling in-memory latency windows + WAL SQLite hourly/daily bucket persistence."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        flush_interval_seconds: float = _FLUSH_INTERVAL_SECONDS,
        flush_threshold: int = _FLUSH_PENDING_THRESHOLD,
        rolling_limit: int = _ROLLING_SAMPLE_LIMIT,
        pricing: dict[str, tuple[float, float, float, float]] | None = None,
        image_unit_cost: float = DEFAULT_IMAGE_COST_USD,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self.retention_days = max(1, int(retention_days))
        self.flush_interval_seconds = max(0.0, float(flush_interval_seconds))
        self.flush_threshold = max(1, int(flush_threshold))
        self.rolling_limit = max(16, int(rolling_limit))
        self._pricing: dict[str, tuple[float, float, float, float]] = dict(
            DEFAULT_MODEL_PRICING if pricing is None else pricing
        )
        self._image_unit_cost = max(0.0, float(image_unit_cost))

        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._pending_count = 0
        self._last_flush_monotonic = time.monotonic()
        self._last_prune_wall = 0.0
        self._dirty_event = threading.Event()
        self._flush_event = threading.Event()
        self._flush_thread: threading.Thread | None = None

        # Pending deltas keyed by (bucket, plugin_id, provider_id, purpose)
        self._pending_hourly: dict[tuple[str, str, str, str], _BucketDelta] = {}
        self._pending_daily: dict[tuple[str, str, str, str], _BucketDelta] = {}
        # Pending error counts keyed by (day_bucket, plugin_id, provider_id, error_type)
        self._pending_errors: dict[tuple[str, str, str, str], int] = {}
        # Pending dimension deltas keyed by (day_bucket, dim_type, dim_key, plugin_id)
        self._pending_dims: dict[tuple[str, str, str, str], _BucketDelta] = {}

        # Rolling recent samples keyed by (plugin_id, provider_id, purpose)
        self._queue_samples: dict[tuple[str, str, str], deque[float]] = {}
        self._gen_samples: dict[tuple[str, str, str], deque[float]] = {}

        if self.db_path is not None:
            self._init_db()
            self._start_flush_worker()

    def set_pricing(
        self,
        pricing: dict[str, tuple[float, float, float, float]],
        *,
        image_unit_cost: float | None = None,
    ) -> None:
        with self._lock:
            self._pricing = dict(pricing)
            if image_unit_cost is not None:
                self._image_unit_cost = max(0.0, float(image_unit_cost))

    def _has_pending_locked(self) -> bool:
        return bool(
            self._pending_hourly
            or self._pending_daily
            or self._pending_errors
            or self._pending_dims
        )

    def _start_flush_worker(self) -> None:
        if self._conn is None or self._flush_thread is not None:
            return

        def _worker() -> None:
            while not self._closed:
                with self._lock:
                    if self._closed or self._conn is None:
                        break
                    has_pending = self._has_pending_locked()
                    if not has_pending:
                        self._dirty_event.clear()
                if not has_pending:
                    # Wait indefinitely while idle to avoid periodic wakeups.
                    self._dirty_event.wait(timeout=None)
                    if self._closed:
                        break
                self._flush_event.wait(timeout=max(0.25, self.flush_interval_seconds))
                self._flush_event.clear()
                if self._closed:
                    break
                with self._lock:
                    if self._closed or self._conn is None:
                        break
                    if self._has_pending_locked():
                        self._flush_locked()

        t = threading.Thread(
            target=_worker,
            name="cd-series-telemetry-flush",
            daemon=True,
        )
        t.start()
        self._flush_thread = t

    def _init_db(self) -> None:
        if self.db_path is None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            from .. import series_panels as _series_panels

            _series_panels.apply_series_sqlite_pragmas(conn)
        except Exception:
            try:
                import series_panels as _series_panels

                _series_panels.apply_series_sqlite_pragmas(conn)
            except Exception:
                pass
        for table in ("telemetry_hourly", "telemetry_daily"):
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    bucket TEXT NOT NULL,
                    plugin_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    calls INTEGER NOT NULL DEFAULT 0,
                    ok_calls INTEGER NOT NULL DEFAULT 0,
                    error_calls INTEGER NOT NULL DEFAULT 0,
                    usage_missing_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    queue_ms_sum REAL NOT NULL DEFAULT 0.0,
                    generation_ms_sum REAL NOT NULL DEFAULT 0.0,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (bucket, plugin_id, provider_id, purpose)
                )
                """
            )
            existing_cols = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "image_count" not in existing_cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN image_count INTEGER NOT NULL DEFAULT 0"
                )
            if "estimated_cost_usd" not in existing_cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN estimated_cost_usd REAL NOT NULL DEFAULT 0.0"
                )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_bucket ON {table} (bucket)"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_errors (
                bucket TEXT NOT NULL,
                plugin_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                error_type TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bucket, plugin_id, provider_id, error_type)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_errors_bucket ON telemetry_errors (bucket)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_dims_daily (
                bucket TEXT NOT NULL,
                dim_type TEXT NOT NULL,
                dim_key TEXT NOT NULL,
                plugin_id TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                error_calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                image_count INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bucket, dim_type, dim_key, plugin_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_dims_bucket ON telemetry_dims_daily (bucket)"
        )
        conn.commit()
        self._conn = conn
        self.prune_old()

    def record(
        self,
        *,
        plugin_id: str,
        provider_id: str,
        purpose: str,
        response: Any = None,
        queue_seconds: float = 0.0,
        generation_seconds: float = 0.0,
        ok: bool = True,
        error_type: str = "",
        model: str = "",
        group_id: str = "",
        image_count: int = 0,
        now: float | None = None,
    ) -> UsageSnapshot:
        clean_plugin = str(plugin_id or "unknown").strip()[:128] or "unknown"
        clean_provider = str(provider_id or "default").strip()[:128] or "default"
        clean_purpose = str(purpose or "background").strip()[:64] or "background"
        clean_error = str(error_type or "").strip()[:64]
        clean_model = str(model or "").strip()[:128]
        if not clean_model and response is not None:
            for attr in ("model", "model_name"):
                raw_m = _get_field(response, attr)
                if isinstance(raw_m, str) and raw_m.strip():
                    clean_model = raw_m.strip()[:128]
                    break
        clean_group = str(group_id or "").strip()[:128]
        clean_images = max(0, int(image_count or 0))

        q_ms = max(0.0, float(queue_seconds or 0.0) * 1000.0)
        g_ms = max(0.0, float(generation_seconds or 0.0) * 1000.0)
        if not math.isfinite(q_ms):
            q_ms = 0.0
        if not math.isfinite(g_ms):
            g_ms = 0.0

        usage = extract_usage(response)
        est_cost = estimate_cost_usd(
            usage,
            model=clean_model,
            provider_id=clean_provider,
            image_count=clean_images,
            pricing=self._pricing,
            image_unit_cost=self._image_unit_cost,
        )
        dt = _utc_dt(now)
        h_bucket = _hour_bucket(dt)
        d_bucket = _day_bucket(dt)

        with self._lock:
            if self._closed:
                return usage
            sample_key = (clean_plugin, clean_provider, clean_purpose)
            self._queue_samples.setdefault(
                sample_key, deque(maxlen=self.rolling_limit)
            ).append(q_ms)
            self._gen_samples.setdefault(
                sample_key, deque(maxlen=self.rolling_limit)
            ).append(g_ms)

            h_key = (h_bucket, clean_plugin, clean_provider, clean_purpose)
            d_key = (d_bucket, clean_plugin, clean_provider, clean_purpose)
            self._pending_hourly.setdefault(h_key, _BucketDelta()).add(
                usage=usage,
                queue_ms=q_ms,
                generation_ms=g_ms,
                ok=bool(ok),
                image_count=clean_images,
                estimated_cost_usd=est_cost,
            )
            self._pending_daily.setdefault(d_key, _BucketDelta()).add(
                usage=usage,
                queue_ms=q_ms,
                generation_ms=g_ms,
                ok=bool(ok),
                image_count=clean_images,
                estimated_cost_usd=est_cost,
            )
            if not ok:
                err_label = clean_error or "unknown_error"
                e_key = (d_bucket, clean_plugin, clean_provider, err_label)
                self._pending_errors[e_key] = self._pending_errors.get(e_key, 0) + 1
            if clean_model:
                m_key = (d_bucket, "model", clean_model, clean_plugin)
                self._pending_dims.setdefault(m_key, _BucketDelta()).add(
                    usage=usage,
                    queue_ms=0.0,
                    generation_ms=0.0,
                    ok=bool(ok),
                    image_count=clean_images,
                    estimated_cost_usd=est_cost,
                )
            if clean_group and len(self._pending_dims) < _MAX_DIM_GROUPS * 4:
                g_key = (d_bucket, "group", clean_group, clean_plugin)
                self._pending_dims.setdefault(g_key, _BucketDelta()).add(
                    usage=usage,
                    queue_ms=0.0,
                    generation_ms=0.0,
                    ok=bool(ok),
                    image_count=clean_images,
                    estimated_cost_usd=est_cost,
                )

            self._pending_count += 1
            self._dirty_event.set()

            mono = time.monotonic()
            should_flush = self._conn is not None and (
                self._pending_count >= self.flush_threshold
                or (mono - self._last_flush_monotonic) >= self.flush_interval_seconds
            )
            if should_flush:
                if _in_running_event_loop() and self._flush_thread is not None:
                    self._flush_event.set()
                else:
                    self._flush_locked(now=now)
        return usage

    def today_usage_summary(self, *, now: float | None = None) -> dict[str, Any]:
        """Fast daily token/cost totals for ProviderBudget soft/hard cap enforcement."""
        day_bucket = _day_bucket(_utc_dt(now))
        with self._lock:
            rows = self._collect_rows_locked("day", day_bucket, day_bucket)
            tokens = sum(d.total_tokens for _, _, _, _, d in rows)
            cost_usd = sum(d.estimated_cost_usd for _, _, _, _, d in rows)
            calls = sum(d.calls for _, _, _, _, d in rows)
        return {
            "bucket": day_bucket,
            "calls": calls,
            "total_tokens": tokens,
            "estimated_cost_usd": round(cost_usd, 6),
        }

    def flush(self, *, now: float | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_locked(now=now)

    def _flush_locked(self, *, now: float | None = None) -> None:
        if self._conn is None:
            return
        if (
            not self._pending_hourly
            and not self._pending_daily
            and not self._pending_errors
            and not self._pending_dims
        ):
            self._last_flush_monotonic = time.monotonic()
            return
        wall = int(time.time() if now is None else float(now))
        upsert_sql = """
            INSERT INTO {table} (
                bucket, plugin_id, provider_id, purpose,
                calls, ok_calls, error_calls, usage_missing_calls,
                input_tokens, output_tokens, total_tokens,
                cached_tokens, cache_creation_tokens,
                queue_ms_sum, generation_ms_sum,
                image_count, estimated_cost_usd, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket, plugin_id, provider_id, purpose) DO UPDATE SET
                calls = calls + excluded.calls,
                ok_calls = ok_calls + excluded.ok_calls,
                error_calls = error_calls + excluded.error_calls,
                usage_missing_calls = usage_missing_calls + excluded.usage_missing_calls,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                cached_tokens = cached_tokens + excluded.cached_tokens,
                cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,
                queue_ms_sum = queue_ms_sum + excluded.queue_ms_sum,
                generation_ms_sum = generation_ms_sum + excluded.generation_ms_sum,
                image_count = image_count + excluded.image_count,
                estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                updated_at = excluded.updated_at
        """
        cur = self._conn.cursor()
        try:
            if self._pending_hourly:
                rows = [
                    (
                        bucket,
                        pid,
                        prov,
                        purp,
                        d.calls,
                        d.ok_calls,
                        d.error_calls,
                        d.usage_missing_calls,
                        d.input_tokens,
                        d.output_tokens,
                        d.total_tokens,
                        d.cached_tokens,
                        d.cache_creation_tokens,
                        d.queue_ms_sum,
                        d.generation_ms_sum,
                        d.image_count,
                        d.estimated_cost_usd,
                        wall,
                    )
                    for (bucket, pid, prov, purp), d in self._pending_hourly.items()
                ]
                cur.executemany(upsert_sql.format(table="telemetry_hourly"), rows)
            if self._pending_daily:
                rows = [
                    (
                        bucket,
                        pid,
                        prov,
                        purp,
                        d.calls,
                        d.ok_calls,
                        d.error_calls,
                        d.usage_missing_calls,
                        d.input_tokens,
                        d.output_tokens,
                        d.total_tokens,
                        d.cached_tokens,
                        d.cache_creation_tokens,
                        d.queue_ms_sum,
                        d.generation_ms_sum,
                        d.image_count,
                        d.estimated_cost_usd,
                        wall,
                    )
                    for (bucket, pid, prov, purp), d in self._pending_daily.items()
                ]
                cur.executemany(upsert_sql.format(table="telemetry_daily"), rows)
            if self._pending_errors:
                err_rows = [
                    (bucket, pid, prov, etype, cnt, wall)
                    for (bucket, pid, prov, etype), cnt in self._pending_errors.items()
                ]
                cur.executemany(
                    """
                    INSERT INTO telemetry_errors (bucket, plugin_id, provider_id, error_type, calls, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bucket, plugin_id, provider_id, error_type) DO UPDATE SET
                        calls = calls + excluded.calls,
                        updated_at = excluded.updated_at
                    """,
                    err_rows,
                )
            if self._pending_dims:
                dim_rows = [
                    (
                        bucket,
                        dtype,
                        dkey,
                        pid,
                        d.calls,
                        d.error_calls,
                        d.input_tokens,
                        d.output_tokens,
                        d.cached_tokens,
                        d.image_count,
                        d.estimated_cost_usd,
                        wall,
                    )
                    for (bucket, dtype, dkey, pid), d in self._pending_dims.items()
                ]
                cur.executemany(
                    """
                    INSERT INTO telemetry_dims_daily (
                        bucket, dim_type, dim_key, plugin_id,
                        calls, error_calls, input_tokens, output_tokens,
                        cached_tokens, image_count, estimated_cost_usd, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bucket, dim_type, dim_key, plugin_id) DO UPDATE SET
                        calls = calls + excluded.calls,
                        error_calls = error_calls + excluded.error_calls,
                        input_tokens = input_tokens + excluded.input_tokens,
                        output_tokens = output_tokens + excluded.output_tokens,
                        cached_tokens = cached_tokens + excluded.cached_tokens,
                        image_count = image_count + excluded.image_count,
                        estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                        updated_at = excluded.updated_at
                    """,
                    dim_rows,
                )
            self._conn.commit()
            self._pending_hourly.clear()
            self._pending_daily.clear()
            self._pending_errors.clear()
            self._pending_dims.clear()
            self._pending_count = 0
            self._last_flush_monotonic = time.monotonic()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
        finally:
            cur.close()

        if wall - self._last_prune_wall >= _PRUNE_INTERVAL_SECONDS:
            self._prune_old_locked(now=float(wall))

    def prune_old(self, *, now: float | None = None) -> int:
        with self._lock:
            if self._closed:
                return 0
            return self._prune_old_locked(now=now)

    def _prune_old_locked(self, *, now: float | None = None) -> int:
        dt = _utc_dt(now)
        cutoff = dt - timedelta(days=self.retention_days)
        cutoff_hour = _hour_bucket(cutoff)
        cutoff_day = _day_bucket(cutoff)
        self._last_prune_wall = dt.timestamp()

        removed = 0
        for key in [k for k in self._pending_hourly if k[0] < cutoff_hour]:
            self._pending_hourly.pop(key, None)
            removed += 1
        for key in [k for k in self._pending_daily if k[0] < cutoff_day]:
            self._pending_daily.pop(key, None)
            removed += 1
        for key in [k for k in self._pending_errors if k[0] < cutoff_day]:
            self._pending_errors.pop(key, None)
            removed += 1
        for key in [k for k in self._pending_dims if k[0] < cutoff_day]:
            self._pending_dims.pop(key, None)
            removed += 1

        if self._conn is not None:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "DELETE FROM telemetry_hourly WHERE bucket < ?", (cutoff_hour,)
                )
                removed += max(0, cur.rowcount or 0)
                cur.execute(
                    "DELETE FROM telemetry_daily WHERE bucket < ?", (cutoff_day,)
                )
                removed += max(0, cur.rowcount or 0)
                cur.execute(
                    "DELETE FROM telemetry_errors WHERE bucket < ?", (cutoff_day,)
                )
                removed += max(0, cur.rowcount or 0)
                cur.execute(
                    "DELETE FROM telemetry_dims_daily WHERE bucket < ?", (cutoff_day,)
                )
                removed += max(0, cur.rowcount or 0)
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
            finally:
                cur.close()
        return removed

    def _collect_rows_locked(
        self, granularity: str, start_bucket: str, end_bucket: str
    ) -> list[tuple[str, str, str, str, _BucketDelta]]:
        merged: dict[tuple[str, str, str, str], _BucketDelta] = {}

        def _merge(key: tuple[str, str, str, str], delta: _BucketDelta) -> None:
            acc = merged.get(key)
            if acc is None:
                merged[key] = _BucketDelta(
                    calls=delta.calls,
                    ok_calls=delta.ok_calls,
                    error_calls=delta.error_calls,
                    usage_missing_calls=delta.usage_missing_calls,
                    input_tokens=delta.input_tokens,
                    output_tokens=delta.output_tokens,
                    total_tokens=delta.total_tokens,
                    cached_tokens=delta.cached_tokens,
                    cache_creation_tokens=delta.cache_creation_tokens,
                    queue_ms_sum=delta.queue_ms_sum,
                    generation_ms_sum=delta.generation_ms_sum,
                    image_count=delta.image_count,
                    estimated_cost_usd=delta.estimated_cost_usd,
                )
            else:
                acc.calls += delta.calls
                acc.ok_calls += delta.ok_calls
                acc.error_calls += delta.error_calls
                acc.usage_missing_calls += delta.usage_missing_calls
                acc.input_tokens += delta.input_tokens
                acc.output_tokens += delta.output_tokens
                acc.total_tokens += delta.total_tokens
                acc.cached_tokens += delta.cached_tokens
                acc.cache_creation_tokens += delta.cache_creation_tokens
                acc.queue_ms_sum += delta.queue_ms_sum
                acc.generation_ms_sum += delta.generation_ms_sum
                acc.image_count += delta.image_count
                acc.estimated_cost_usd += delta.estimated_cost_usd

        if self._conn is not None:
            table = "telemetry_hourly" if granularity == "hour" else "telemetry_daily"
            cur = self._conn.cursor()
            try:
                cur.execute(
                    f"""
                    SELECT bucket, plugin_id, provider_id, purpose,
                           calls, ok_calls, error_calls, usage_missing_calls,
                           input_tokens, output_tokens, total_tokens,
                           cached_tokens, cache_creation_tokens,
                           queue_ms_sum, generation_ms_sum,
                           image_count, estimated_cost_usd
                    FROM {table}
                    WHERE bucket >= ? AND bucket <= ?
                    """,
                    (start_bucket, end_bucket),
                )
                for row in cur.fetchall():
                    key = (row[0], row[1], row[2], row[3])
                    _merge(
                        key,
                        _BucketDelta(
                            calls=int(row[4]),
                            ok_calls=int(row[5]),
                            error_calls=int(row[6]),
                            usage_missing_calls=int(row[7]),
                            input_tokens=int(row[8]),
                            output_tokens=int(row[9]),
                            total_tokens=int(row[10]),
                            cached_tokens=int(row[11]),
                            cache_creation_tokens=int(row[12]),
                            queue_ms_sum=float(row[13]),
                            generation_ms_sum=float(row[14]),
                            image_count=int(row[15] or 0),
                            estimated_cost_usd=float(row[16] or 0.0),
                        ),
                    )
            finally:
                cur.close()

        pending = self._pending_hourly if granularity == "hour" else self._pending_daily
        for key, delta in pending.items():
            if start_bucket <= key[0] <= end_bucket:
                _merge(key, delta)

        return [(b, p, prov, purp, d) for (b, p, prov, purp), d in merged.items()]

    def _collect_errors_locked(self, start_day: str, end_day: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        if self._conn is not None:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT error_type, SUM(calls)
                    FROM telemetry_errors
                    WHERE bucket >= ? AND bucket <= ?
                    GROUP BY error_type
                    """,
                    (start_day, end_day),
                )
                for etype, cnt in cur.fetchall():
                    counts[str(etype)] = counts.get(str(etype), 0) + int(cnt or 0)
            finally:
                cur.close()
        for (bucket, _pid, _prov, etype), cnt in self._pending_errors.items():
            if start_day <= bucket <= end_day:
                counts[etype] = counts.get(etype, 0) + cnt
        return [
            {"error_type": etype, "calls": cnt}
            for etype, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:30]
        ]

    def _collect_dim_locked(
        self, dim_type: str, start_day: str, end_day: str
    ) -> list[dict[str, Any]]:
        acc_map: dict[str, _BucketDelta] = {}

        def _add(k: str, d: _BucketDelta) -> None:
            cur_d = acc_map.get(k)
            if cur_d is None:
                acc_map[k] = _BucketDelta(
                    calls=d.calls,
                    error_calls=d.error_calls,
                    input_tokens=d.input_tokens,
                    output_tokens=d.output_tokens,
                    total_tokens=d.input_tokens + d.output_tokens,
                    cached_tokens=d.cached_tokens,
                    image_count=d.image_count,
                    estimated_cost_usd=d.estimated_cost_usd,
                )
            else:
                cur_d.calls += d.calls
                cur_d.error_calls += d.error_calls
                cur_d.input_tokens += d.input_tokens
                cur_d.output_tokens += d.output_tokens
                cur_d.total_tokens += d.input_tokens + d.output_tokens
                cur_d.cached_tokens += d.cached_tokens
                cur_d.image_count += d.image_count
                cur_d.estimated_cost_usd += d.estimated_cost_usd

        if self._conn is not None:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT dim_key, SUM(calls), SUM(error_calls),
                           SUM(input_tokens), SUM(output_tokens),
                           SUM(cached_tokens), SUM(image_count), SUM(estimated_cost_usd)
                    FROM telemetry_dims_daily
                    WHERE dim_type = ? AND bucket >= ? AND bucket <= ?
                    GROUP BY dim_key
                    """,
                    (dim_type, start_day, end_day),
                )
                for row in cur.fetchall():
                    _add(
                        str(row[0]),
                        _BucketDelta(
                            calls=int(row[1] or 0),
                            error_calls=int(row[2] or 0),
                            input_tokens=int(row[3] or 0),
                            output_tokens=int(row[4] or 0),
                            cached_tokens=int(row[5] or 0),
                            image_count=int(row[6] or 0),
                            estimated_cost_usd=float(row[7] or 0.0),
                        ),
                    )
            finally:
                cur.close()
        for (bucket, dt_key, dkey, _pid), delta in self._pending_dims.items():
            if dt_key == dim_type and start_day <= bucket <= end_day:
                _add(dkey, delta)
        key_name = "model" if dim_type == "model" else "group_id"
        return [
            {
                key_name: k,
                "calls": d.calls,
                "error_calls": d.error_calls,
                "input_tokens": d.input_tokens,
                "output_tokens": d.output_tokens,
                "cached_tokens": d.cached_tokens,
                "cache_hit_rate": _safe_rate(d.cached_tokens, d.input_tokens),
                "image_count": d.image_count,
                "estimated_cost_usd": round(d.estimated_cost_usd, 6),
            }
            for k, d in sorted(
                acc_map.items(),
                key=lambda kv: (-kv[1].estimated_cost_usd, -kv[1].calls, kv[0]),
            )[:30]
        ]

    def _latency_percentiles_locked(
        self,
        *,
        plugin_id: str | None = None,
        provider_id: str | None = None,
    ) -> tuple[float, float, float, float]:
        q_vals: list[float] = []
        g_vals: list[float] = []
        for (pid, prov, _purp), samples in self._queue_samples.items():
            if plugin_id is not None and pid != plugin_id:
                continue
            if provider_id is not None and prov != provider_id:
                continue
            q_vals.extend(samples)
        for (pid, prov, _purp), samples in self._gen_samples.items():
            if plugin_id is not None and pid != plugin_id:
                continue
            if provider_id is not None and prov != provider_id:
                continue
            g_vals.extend(samples)
        return (
            _percentile(q_vals, 0.50),
            _percentile(q_vals, 0.95),
            _percentile(g_vals, 0.50),
            _percentile(g_vals, 0.95),
        )

    @staticmethod
    def _format_aggregate(
        agg: _BucketDelta,
        p50_q: float,
        p95_q: float,
        p50_g: float,
        p95_g: float,
    ) -> dict[str, Any]:
        calls = agg.calls
        return {
            "calls": calls,
            "ok_calls": agg.ok_calls,
            "error_calls": agg.error_calls,
            "error_rate": _safe_rate(agg.error_calls, calls),
            "usage_missing_calls": agg.usage_missing_calls,
            "input_tokens": agg.input_tokens,
            "output_tokens": agg.output_tokens,
            "total_tokens": agg.total_tokens,
            "cached_tokens": agg.cached_tokens,
            "cache_creation_tokens": agg.cache_creation_tokens,
            "cache_hit_rate": _safe_rate(agg.cached_tokens, agg.input_tokens),
            "avg_queue_ms": round(agg.queue_ms_sum / calls, 2) if calls else 0.0,
            "avg_generation_ms": round(agg.generation_ms_sum / calls, 2) if calls else 0.0,
            "p50_queue_ms": p50_q,
            "p95_queue_ms": p95_q,
            "p50_generation_ms": p50_g,
            "p95_generation_ms": p95_g,
            "image_count": agg.image_count,
            "estimated_cost_usd": round(agg.estimated_cost_usd, 6),
        }

    def snapshot(
        self,
        window: str = "24h",
        *,
        live_queue: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        clean_window = str(window or "24h").strip().lower()
        if clean_window not in VALID_WINDOWS:
            raise ValueError(f"unsupported window: {clean_window}")
        granularity, span = _WINDOW_CONFIG[clean_window]
        dt = _utc_dt(now)
        if granularity == "hour":
            end_bucket = _hour_bucket(dt)
            start_dt = dt - timedelta(hours=max(0, span - 1))
            start_bucket = _hour_bucket(start_dt)
        else:
            end_bucket = _day_bucket(dt)
            start_dt = dt - timedelta(days=max(0, span - 1))
            start_bucket = _day_bucket(start_dt)
        start_day = _day_bucket(start_dt)
        end_day = _day_bucket(dt)

        with self._lock:
            if not self._closed:
                self._flush_locked(now=now)
            rows = self._collect_rows_locked(granularity, start_bucket, end_bucket)
            by_error_type = self._collect_errors_locked(start_day, end_day)
            by_model = self._collect_dim_locked("model", start_day, end_day)
            by_group = self._collect_dim_locked("group", start_day, end_day)

            total = _BucketDelta()
            by_plugin_acc: dict[str, _BucketDelta] = {}
            by_provider_acc: dict[str, _BucketDelta] = {}
            by_bucket_acc: dict[str, _BucketDelta] = {}

            def _accum(target: _BucketDelta, src: _BucketDelta) -> None:
                target.calls += src.calls
                target.ok_calls += src.ok_calls
                target.error_calls += src.error_calls
                target.usage_missing_calls += src.usage_missing_calls
                target.input_tokens += src.input_tokens
                target.output_tokens += src.output_tokens
                target.total_tokens += src.total_tokens
                target.cached_tokens += src.cached_tokens
                target.cache_creation_tokens += src.cache_creation_tokens
                target.queue_ms_sum += src.queue_ms_sum
                target.generation_ms_sum += src.generation_ms_sum
                target.image_count += src.image_count
                target.estimated_cost_usd += src.estimated_cost_usd

            for bucket, pid, prov, _purp, delta in rows:
                _accum(total, delta)
                _accum(by_plugin_acc.setdefault(pid, _BucketDelta()), delta)
                _accum(by_provider_acc.setdefault(prov, _BucketDelta()), delta)
                _accum(by_bucket_acc.setdefault(bucket, _BucketDelta()), delta)

            summary = self._format_aggregate(
                total, *self._latency_percentiles_locked()
            )
            by_plugin = []
            for pid, agg in sorted(
                by_plugin_acc.items(), key=lambda kv: (-kv[1].calls, kv[0])
            ):
                row_data = self._format_aggregate(
                    agg, *self._latency_percentiles_locked(plugin_id=pid)
                )
                by_plugin.append(
                    {
                        "plugin_id": pid,
                        "label": _PLUGIN_LABELS.get(pid, pid),
                        **row_data,
                    }
                )

            by_provider = []
            for prov, agg in sorted(
                by_provider_acc.items(), key=lambda kv: (-kv[1].calls, kv[0])
            ):
                row_data = self._format_aggregate(
                    agg, *self._latency_percentiles_locked(provider_id=prov)
                )
                by_provider.append({"provider_id": prov, **row_data})

            timeline = []
            for bucket in sorted(by_bucket_acc):
                agg = by_bucket_acc[bucket]
                timeline.append(
                    {
                        "bucket": bucket,
                        "granularity": granularity,
                        "calls": agg.calls,
                        "error_calls": agg.error_calls,
                        "input_tokens": agg.input_tokens,
                        "output_tokens": agg.output_tokens,
                        "cached_tokens": agg.cached_tokens,
                        "cache_hit_rate": _safe_rate(agg.cached_tokens, agg.input_tokens),
                        "avg_generation_ms": round(agg.generation_ms_sum / agg.calls, 2)
                        if agg.calls
                        else 0.0,
                        "image_count": agg.image_count,
                        "estimated_cost_usd": round(agg.estimated_cost_usd, 6),
                    }
                )

        default_queue = {
            "capacity": 4,
            "background_capacity": 3,
            "max_queue": 128,
            "active": 0,
            "queued": 0,
            "by_provider": {},
            "by_plugin": {},
        }
        return {
            "window": clean_window,
            "generated_at": dt.astimezone().isoformat(timespec="seconds"),
            "retention_days": self.retention_days,
            "summary": summary,
            "by_plugin": by_plugin,
            "by_provider": by_provider,
            "by_error_type": by_error_type,
            "by_model": by_model,
            "by_group": by_group,
            "timeline": timeline,
            "live_queue": live_queue if isinstance(live_queue, dict) else default_queue,
        }

    def close(self) -> None:
        thread: threading.Thread | None = None
        with self._lock:
            if self._closed:
                return
            try:
                self._flush_locked()
            finally:
                self._closed = True
                self._dirty_event.set()
                self._flush_event.set()
                thread = self._flush_thread
                self._flush_thread = None
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
