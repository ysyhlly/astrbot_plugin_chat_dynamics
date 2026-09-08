"""Plugin-page Web API handlers for Chat Dynamics."""

from __future__ import annotations

import logging
import hashlib
import json
import math
import time
from typing import Any, Dict

from .dashboard import (
    scene_replay_snapshot,
    snapshot_overview,
    snapshot_session_or_none,
    snapshot_sessions,
)
from .web_compat import error_response, json_response, query_value, request, request_json

logger = logging.getLogger("astrbot_plugin_chat_dynamics.web_api")

PLUGIN_NAME = "astrbot_plugin_chat_dynamics"
_MAX_BODY_BYTES = 64 * 1024
_MAX_SESSION_ID_LENGTH = 256


def _query_param(name: str) -> str:
    try:
        return query_value(name)
    except Exception:
        return ""


async def _json_body() -> Dict[str, Any]:
    try:
        content_length = getattr(request, "content_length", None)
        if content_length is not None and int(content_length) > _MAX_BODY_BYTES:
            return {"__invalid_body__": "body too large"}
        payload = await request_json({})
        if isinstance(payload, dict):
            try:
                if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > _MAX_BODY_BYTES:
                    return {"__invalid_body__": "body too large"}
            except (TypeError, ValueError, OverflowError):
                return {"__invalid_body__": "invalid JSON body"}
            return payload
    except Exception:
        return {"__invalid_body__": "invalid JSON body"}
    return {"__invalid_body__": "JSON body must be an object"}


def _session_identifier(body: Dict[str, Any]) -> str:
    for key in ("session_key", "session_id", "group_id", "id"):
        value = body.get(key)
        if value is None or value == "":
            continue
        # Group IDs are often numeric, while session keys are strings. Reject
        # containers and other coercible values so malformed JSON cannot turn
        # into an ambiguous identifier such as "['room']".
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return str(value).strip()
        return ""
    return ""


def _json_ok(data: Any):
    # status_code is keyword-only on official astrbot.api.web.json_response.
    return json_response(
        {"status": "ok", "ok": True, "data": data, "error": None, "message": None},
        status_code=200,
    )


def _json_err(message: str, status_code: int = 400, headers: Any = None):
    try:
        return error_response(message, status_code=status_code, data=None, headers=headers)
    except TypeError:
        response = error_response(message, status_code=status_code, data=None)
        if headers:
            if isinstance(response, dict):
                response.setdefault("headers", {}).update(headers)
            elif isinstance(response, tuple) and response:
                if isinstance(response[0], dict):
                    response[0].setdefault("headers", {}).update(headers)
                elif hasattr(response[0], "headers"):
                    response[0].headers.update(headers)
            elif hasattr(response, "headers"):
                response.headers.update(headers)
        return response


class ConsoleWebAPI:
    """Registers and serves the official plugin-extension endpoints."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.registered = False
        self._registered_endpoints: set[str] = set()
        self._rate_buckets: Dict[tuple[str, str], list[float]] = {}

    @staticmethod
    def _endpoint_key(route: str, methods: list[str]) -> str:
        # AstrBot allows the same path with different methods; dedupe must include methods.
        normalized = "|".join(sorted(str(method).upper() for method in (methods or [])))
        return f"{route}@{normalized}"

    def register(self) -> None:
        if self.registered:
            return
        context = getattr(self.plugin, "context", None)
        if context is None or not hasattr(context, "register_web_api"):
            return
        routes = [
            ("overview", self.overview, ["GET"], "群聊动态总览"),
            ("sessions", self.sessions, ["GET"], "会话列表与遥测"),
            ("session", self.session, ["GET"], "单个会话详情与 DAG 片段"),
            ("cool", self.cool, ["POST"], "为指定群开启深度冷却"),
            ("reset", self.reset, ["POST"], "重置指定群的图谱与状态"),
            ("presets", self.presets, ["GET"], "查看推荐配置预设"),
            ("preset/apply", self.apply_preset, ["POST"], "应用推荐配置预设"),
            ("config", self.config_get, ["GET"], "查看插件配置与生效值"),
            ("config", self.config_save, ["POST"], "保存插件配置并立即应用到运行时"),
            ("config/apply", self.config_apply, ["POST"], "从已存配置强制同步到运行时"),
            ("providers", self.providers, ["GET"], "列出可用 Chat/Embedding Provider"),
            ("page_nav", self.page_nav, ["GET"], "签发兄弟插件页 content_path（互跳）"),
            ("ui_preferences", self.ui_preferences_get, ["GET"], "读取当前后台账号的界面偏好"),
            ("ui_preferences", self.ui_preferences_save, ["POST"], "保存当前后台账号的界面偏好"),
            ("notebook", self.notebook_get, ["GET"], "群记忆小本列表"),
            ("notebook", self.notebook_post, ["POST"], "群记忆小本写入/忘掉/静音"),
            ("read_air", self.read_air, ["GET"], "今日读空气摘要"),
            ("replay", self.replay, ["GET"], "场景回放色块时间轨"),
        ]
        for endpoint, handler, methods, desc in routes:
            route = f"/{PLUGIN_NAME}/{endpoint}"
            key = self._endpoint_key(route, methods)
            if key in self._registered_endpoints:
                continue
            try:
                context.register_web_api(route, handler, methods, desc)
            except Exception as exc:
                logger.error(
                    "[ChatDynamics] Web API registration failed code=CD_WEB_REGISTER endpoint=%s methods=%s type=%s",
                    route,
                    ",".join(methods),
                    type(exc).__name__,
                )
                continue
            self._registered_endpoints.add(key)
        self.registered = all(
            self._endpoint_key(f"/{PLUGIN_NAME}/{endpoint}", methods) in self._registered_endpoints
            for endpoint, _handler, methods, _desc in routes
        )

    @staticmethod
    def _request_identity() -> str:
        try:
            value = getattr(request, "username", None)
        except Exception:
            value = None
        return str(value or "anonymous")[:128]

    def _rate_limit(self, method: str, limit: int) -> Any:
        now = time.monotonic()
        identity = self._request_identity()
        key = (method, identity)
        bucket = [stamp for stamp in self._rate_buckets.get(key, []) if now - stamp < 60.0]
        if len(bucket) >= limit:
            self._rate_buckets[key] = bucket
            metric = getattr(self.plugin, "_metric", None)
            if callable(metric):
                metric("rate_limited")
            return _json_err("request rate limit exceeded", 429, headers={"Retry-After": "60"})
        bucket.append(now)
        self._rate_buckets[key] = bucket
        # Usernames are host supplied and can be high cardinality. Keep the
        # in-process limiter bounded without changing the one-minute window.
        if len(self._rate_buckets) > 4096:
            cutoff = now - 60.0
            self._rate_buckets = {
                bucket_key: stamps
                for bucket_key, stamps in self._rate_buckets.items()
                if stamps and stamps[-1] >= cutoff
            }
        return None

    @staticmethod
    def _validate_fields(body: Dict[str, Any], allowed: set[str]) -> Any:
        if "__invalid_body__" in body:
            return _json_err(str(body["__invalid_body__"]), 400)
        unknown = sorted(set(body) - allowed)
        if unknown:
            return _json_err("unknown fields: " + ", ".join(unknown), 400)
        return None

    async def overview(self):
        if (limited := self._rate_limit("GET", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            return _json_ok(snapshot_overview(self.plugin))
        except Exception as exc:
            logger.error("[ChatDynamics] Overview snapshot failed code=CD_WEB_OVERVIEW type=%s", type(exc).__name__)
            return _json_err("runtime state unavailable", 503)

    async def sessions(self):
        if (limited := self._rate_limit("GET", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            return _json_ok({"sessions": snapshot_sessions(self.plugin)})
        except Exception as exc:
            logger.error("[ChatDynamics] Session snapshot failed code=CD_WEB_SESSION type=%s", type(exc).__name__)
            return _json_err("runtime state unavailable", 503)

    async def session(self):
        if (limited := self._rate_limit("GET", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        session_id = _query_param("session_key") or _query_param("session_id") or _query_param("id")
        if not session_id:
            body = await _json_body()
            if (invalid := self._validate_fields(body, {"session_key", "session_id", "group_id", "id"})) is not None:
                return invalid
            session_id = _session_identifier(body)
        if not session_id:
            return _json_err("missing session_key", 400)
        if len(session_id) > _MAX_SESSION_ID_LENGTH:
            return _json_err("session_key is too long", 400)
        try:
            data = snapshot_session_or_none(self.plugin, session_id)
        except Exception as exc:
            logger.error(
                "[ChatDynamics] Session detail snapshot failed code=CD_WEB_SESSION_DETAIL type=%s",
                type(exc).__name__,
            )
            return _json_err("runtime state unavailable", 503)
        if data is None:
            return _json_err("unknown session", 404)
        return _json_ok(data)

    async def cool(self):
        if (limited := self._rate_limit("POST", 20)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if (invalid := self._validate_fields(body, {"session_key", "session_id", "group_id", "id", "minutes"})) is not None:
            return invalid
        session_id = _session_identifier(body) or _query_param("session_key") or _query_param("session_id")
        if not session_id:
            return _json_err("missing session_key", 400)
        if len(session_id) > _MAX_SESSION_ID_LENGTH:
            return _json_err("session_key is too long", 400)
        key = self.plugin._resolve_session_key(session_id)
        if key is None:
            return _json_err("unknown session", 404)
        runtime = self.plugin._sessions.get(key)
        group_id = runtime.group_id if runtime is not None else key
        if not self.plugin.is_group_takeover_enabled(group_id):
            return _json_err("unknown session", 404)
        try:
            raw_minutes = body.get("minutes", 15.0)
            if isinstance(raw_minutes, bool):
                raise ValueError
            minutes = float(raw_minutes)
            if not math.isfinite(minutes) or not 1.0 <= minutes <= 180.0:
                raise ValueError
        except (TypeError, ValueError):
            return _json_err("minutes must be between 1 and 180", 400)
        try:
            if not await self.plugin._cool_session_async(key, minutes):
                return _json_err("unknown session", 404)
            data = snapshot_session_or_none(self.plugin, key)
        except Exception as exc:
            logger.error("[ChatDynamics] Cool operation failed code=CD_WEB_COOL type=%s", type(exc).__name__)
            return _json_err("cool operation unavailable", 503)
        return _json_ok(data)

    async def reset(self):
        if (limited := self._rate_limit("POST", 20)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if (invalid := self._validate_fields(body, {"session_key", "session_id", "group_id", "id"})) is not None:
            return invalid
        session_id = _session_identifier(body) or _query_param("session_key") or _query_param("session_id")
        if not session_id:
            return _json_err("missing session_key", 400)
        if len(session_id) > _MAX_SESSION_ID_LENGTH:
            return _json_err("session_key is too long", 400)
        key = self.plugin._resolve_session_key(session_id)
        if key is None:
            return _json_err("unknown session", 404)
        runtime = self.plugin._sessions.get(key)
        group_id = runtime.group_id if runtime is not None else key
        if not self.plugin.is_group_takeover_enabled(group_id):
            return _json_err("unknown session", 404)
        try:
            await self.plugin._reset_session_state_async(key)
            data = snapshot_session_or_none(self.plugin, key)
        except Exception as exc:
            logger.error("[ChatDynamics] Reset operation failed code=CD_WEB_RESET type=%s", type(exc).__name__)
            return _json_err("reset operation unavailable", 503)
        return _json_ok(data)


    async def config_get(self):
        limited = self._rate_limit("config_get", 60)
        if limited is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            sync = getattr(self.plugin, "_sync_runtime_from_config", None)
            if callable(sync):
                sync()
            return _json_ok(self.plugin.get_config_panel())
        except Exception as exc:
            logger.error("[ChatDynamics] Config get failed code=CD_CONFIG_GET type=%s", type(exc).__name__)
            return _json_err("config unavailable", 503)

    async def config_save(self):
        limited = self._rate_limit("config_save", 20)
        if limited is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        invalid = self._validate_fields(body, {"config", "values"})
        if invalid is not None:
            return invalid
        updates = body.get("config")
        if updates is None:
            updates = body.get("values")
        if not isinstance(updates, dict):
            return _json_err("config must be an object", 400)
        try:
            panel = await self.plugin.save_config_values(updates)
            return _json_ok(panel)
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] Config save failed code=CD_CONFIG_SAVE type=%s", type(exc).__name__)
            return _json_err("config could not be saved", 503)


    @staticmethod
    def _ui_preference_key() -> str | None:
        try:
            username = getattr(request, "username", None)
        except Exception:
            return None
        if not isinstance(username, str) or not username.strip():
            return None
        digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
        return f"ui_theme:{digest}"

    async def ui_preferences_get(self):
        key = self._ui_preference_key()
        if key is None:
            return _json_err("unauthorized", 401)
        limited = self._rate_limit("ui_preferences_get", 120)
        if limited is not None:
            return limited
        try:
            value = await self.plugin.get_kv_data(key, None)
            return _json_ok({"ui": value if value in ("day", "night") else None})
        except Exception as exc:
            logger.warning("[ChatDynamics] UI preference read failed type=%s", type(exc).__name__)
            return _json_err("UI preference unavailable", 503)

    async def ui_preferences_save(self):
        key = self._ui_preference_key()
        if key is None:
            return _json_err("unauthorized", 401)
        limited = self._rate_limit("ui_preferences_save", 60)
        if limited is not None:
            return limited
        body = await _json_body()
        if set(body) != {"ui"} or body.get("ui") not in ("day", "night"):
            return _json_err("ui must be day or night", 400)
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            await self.plugin.put_kv_data(key, body["ui"])
            return _json_ok({"ui": body["ui"], "saved": True})
        except Exception as exc:
            logger.warning("[ChatDynamics] UI preference save failed type=%s", type(exc).__name__)
            return _json_err("UI preference save failed", 503)

    async def page_nav(self):
        """Return a fresh content_path for a sibling plugin page.

        Sandboxed plugin iframes cannot change parent hash and cannot reuse
        page-scoped asset_token. Parent-authenticated bridge.apiGet hits this
        endpoint; we mint a new token via PluginPageService.
        """
        limited = self._rate_limit("page_nav", 60)
        if limited is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)

        page_name = (query_value("page") or query_value("page_name") or "").strip()
        allowed = {"console", "config", "today", "manners", "memory", "replay"}
        if page_name not in allowed:
            return _json_err("unsupported page", 400)

        try:
            getter = getattr(request, "_get_current", None)
            plugin_req = getter() if callable(getter) else None
        except Exception:
            plugin_req = None
        if plugin_req is None:
            return _json_err("plugin request unavailable", 503)

        username = getattr(plugin_req, "username", None)
        if not isinstance(username, str) or not username.strip():
            return _json_err("unauthorized", 401)

        plugin_name = getattr(plugin_req, "plugin_name", None) or PLUGIN_NAME
        raw = getattr(plugin_req, "_request", None)
        if raw is None:
            return _json_err("underlying request unavailable", 503)

        try:
            page_service = raw.app.state.services.plugin_pages
        except Exception as exc:
            logger.error(
                "[ChatDynamics] page_nav missing plugin_pages service type=%s",
                type(exc).__name__,
            )
            return _json_err("page service unavailable", 503)

        locale = "zh-CN"
        try:
            raw_locale = (plugin_req.headers.get("accept-language") or "").strip()
            if raw_locale:
                locale = raw_locale.split(",", 1)[0].split(";", 1)[0].strip() or locale
        except Exception:
            pass

        try:
            entry = await page_service.get_plugin_page_entry_config(
                plugin_name=plugin_name,
                page_name=page_name,
                username=username,
                locale=locale,
            )
        except Exception as exc:
            logger.error(
                "[ChatDynamics] page_nav entry failed page=%s type=%s",
                page_name,
                type(exc).__name__,
            )
            return _json_err("page entry unavailable", 503)

        content_path = ""
        if isinstance(entry, dict):
            content_path = str(entry.get("content_path") or "").strip()
        if not content_path:
            return _json_err("content_path missing", 503)

        theme = (query_value("theme") or "").strip()
        if theme in ("dark", "light"):
            sep = "&" if "?" in content_path else "?"
            if "theme=" not in content_path:
                content_path = f"{content_path}{sep}theme={theme}"

        return _json_ok(
            {
                "page": page_name,
                "plugin": plugin_name,
                "content_path": content_path,
            }
        )


    async def providers(self):
        limited = self._rate_limit("providers", 60)
        if limited is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            return _json_ok(self.plugin.list_available_providers())
        except Exception as exc:
            logger.error("[ChatDynamics] Providers list failed code=CD_PROVIDERS type=%s", type(exc).__name__)
            return _json_err("providers unavailable", 503)

    async def config_apply(self):
        limited = self._rate_limit("config_apply", 20)
        if limited is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            panel = await self.plugin.apply_stored_config()
            return _json_ok(panel)
        except Exception as exc:
            logger.error("[ChatDynamics] Config apply failed code=CD_CONFIG_APPLY type=%s", type(exc).__name__)
            return _json_err("config could not be applied", 503)

    async def presets(self):
        if (limited := self._rate_limit("GET", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            sync = getattr(self.plugin, "_sync_runtime_from_config", None)
            if callable(sync):
                sync()
            return _json_ok(self.plugin.preset_catalog())
        except Exception as exc:
            logger.error("[ChatDynamics] Preset catalog failed code=CD_WEB_PRESETS type=%s", type(exc).__name__)
            return _json_err("preset catalog unavailable", 503)

    async def apply_preset(self):
        if (limited := self._rate_limit("POST", 20)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if (invalid := self._validate_fields(body, {"name", "confirm"})) is not None:
            return invalid
        if body.get("confirm") is not True:
            return _json_err("confirm must be true", 400)
        raw_name = body.get("name")
        if not isinstance(raw_name, str):
            return _json_err("name must be a string", 400)
        name = raw_name.strip()
        if len(name) > 32:
            return _json_err("preset name is too long", 400)
        try:
            result = await self.plugin.apply_preset(name)
        except KeyError:
            return _json_err("unknown preset", 400)
        except Exception as exc:
            logger.error("[ChatDynamics] Preset apply failed code=CD_PRESET_APPLY type=%s", type(exc).__name__)
            return _json_err("preset could not be applied", 503)
        return _json_ok(result)


    async def read_air(self):
        if (limited := self._rate_limit("GET", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            data = snapshot_overview(self.plugin)
            air = dict(data.get("read_air") or {})
            umo = (_query_param("umo") or _query_param("session_key") or "").strip()
            if umo:
                from .dashboard import _read_air_summary

                air = _read_air_summary(self.plugin, data.get("sessions") or [], session_key=umo)
                air["presence_knob"] = air.get("presence_knob") or data.get("presence_knob")
            # Attach partner lamps for dashboard pages without a second round-trip.
            air["selflearning"] = data.get("selflearning") or {}
            air["media_gate"] = data.get("media_gate") or {}
            air["social_manners"] = data.get("social_manners") or {}
            air["useful_proactive"] = data.get("useful_proactive") or {}
            air["daily_rhythm_cfg"] = data.get("daily_rhythm") or {}
            if "daily_rhythm" not in air:
                air["daily_rhythm"] = (data.get("read_air") or {}).get("daily_rhythm") or {}
            if "rhythm_summary" not in air:
                air["rhythm_summary"] = (data.get("read_air") or {}).get("rhythm_summary") or ""
            if "proactive_used" not in air:
                air["proactive_used"] = (data.get("read_air") or {}).get("proactive_used", 0)
            if "proactive_cap" not in air:
                air["proactive_cap"] = (data.get("read_air") or {}).get("proactive_cap", 2)
            air["sessions"] = [
                {
                    "session_key": row.get("session_key") or row.get("session_id"),
                    "session_id": row.get("session_id"),
                    "group_id": row.get("group_id"),
                    "mode": row.get("mode"),
                    "mpm": row.get("mpm"),
                    "takeover": row.get("takeover"),
                    "cooling": row.get("cooling"),
                }
                for row in (data.get("sessions") or [])
            ]
            return _json_ok(air)
        except Exception as exc:
            logger.error("[ChatDynamics] read_air failed code=CD_READ_AIR type=%s", type(exc).__name__)
            return _json_err("read air unavailable", 503)

    async def replay(self):
        if (limited := self._rate_limit("GET", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        umo = (_query_param("umo") or _query_param("session_key") or "").strip()
        if len(umo) > _MAX_SESSION_ID_LENGTH:
            return _json_err("umo too long", 400)
        try:
            return _json_ok(scene_replay_snapshot(self.plugin, session_key=umo))
        except Exception as exc:
            logger.error("[ChatDynamics] replay failed code=CD_REPLAY type=%s", type(exc).__name__)
            return _json_err("replay unavailable", 503)

    async def notebook_get(self):
        if (limited := self._rate_limit("GET", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        umo = (_query_param("umo") or _query_param("session_key") or "").strip()
        if not umo:
            return _json_err("umo required", 400)
        if len(umo) > _MAX_SESSION_ID_LENGTH:
            return _json_err("umo too long", 400)
        try:
            return _json_ok(self.plugin.notebook_list(umo))
        except Exception as exc:
            logger.error("[ChatDynamics] notebook get failed code=CD_NOTEBOOK type=%s", type(exc).__name__)
            return _json_err("notebook unavailable", 503)

    async def notebook_post(self):
        if (limited := self._rate_limit("POST", 20)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if "__invalid_body__" in body:
            return _json_err(str(body["__invalid_body__"]), 400)
        action = str(body.get("action") or "").strip()
        if not action:
            return _json_err("action required", 400)
        try:
            mutate_async = getattr(self.plugin, "notebook_mutate_async", None)
            result = await mutate_async(action, body) if callable(mutate_async) else self.plugin.notebook_mutate(action, body)
            return _json_ok(result)
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] notebook post failed code=CD_NOTEBOOK type=%s", type(exc).__name__)
            return _json_err("notebook write failed", 503)
