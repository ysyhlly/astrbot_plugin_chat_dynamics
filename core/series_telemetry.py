"""Series-wide LLM token, cache, latency, and error telemetry store."""

from __future__ import annotations

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

    def add(
        self,
        *,
        usage: UsageSnapshot,
        queue_ms: float,
        generation_ms: float,
        ok: bool,
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
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self.retention_days = max(1, int(retention_days))
        self.flush_interval_seconds = max(0.0, float(flush_interval_seconds))
        self.flush_threshold = max(1, int(flush_threshold))
        self.rolling_limit = max(16, int(rolling_limit))

        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._pending_count = 0
        self._last_flush_monotonic = time.monotonic()
        self._last_prune_wall = 0.0

        # Pending deltas keyed by (bucket, plugin_id, provider_id, purpose)
        self._pending_hourly: dict[tuple[str, str, str, str], _BucketDelta] = {}
        self._pending_daily: dict[tuple[str, str, str, str], _BucketDelta] = {}

        # Rolling recent samples keyed by (plugin_id, provider_id, purpose)
        self._queue_samples: dict[tuple[str, str, str], deque[float]] = {}
        self._gen_samples: dict[tuple[str, str, str], deque[float]] = {}

        if self.db_path is not None:
            self._init_db()

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
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (bucket, plugin_id, provider_id, purpose)
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_bucket ON {table} (bucket)"
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
        now: float | None = None,
    ) -> UsageSnapshot:
        clean_plugin = str(plugin_id or "unknown").strip()[:128] or "unknown"
        clean_provider = str(provider_id or "default").strip()[:128] or "default"
        clean_purpose = str(purpose or "background").strip()[:64] or "background"
        _ = error_type  # reserved for future breakdown; counted via ok=False

        q_ms = max(0.0, float(queue_seconds or 0.0) * 1000.0)
        g_ms = max(0.0, float(generation_seconds or 0.0) * 1000.0)
        if not math.isfinite(q_ms):
            q_ms = 0.0
        if not math.isfinite(g_ms):
            g_ms = 0.0

        usage = extract_usage(response)
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
                usage=usage, queue_ms=q_ms, generation_ms=g_ms, ok=bool(ok)
            )
            self._pending_daily.setdefault(d_key, _BucketDelta()).add(
                usage=usage, queue_ms=q_ms, generation_ms=g_ms, ok=bool(ok)
            )
            self._pending_count += 1

            mono = time.monotonic()
            if self._conn is not None and (
                self._pending_count >= self.flush_threshold
                or (mono - self._last_flush_monotonic) >= self.flush_interval_seconds
            ):
                self._flush_locked(now=now)
        return usage

    def flush(self, *, now: float | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_locked(now=now)

    def _flush_locked(self, *, now: float | None = None) -> None:
        if self._conn is None:
            return
        if not self._pending_hourly and not self._pending_daily:
            self._last_flush_monotonic = time.monotonic()
            return
        wall = int(time.time() if now is None else float(now))
        upsert_sql = """
            INSERT INTO {table} (
                bucket, plugin_id, provider_id, purpose,
                calls, ok_calls, error_calls, usage_missing_calls,
                input_tokens, output_tokens, total_tokens,
                cached_tokens, cache_creation_tokens,
                queue_ms_sum, generation_ms_sum, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        wall,
                    )
                    for (bucket, pid, prov, purp), d in self._pending_daily.items()
                ]
                cur.executemany(upsert_sql.format(table="telemetry_daily"), rows)
            self._conn.commit()
            self._pending_hourly.clear()
            self._pending_daily.clear()
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
                           queue_ms_sum, generation_ms_sum
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
                        ),
                    )
            finally:
                cur.close()

        pending = self._pending_hourly if granularity == "hour" else self._pending_daily
        for key, delta in pending.items():
            if start_bucket <= key[0] <= end_bucket:
                _merge(key, delta)

        return [(b, p, prov, purp, d) for (b, p, prov, purp), d in merged.items()]

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

        with self._lock:
            if not self._closed:
                self._flush_locked(now=now)
            rows = self._collect_rows_locked(granularity, start_bucket, end_bucket)

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
            "timeline": timeline,
            "live_queue": live_queue if isinstance(live_queue, dict) else default_queue,
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._flush_locked()
            finally:
                self._closed = True
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
