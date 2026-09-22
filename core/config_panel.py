"""ConfigPanel operations composed around the plugin runtime host."""
from __future__ import annotations

from typing import Any
import inspect
import json
import math
import re
from astrbot.api import logger
from .config import PIPELINE_FILTER, RuntimeConfig, parse_runtime_config

_PRESETS = {
    "observe": {
        "shadow_mode": True,
    },
    "balanced": {
        "pipeline_mode": PIPELINE_FILTER,
        "shadow_mode": False,
        "ambient_intervention": False,
        "vibe_llm_enabled": False,
        "debounce_base_cooldown": 3.5,
        "debounce_extended_cooldown": 6.5,
        "debounce_max_cap": 12.0,
        "deep_cooling_minutes": 15.0,
        "casual_emoji_enabled": False,
    },
    "active": {
        "pipeline_mode": PIPELINE_FILTER,
        "shadow_mode": False,
        "ambient_intervention": True,
        "vibe_llm_enabled": True,
        "debounce_base_cooldown": 2.0,
        "debounce_extended_cooldown": 4.0,
        "debounce_max_cap": 8.0,
        "deep_cooling_minutes": 10.0,
        "casual_emoji_enabled": True,
    },
}


def _effective_int(cfg: Any, key: str, default: int) -> int:
    val = getattr(cfg, key, None)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


class ConfigPanel:
    def __init__(self, host: Any) -> None:
        self.host = host

    def preset_catalog(self) -> dict[str, Any]:
        current = {}
        for name, values in _PRESETS.items():
            current[name] = {
                "values": dict(values),
                "changes": {
                    key: value
                    for key, value in values.items()
                    if self.host.config.get(key) != value
                },
            }
        return {
            "presets": current,
            "current": {
                "shadow_mode": self.host.shadow_mode,
                "pipeline_mode": self.host.pipeline_mode,
                "ambient_intervention": self.host.ambient_intervention,
                "vibe_llm_enabled": self.host.vibe_llm_enabled,
            },
        }


    async def apply_preset(self, name: str) -> dict[str, Any]:
        async with self.host._config_lock:
            return await self._apply_preset_locked(name)


    def _config_schema(self) -> dict[str, Any]:
        schema = getattr(getattr(self.host, "config", None), "schema", None)
        if isinstance(schema, dict) and schema:
            return schema
        try:
            from pathlib import Path as _Path
            path = _Path(__file__).resolve().parents[1] / "_conf_schema.json"
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}


    def _config_stored_values(self) -> dict[str, Any]:
        raw = self.host._coerce_config(getattr(self.host, "config", {}) or {})
        schema = self._config_schema()
        values: dict[str, Any] = {}
        for key in schema:
            try:
                values[key] = raw.get(key)
            except Exception:
                values[key] = None
        return values


    def get_effective_config(self, *, runtime_config: RuntimeConfig | None = None) -> dict[str, Any]:
        cfg = runtime_config if runtime_config is not None else getattr(self.host, "_runtime_config", None)
        if cfg is None:
            cfg, _ = parse_runtime_config(self.host._coerce_config(getattr(self.host, "config", {}) or {}))
        return {
            "enable": bool(getattr(cfg, "enabled", True)),
            "decision_mode": getattr(cfg, "decision_mode", "legacy"),
            "conversation_router_enabled": getattr(cfg, "conversation_router_enabled", True),
            "routing_neural_timeout": getattr(cfg, "routing_neural_timeout", 0.5),
            "topic_reranker_enabled": cfg.topic_reranker_enabled,
            "topic_reranker_provider": cfg.topic_reranker_provider,
            "topic_reranker_timeout": cfg.topic_reranker_timeout,
            "decision_provider": getattr(cfg, "decision_provider_id", ""),
            "decision_backend": getattr(cfg, "decision_backend", "model"),
            "decision_timeout": getattr(cfg, "decision_timeout", 8.0),
            "jev_base_url": getattr(cfg, "jev_base_url", ""),
            "jev_model": getattr(cfg, "jev_model", "jev-latest"),
            "jev_api_key_env": getattr(cfg, "jev_api_key_env", "TYPESAFE_API_KEY"),
            "jev_timeout": getattr(cfg, "jev_timeout", 6.0),
            "jev_min_confidence": getattr(cfg, "jev_min_confidence", 0.6),
            "laya_base_url": getattr(cfg, "laya_base_url", ""),
            "laya_timeout": getattr(cfg, "laya_timeout", 1.5),
            "laya_min_confidence": getattr(cfg, "laya_min_confidence", 0.6),
            "laya_max_uncertainty": getattr(cfg, "laya_max_uncertainty", 0.25),
            "decision_learning_mode": getattr(cfg, "decision_learning_mode", "off"),
            "decision_learning_sessions": list(getattr(cfg, "decision_learning_sessions", ())),
            "decision_learning_retention_days": getattr(cfg, "decision_learning_retention_days", 30),
            "decision_learning_sample_rate": getattr(cfg, "decision_learning_sample_rate", 0.05),
            "decision_learning_labels_per_hour": getattr(cfg, "decision_learning_labels_per_hour", 120),
            "decision_learning_jev_fallback": getattr(cfg, "decision_learning_jev_fallback", False),
            "laya_internal_hosts": list(getattr(cfg, "laya_internal_hosts", ())),
            "vibe_backend": getattr(cfg, "vibe_backend", "llm"),
            "vibe_min_confidence": getattr(cfg, "vibe_min_confidence", 0.55),
            "vibe_max_uncertainty": getattr(cfg, "vibe_max_uncertainty", 0.35),
            "reply_timeout": cfg.reply_timeout,
            "tool_agent_timeout": cfg.tool_agent_timeout,
            "topic_commit_threshold": cfg.topic_commit_threshold,
            "topic_ambiguity_threshold": cfg.topic_ambiguity_threshold,
            "topic_margin_threshold": cfg.topic_margin_threshold,
            "topic_join_threshold": cfg.topic_join_threshold,
            "topic_window_seconds": cfg.topic_window_seconds,
            "replay_message_limit": cfg.replay_message_limit,
            "pipeline_mode": getattr(cfg, "pipeline_mode", PIPELINE_FILTER),
            "ambient_intervention": bool(getattr(cfg, "ambient_intervention", False)),
            "takeover_all": bool(getattr(cfg, "takeover_all", False)),
            "takeover_groups": sorted(getattr(cfg, "takeover_groups", ()) or ()),
            "exclude_groups": sorted(getattr(cfg, "exclude_groups", ()) or ()),
            "provider": getattr(cfg, "provider_id", ""),
            "reply_provider": getattr(cfg, "reply_provider_id", ""),
            "vibe_provider": getattr(cfg, "vibe_provider_id", ""),
            # The panel reads this map by *schema* key, and the schema key is
            # annotation_draft_provider: publishing it as `draft_provider` left the row
            # showing "生效：—" and excluded it from the drift check.
            "annotation_draft_provider": getattr(cfg, "draft_provider_id", ""),
            "annotation_draft_enabled": bool(getattr(cfg, "annotation_draft_enabled", False)),
            "annotation_draft_auto_enabled": bool(getattr(cfg, "annotation_draft_auto_enabled", False)),
            "annotation_draft_interval_minutes": getattr(cfg, "annotation_draft_interval_minutes", 15.0),
            "annotation_draft_limit": getattr(cfg, "annotation_draft_limit", 20),
            "annotation_draft_timeout": getattr(cfg, "annotation_draft_timeout", 60.0),
            "parent_window_seconds": getattr(cfg, "parent_window_seconds", 180.0),
            "parent_accept_threshold": getattr(cfg, "parent_accept_threshold", 0.72),
            "learning_policy_mode": getattr(cfg, "learning_policy_mode", "off"),
            "learning_policy_source_id": getattr(cfg, "learning_policy_source_id", ""),
            "learning_policy_expected_policy_id": getattr(
                cfg, "learning_policy_expected_policy_id", ""),
            "learning_policy_expected_dataset_fingerprint": getattr(
                cfg, "learning_policy_expected_dataset_fingerprint", ""),
            "learning_policy_refresh_seconds": getattr(cfg, "learning_policy_refresh_seconds", 60),
            "bot_names": list(getattr(cfg, "bot_names", ()) or ()),
            "command_prefix": getattr(cfg, "command_prefix", "/"),
            "debounce_base_cooldown": getattr(cfg, "debounce_base_cooldown", 3.5),
            "debounce_extended_cooldown": getattr(cfg, "debounce_extended_cooldown", 6.5),
            "debounce_max_cap": getattr(cfg, "debounce_max_cap", 12.0),
            "strong_addressivity_threshold": getattr(cfg, "strong_addressivity_threshold", 0.7),
            "safe_hover_threshold": getattr(cfg, "safe_hover_threshold", 0.4),
            "deep_cooling_minutes": getattr(cfg, "deep_cooling_minutes", 15.0),
            "chars_per_second": getattr(cfg, "chars_per_second", 25.0),
            "base_thinking_delay": getattr(cfg, "base_thinking_delay", 0.8),
            "max_fragments": getattr(cfg, "max_fragments", 3),
            "max_fragment_chars": getattr(cfg, "max_fragment_chars", 120),
            "inter_burst_interval": getattr(cfg, "inter_burst_interval", 1.2),
            "casual_emoji_enabled": bool(getattr(cfg, "casual_emoji_enabled", False)),
            "strip_markdown_in_banter": bool(getattr(cfg, "strip_markdown_in_banter", True)),
            "vibe_llm_enabled": bool(getattr(cfg, "vibe_llm_enabled", False)),
            "shadow_mode": bool(getattr(cfg, "shadow_mode", False)),
            "console_show_message_content": bool(getattr(cfg, "console_show_message_content", False)),
            "telemetrics_window_seconds": getattr(cfg, "telemetrics_window_seconds", 60.0),
            "fast_banter_enter_mpm": getattr(cfg, "fast_banter_enter_mpm", 12.0),
            "chill_fade_enter_mpm": getattr(cfg, "chill_fade_enter_mpm", 3.0),
            "wts_topic_weight": getattr(cfg, "wts_topic_weight", 0.12),
            "wts_professionalism_weight": getattr(cfg, "wts_professionalism_weight", 0.08),
            "wts_question_weight": getattr(cfg, "wts_question_weight", 0.08),
            "wts_participation_weight": getattr(cfg, "wts_participation_weight", 0.06),
            "wts_fatigue_weight": getattr(cfg, "wts_fatigue_weight", 1.0),
            "neural_embedding_enabled": bool(getattr(cfg, "neural_embedding_enabled", False)),
            "embedding_provider": getattr(cfg, "embedding_provider", ""),
            "neural_link_threshold": getattr(cfg, "neural_link_threshold", 0.78),
            "embedding_cache_size": getattr(cfg, "embedding_cache_size", 512),
            "embedding_cache_ttl_seconds": getattr(cfg, "embedding_cache_ttl_seconds", 1800),
            "presence_knob": getattr(cfg, "presence_knob", "sensible"),
            "social_manners_enabled": bool(getattr(cfg, "social_manners_enabled", True)),
            "relay_baton_enabled": bool(getattr(cfg, "relay_baton_enabled", True)),
            "private_field_enabled": bool(getattr(cfg, "private_field_enabled", True)),
            "hyped_quota_enabled": bool(getattr(cfg, "hyped_quota_enabled", True)),
            "media_image_gate_enabled": bool(getattr(cfg, "media_image_gate_enabled", True)),
            "media_voice_gate_enabled": bool(getattr(cfg, "media_voice_gate_enabled", True)),
            "media_understand_reply_enabled": bool(getattr(cfg, "media_understand_reply_enabled", False)),
            "media_privacy_strict": bool(getattr(cfg, "media_privacy_strict", True)),
            "deciding_detect_enabled": bool(getattr(cfg, "deciding_detect_enabled", True)),
            "gap_fill_proactive_enabled": bool(getattr(cfg, "gap_fill_proactive_enabled", True)),
            "cold_memory_nudge_enabled": bool(getattr(cfg, "cold_memory_nudge_enabled", True)),
            "newcomer_caution_enabled": bool(getattr(cfg, "newcomer_caution_enabled", True)),
            "pace_align_enabled": bool(getattr(cfg, "pace_align_enabled", True)),
            "proactive_quota_enabled": bool(getattr(cfg, "proactive_quota_enabled", True)),
            "proactive_quota_per_hour": _effective_int(cfg, "proactive_quota_per_hour", 2),
            "proactive_quota_per_topic": _effective_int(cfg, "proactive_quota_per_topic", 1),
            "rhythm_timezone": str(getattr(cfg, "rhythm_timezone", "") or ""),
            "daily_rhythm_enabled": bool(getattr(cfg, "daily_rhythm_enabled", True)),
            "rhythm_morning_hi_enabled": bool(getattr(cfg, "rhythm_morning_hi_enabled", True)),
            "rhythm_day_share_slots": _effective_int(cfg, "rhythm_day_share_slots", 1),
            "rhythm_goodnight_text_quota": int(getattr(cfg, "rhythm_goodnight_text_quota", 1) or 1),
            "rhythm_sleep_after_winddown": bool(getattr(cfg, "rhythm_sleep_after_winddown", True)),
            "rhythm_allow_self_sleep": bool(getattr(cfg, "rhythm_allow_self_sleep", True)),
            "rhythm_allow_wake": bool(getattr(cfg, "rhythm_allow_wake", True)),
            "rhythm_insomnia_enabled": bool(getattr(cfg, "rhythm_insomnia_enabled", False)),
            "rhythm_force_sleep": bool(getattr(cfg, "rhythm_force_sleep", False)),
            "rhythm_skip_morning_hi_tonight": bool(getattr(cfg, "rhythm_skip_morning_hi_tonight", False)),
            "mood_memory_enabled": bool(getattr(cfg, "mood_memory_enabled", False)),
            "slang_trial_enabled": bool(getattr(cfg, "slang_trial_enabled", False)),
            "group_memory_enabled": bool(getattr(cfg, "group_memory_enabled", True)),
            "selflearning_integration": bool(getattr(cfg, "selflearning_integration", True)),
            "selflearning_hub_url": cfg.selflearning_hub_url,
            "selflearning_hub_key_env": cfg.selflearning_hub_key_env,
        }


    def get_config_panel(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self.host._sync_runtime_from_config()
        stored = self._config_stored_values()
        effective = self.get_effective_config()
        raw = self.host._coerce_config(getattr(self.host, "config", {}) or {})
        defaults = self.get_effective_config(runtime_config=parse_runtime_config({})[0])
        missing = object()
        mismatches = []
        for key, eff in effective.items():
            if key not in stored:
                continue
            value = raw.get(key, missing)
            if value is missing:
                value = defaults.get(key)
            elif key in {"takeover_groups", "exclude_groups"} and isinstance(value, (str, list, tuple, set, frozenset)):
                source = value.split(",") if isinstance(value, str) else value
                value = sorted({str(item).strip() for item in source if str(item).strip()})
            if value != eff:
                mismatches.append(key)
        # A parameter the learning policy has overridden legitimately differs
        # from the stored value, so it is listed separately instead of being
        # reported as an unexplained mismatch: "effective != stored" is a
        # finding, and the reason has to travel with it.
        policy = self.host.get_learning_policy_status()
        overridden = sorted((policy.get("overrides") or {}).keys()) if policy.get("applied") \
            else []
        return {
            "schema": self._config_schema(),
            "stored": stored,
            "effective": effective,
            "mismatches": [key for key in mismatches if key not in overridden],
            "learning_policy": policy,
            "learning_policy_overridden": overridden,
            "warnings": list(getattr(self.host, "_config_warnings_seen", set()) or []),
        }


    def _normalize_config_update_value(self, key: str, value: Any, field_schema: dict[str, Any]) -> Any:
        field_type = str((field_schema or {}).get("type") or "string")
        if field_type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
                return bool(value)
            if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}:
                return value.strip().lower() in {"true", "1", "yes"}
            raise ValueError(f"{key} must be a boolean")
        if field_type == "int":
            if isinstance(value, bool) or value is None:
                raise ValueError(f"{key} must be an integer")
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{key} must be an integer")
            try:
                number = float(value) if not isinstance(value, int) else float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{key} must be an integer") from None
            if not math.isfinite(number) or abs(number - round(number)) > 1e-9:
                raise ValueError(f"{key} must be an integer")
            return int(round(number))
        if field_type == "float":
            if isinstance(value, bool) or value is None:
                raise ValueError(f"{key} must be a number")
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{key} must be a number")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"{key} must be a number") from None
            if not math.isfinite(number):
                raise ValueError(f"{key} must be a finite number")
            return number
        if field_type == "list":
            if value is None:
                return []
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                return [part.strip() for part in re.split(r"[\n,]", value) if part.strip()]
            raise ValueError(f"{key} must be a list")
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)


    def _prepare_config_candidate(
        self, candidate: tuple[RuntimeConfig, tuple[str, ...]], updates: dict[str, Any],
    ) -> tuple[RuntimeConfig, tuple[str, ...]]:
        prepare = getattr(self.host, "_prepare_config_candidate", None)
        if callable(prepare):
            return prepare(candidate[0], explicit_persona=updates.get("decision_mode") == "persona_model"), candidate[1]
        self.host._validate_runtime_config(candidate[0])
        return candidate

    async def save_config_values(self, updates: dict[str, Any], *, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise ValueError("config must be an object")
        if baseline is not None and not isinstance(baseline, dict):
            raise ValueError("baseline must be an object")
        schema = self._config_schema()
        unknown = sorted(set(updates) - set(schema))
        if unknown:
            raise ValueError("unknown fields: " + ", ".join(unknown))
        normalized: dict[str, Any] = {}
        for key, value in updates.items():
            field_schema = schema.get(key) if isinstance(schema.get(key), dict) else {}
            normalized[key] = self._normalize_config_update_value(key, value, field_schema)
        async with self.host._config_lock:
            if baseline is not None:
                conflicts = [key for key in normalized if key not in baseline or
                             self.host.config.get(key) != baseline[key]]
                if conflicts:
                    raise ValueError("配置冲突：这些字段已被其他页面修改，请重新读取后再保存：" + ", ".join(conflicts))
            missing = object()
            original = {key: self.host.config.get(key, missing) for key in normalized}
            self.host._config_save_in_progress = True
            try:
                for key, value in normalized.items():
                    try:
                        self.host.config[key] = value
                    except Exception as exc:
                        raise RuntimeError(f"failed to set {key}: {type(exc).__name__}") from exc
                candidate = parse_runtime_config(self.host.config)
                candidate = self._prepare_config_candidate(candidate, normalized)
                saver = getattr(self.host.config, "save_config", None)
                if not callable(saver):
                    saver = getattr(self.host, "save_config", None)
                if callable(saver):
                    result = saver()
                    if inspect.isawaitable(result):
                        result = await result
                    if result is False:
                        raise RuntimeError("save_config returned false")
            except BaseException:
                # Cancellation during an async save must restore live config too.
                for key, previous in original.items():
                    if self.host.config.get(key, missing) == previous:
                        continue
                    if previous is missing:
                        del self.host.config[key]
                    else:
                        self.host.config[key] = previous
                raise
            finally:
                self.host._config_save_in_progress = False
            self.host._sync_runtime_from_config(validated_config=candidate)
            self.host._metric("config_saved")
            return self.get_config_panel(refresh=False)


    def _serialize_provider(self, provider: Any) -> dict[str, Any]:
        meta = None
        try:
            meta_fn = getattr(provider, "meta", None)
            if callable(meta_fn):
                meta = meta_fn()
        except Exception:
            meta = None
        cfg = getattr(provider, "provider_config", None) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        pid = ""
        model = ""
        ptype = ""
        provider_type = ""
        if meta is not None:
            pid = str(getattr(meta, "id", "") or "")
            model = str(getattr(meta, "model", "") or "")
            ptype = str(getattr(meta, "type", "") or "")
            provider_type = str(getattr(meta, "provider_type", "") or "")
        if not pid:
            pid = str(cfg.get("id") or "")
        if not model:
            model = str(getattr(provider, "model_name", "") or cfg.get("model") or "")
        if not ptype:
            ptype = str(cfg.get("type") or "")
        if not provider_type:
            provider_type = str(cfg.get("provider_type") or "")
        label_bits = [pid]
        if model and model != pid:
            label_bits.append(model)
        if ptype and ptype not in label_bits:
            label_bits.append(ptype)
        return {
            "id": pid,
            "model": model,
            "type": ptype,
            "provider_type": provider_type,
            "label": " · ".join(bit for bit in label_bits if bit) or pid or "(unnamed)",
        }


    def list_available_providers(self) -> dict[str, Any]:
        """List AstrBot chat/embedding providers for config dropdowns."""
        ctx = getattr(self.host, "context", None)
        chat: list[dict[str, Any]] = []
        embedding: list[dict[str, Any]] = []
        if ctx is not None:
            getter = getattr(ctx, "get_all_providers", None)
            if callable(getter):
                try:
                    for provider in list(getter() or []):
                        item = self._serialize_provider(provider)
                        if item.get("id"):
                            chat.append(item)
                except Exception as exc:
                    logger.warning(
                        "[ChatDynamics] list chat providers failed type=%s",
                        type(exc).__name__,
                    )
            emb_getter = getattr(ctx, "get_all_embedding_providers", None)
            if callable(emb_getter):
                try:
                    for provider in list(emb_getter() or []):
                        item = self._serialize_provider(provider)
                        if item.get("id"):
                            embedding.append(item)
                except Exception as exc:
                    logger.warning(
                        "[ChatDynamics] list embedding providers failed type=%s",
                        type(exc).__name__,
                    )
        # Stable order for UI.
        chat.sort(key=lambda row: str(row.get("id") or "").lower())
        embedding.sort(key=lambda row: str(row.get("id") or "").lower())
        return {"chat": chat, "embedding": embedding}


    async def apply_stored_config(self) -> dict[str, Any]:
        async with self.host._config_lock:
            self.host._sync_runtime_from_config()
            self.host._metric("config_applied")
            return self.get_config_panel()


    async def _apply_preset_locked(self, name: str) -> dict[str, Any]:
        if name not in _PRESETS:
            raise KeyError(name)
        values = _PRESETS[name]
        changed: dict[str, Any] = {}
        missing = object()
        original: dict[str, Any] = {}
        saver = getattr(self.host.config, "save_config", None)
        if not callable(saver):
            saver = getattr(self.host, "save_config", None)
        saved = False
        self.host._config_save_in_progress = True
        try:
            for key, value in values.items():
                try:
                    current = self.host.config.get(key, missing)
                except Exception:
                    current = missing
                original[key] = current
                if current != value:
                    self.host.config[key] = value
                    changed[key] = value
            candidate = parse_runtime_config(self.host.config)
            candidate = self._prepare_config_candidate(candidate, values)
            if callable(saver):
                result = saver()
                if inspect.isawaitable(result):
                    result = await result
                if result is False:
                    raise RuntimeError("save_config returned false")
                saved = True
        except BaseException:
            for key, previous in original.items():
                try:
                    if previous is missing:
                        pop = getattr(self.host.config, "pop", None)
                        if callable(pop):
                            pop(key, None)
                        else:
                            del self.host.config[key]
                    else:
                        self.host.config[key] = previous
                except Exception:
                    pass
            self.host._config_save_in_progress = False
            self.host._sync_runtime_from_config()
            self.host._metric("preset_apply_failed")
            raise
        finally:
            self.host._config_save_in_progress = False
        self.host._sync_runtime_from_config(validated_config=candidate)
        self.host._metric("preset_applied")
        return {"name": name, "changed": changed, "saved": saved}
