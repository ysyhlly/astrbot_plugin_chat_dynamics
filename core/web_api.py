"""Plugin-page Web API handlers for Chat Dynamics."""

from __future__ import annotations

from astrbot.api import logger
import asyncio
import hashlib
import json
import math
import inspect
import time
from typing import Any, Dict, Optional

from .dashboard import (
    scene_replay_snapshot,
    snapshot_overview,
    snapshot_session_or_none,
    snapshot_sessions,
)
from .web_compat import error_response, json_response, query_value, request, request_json
from .topic_annotations import TopicAnnotations
from .page_layout import PAGE_ENTRY, SECTIONS, section_content_path


PLUGIN_NAME = "astrbot_plugin_chat_dynamics"
_MAX_BODY_BYTES = 64 * 1024
_MAX_SESSION_ID_LENGTH = 256


def _query_param(name: str) -> str:
    try:
        return query_value(name)
    except Exception:
        return ""


def _declared_body_bytes() -> Optional[int]:
    """Declared Content-Length, when the host request object exposes it.

    AstrBot's ``PluginRequest`` has no ``content_length`` attribute, so the
    header is the only place the size is visible before the body is parsed.
    """
    raw = getattr(request, "content_length", None)
    if raw is None:
        try:
            raw = request.headers.get("content-length")
        except Exception:
            raw = None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _json_body() -> Dict[str, Any]:
    try:
        declared = _declared_body_bytes()
        if declared is not None and declared > _MAX_BODY_BYTES:
            return {"__invalid_body__": "body too large"}
        # Supported hosts expose raw body()/get_data(). Bound bytes before JSON
        # parsing even for chunked requests or misleading Content-Length. The
        # host still owns the streaming/transport memory limit.
        loader = getattr(request, "body", None)
        if not callable(loader):
            loader = getattr(request, "get_data", None)
        if callable(loader):
            raw = loader()
            if inspect.isawaitable(raw):
                raw = await raw
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, (bytes, bytearray)):
                return {"__invalid_body__": "invalid JSON body"}
            if len(raw) > _MAX_BODY_BYTES:
                return {"__invalid_body__": "body too large"}
            payload = json.loads(raw)
        else:
            # Legacy host shims lacking raw access still get a bounded payload
            # check; they cannot offer a pre-parse byte limit at this layer.
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
        if value is None or value == "" or (isinstance(value, str) and not value.strip()):
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


def _json_err(message: str, status_code: int = 400, headers: Any = None, data: Any = None):
    try:
        return error_response(message, status_code=status_code, data=data, headers=headers)
    except TypeError:
        response = error_response(message, status_code=status_code, data=data)
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
    """Serve endpoints behind the host's Dashboard/plugin-scope authorization.

    Preference/navigation username checks require a per-user storage or signing
    identity; a nonempty username alone does not assert administrator privileges.
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.registered = False
        self._registered_endpoints: set[str] = set()
        self._rate_buckets: Dict[tuple[str, str], list[float]] = {}
        # Share the plugin-owned store when it exists: the manual annotation path in
        # main.py and these endpoints must read and write through one lock.
        self.topic_annotations = getattr(plugin, "topic_annotations", None) or TopicAnnotations(plugin)

    @staticmethod
    def _endpoint_key(route: str, methods: list[str]) -> str:
        # AstrBot allows the same path with different methods; dedupe must include methods.
        normalized = "|".join(sorted(str(method).upper() for method in (methods or [])))
        return f"{route}@{normalized}"

    def register(self) -> None:
        from ..series_workspace import register_panel_api

        if self.registered:
            return
        context = getattr(self.plugin, "context", None)
        if context is None or not hasattr(context, "register_web_api"):
            return
        # AstrBot keeps removed endpoints across hot reloads. Retire only our
        # former approval routes so an old bound handler cannot remain callable.
        registered = getattr(context, "registered_web_apis", None)
        if isinstance(registered, list):
            retired = {f"/{PLUGIN_NAME}/annotation_draft", f"/{PLUGIN_NAME}/annotation_drafts"}
            registered[:] = [api for api in registered if api[0] not in retired]
        routes = [
            ("overview", self.overview, ["GET"], "群聊动态总览"),
            ("sessions", self.sessions, ["GET"], "会话列表与遥测"),
            ("session", self.session, ["GET"], "单个会话详情与 DAG 片段"),
            ("cool", self.cool, ["POST"], "为指定群开启深度冷却"),
            ("reset", self.reset, ["POST"], "重置指定群的图谱与状态"),
            ("presets", self.presets, ["GET"], "查看推荐配置预设"),
            ("preset/apply", self.apply_preset, ["POST"], "应用推荐配置预设"),
            ("config", self.config_get, ["GET"], "查看插件配置与生效值"),
            ("integrations", self.integrations, ["GET"], "查看插件联动与设置归属"),
            ("config", self.config_save, ["POST"], "保存插件配置并立即应用到运行时"),
            ("config/apply", self.config_apply, ["POST"], "从已存配置强制同步到运行时"),
            ("providers", self.providers, ["GET"], "列出可用 Chat/Embedding Provider"),
            ("page_nav", self.page_nav, ["GET"], "签发兄弟插件页 content_path（互跳）"),
            ("ui_preferences", self.ui_preferences_get, ["GET"], "读取当前后台账号的界面偏好"),
            ("ui_preferences", self.ui_preferences_save, ["POST"], "保存当前后台账号的界面偏好"),
            ("notebook", self.notebook_get, ["GET"], "群记忆小本列表"),
            ("notebook", self.notebook_post, ["POST"], "群记忆小本写入/忘掉/静音"),
            ("narrative", self.narrative_get, ["GET"], "管理员查看角色生活与发送回执"),
            ("narrative", self.narrative_post, ["POST"], "管理员控制角色生活与联系意图"),
            ("read_air", self.read_air, ["GET"], "今日读空气摘要"),
            ("replay", self.replay, ["GET"], "场景回放色块时间轨"),
            ("topic_annotations", self.annotations_get, ["GET"], "话题标注与混淆统计"),
            ("topic_annotations", self.annotations_post, ["POST"], "保存话题纠错标注"),
            ("setup/groups", self.setup_groups, ["GET"], "发现可用于引导接管的群聊列表"),
            ("setup/probe", self.setup_probe, ["POST"], "手动检测所选模型的连通性"),
            ("series_matrix", self.series_matrix_get, ["GET"], "跨 8 插件群聊总控矩阵"),
            ("series_matrix_toggle", self.series_matrix_toggle_post, ["POST"], "跨插件群聊接管开关"),
            ("series_matrix_batch", self.series_matrix_batch_post, ["POST"], "跨插件群聊整行批量切换与应用模板"),
            ("series_search", self.series_search_get, ["GET"], "跨插件联邦快捷搜索"),
            ("series_telemetry", self.series_telemetry_get, ["GET"], "跨插件 LLM 与 Token 遥测"),
            ("series_health", self.series_health_get, ["GET"], "全系列 8 插件健康诊断与 Provider 体检"),
            ("series_preflight", self.series_preflight_post, ["POST"], "全系列预检与模型绑定核查"),
            ("series_templates", self.series_templates_get, ["GET"], "查看群聊内置与自定义配置模板"),
            ("series_templates", self.series_templates_post, ["POST"], "新建/更新/删除群聊自定义模板"),
            ("series_templates", self.series_templates_delete, ["DELETE"], "删除群聊自定义模板"),
        ]
        for endpoint, handler, methods, desc in routes:
            route = f"/{PLUGIN_NAME}/{endpoint}"
            key = self._endpoint_key(route, methods)
            if key in self._registered_endpoints:
                continue
            try:
                register_panel_api(self.plugin, context.register_web_api, route, handler, methods, desc)
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

    def _rate_limit(self, endpoint: str, limit: int) -> Any:
        """Per-endpoint budget for one user.

        The key has to name the endpoint: keying it by the HTTP method put nine
        unrelated GETs into a single 60/min bucket, so ordinary use of the console,
        the replay page exhausted each other's allowance.
        """
        now = time.monotonic()
        identity = self._request_identity()
        key = (endpoint, identity)
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

    async def _narrative_admin_allowed(self):
        """Require the host Dashboard JWT, not a plugin-scoped API key."""
        try:
            from astrbot.dashboard.api.auth import require_dashboard_user

            raw = getattr(request, "_request", None)
            if raw is None:
                return False
            username = await require_dashboard_user(raw)
            expected = self.plugin.context.get_config().get("dashboard", {}).get("username")
            return bool(username and (not expected or username == expected))
        except Exception:
            return False

    async def narrative_get(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("narrative_get", 60)) is not None:
            return limited
        umo = (_query_param("umo") or "").strip()
        if not umo or len(umo) > _MAX_SESSION_ID_LENGTH:
            return _json_err("umo required", 400)
        try:
            return _json_ok(await self.plugin.narrative.admin(umo))
        except Exception as exc:
            logger.warning("Narrative admin read unavailable type=%s", type(exc).__name__)
            return _json_err("narrative unavailable", 503)

    async def narrative_post(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("narrative_post", 20)) is not None:
            return limited
        body = await _json_body()
        invalid = self._validate_fields(body, {"umo", "action", "intent_id", "patch", "query", "participants", "bundle_id"})
        if invalid:
            return invalid
        umo = str(body.get("umo", "")).strip()
        if not umo or len(umo) > _MAX_SESSION_ID_LENGTH:
            return _json_err("umo required", 400)
        coordinator = self.plugin.narrative
        if body.get('action') == 'context':
            bundle_id = body.get('bundle_id')
            if not isinstance(bundle_id, str) or not 1 <= len(bundle_id) <= 128:
                return _json_err('bundle_id required', 400)
            material = await asyncio.to_thread(coordinator.journal.full_bundle, bundle_id)
            if not material or material.get('scope', {}).get('umo') != umo:
                return _json_err('context not found in this scope', 404)
            return _json_ok(material)
        if body.get("action") == "social":
            participants = body.get("participants")
            if not isinstance(participants, list) or not 1 <= len(participants) <= 8 or any(
                    not isinstance(uid, str) or not uid or len(uid) > 128 for uid in participants):
                return _json_err("participants must contain 1–8 participant IDs", 400)
            return _json_ok(await coordinator.inspect_social(umo, participants))
        if body.get("action") == "recall":
            query = body.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                return _json_err("query must contain 1–500 characters", 400)
            return _json_ok(await coordinator.inspect_memory(umo, query.strip()))
        data = await coordinator.admin(umo)
        scope = (data.get("life", {}).get("snapshot") or {}).get("scope")
        if not scope:
            character = await coordinator.call("worldbook", "get_character_context",
                                               {"platform": umo.rsplit(":", 2)[0], "umo": umo, "persona_id": "", "instance_id": ""}, None)
            scope = (character or {}).get("scope")
        if not scope:
            return _json_err("character unavailable", 409)
        action = body.get("action")
        if action in {"pause", "resume", "pause_outreach", "resume_outreach"}:
            method = "set_outreach_paused" if "outreach" in action else "set_paused"
            result = await coordinator.call("dynamic_life_state", method, action.startswith("pause"), scope)
        elif action == "cancel_intent":
            iid = str(body.get("intent_id", ""))
            owned = [*data.get("intents", []), *data.get("life", {}).get("intents", [])]
            if not iid or not any(x.get("intent_id") == iid for x in owned):
                return _json_err("intent not found in this conversation", 404)
            result = await coordinator.call("dynamic_life_state", "cancel_intent", iid)
            if result is not None:
                await asyncio.to_thread(coordinator.journal.intent_status, iid, "cancelled")
        elif action in {"correct", "correct_global"}:
            patch = body.get("patch")
            if not isinstance(patch, dict) or not patch or set(patch) - {"activity", "availability", "subjective_state", "summary"}:
                return _json_err("invalid state correction", 400)
            if any(not isinstance(value, str) or len(value) > 600 for value in patch.values()):
                return _json_err("correction values must be bounded text", 400)
            if "availability" in patch and patch["availability"] not in {"available", "busy", "asleep", "unknown"}:
                return _json_err("invalid availability", 400)
            if action == "correct_global":
                # A global edit must resolve the current authoritative binding,
                # never a cached bundle from before a card change or unload.
                character = await coordinator.call("worldbook", "get_character_context",
                    {"platform": umo.rsplit(":", 2)[0], "umo": umo, "persona_id": "", "instance_id": ""}, None)
                scope = (character or {}).get("scope") or {}
                if not character or character.get("disabled") or not character.get("available", True) or scope.get("umo") != umo or not scope.get("persona_id"):
                    return _json_err("current character unavailable", 409)
                scope = dict(scope, platform="", umo="")
            result = await coordinator.call("dynamic_life_state", "correct_state", scope, patch)
        else:
            return _json_err("unknown narrative action", 400)
        if result is None:
            return _json_err("narrative operation unavailable", 503)
        return _json_ok(await coordinator.admin(umo))

    async def overview(self):
        if (limited := self._rate_limit("overview", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            return _json_ok(snapshot_overview(self.plugin))
        except Exception as exc:
            logger.error("[ChatDynamics] Overview snapshot failed code=CD_WEB_OVERVIEW type=%s", type(exc).__name__)
            return _json_err("runtime state unavailable", 503)

    async def sessions(self):
        if (limited := self._rate_limit("sessions", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            return _json_ok({"sessions": snapshot_sessions(self.plugin)})
        except Exception as exc:
            logger.error("[ChatDynamics] Session snapshot failed code=CD_WEB_SESSION type=%s", type(exc).__name__)
            return _json_err("runtime state unavailable", 503)

    async def session(self):
        if (limited := self._rate_limit("session", 60)) is not None:
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
        if (limited := self._rate_limit("cool", 20)) is not None:
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
        if (limited := self._rate_limit("reset", 20)) is not None:
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


    async def integrations(self):
        if (limited := self._rate_limit("integrations", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            from .integrations.catalog import companion_snapshot
            return _json_ok(companion_snapshot(self.plugin.context, self.plugin.get_effective_config()))
        except Exception as exc:
            logger.warning("[ChatDynamics] Integration snapshot unavailable type=%s", type(exc).__name__)
            return _json_err("integration snapshot unavailable", 503)

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
        invalid = self._validate_fields(body, {"config", "values", "baseline"})
        if invalid is not None:
            return invalid
        updates = body.get("config")
        if updates is None:
            updates = body.get("values")
        if not isinstance(updates, dict):
            return _json_err("config must be an object", 400)
        try:
            if "baseline" in body:
                if not isinstance(body["baseline"], dict):
                    return _json_err("baseline must be an object", 400)
                panel = await self.plugin.save_config_values(updates, baseline=body["baseline"])
            else:
                panel = await self.plugin.save_config_values(updates)
            return _json_ok(panel)
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] Config save failed code=CD_CONFIG_SAVE type=%s", type(exc).__name__)
            return _json_err("config could not be saved", 503)


    @staticmethod
    def _ui_preference_digest() -> str | None:
        try:
            username = getattr(request, "username", None)
        except Exception:
            return None
        if not isinstance(username, str) or not username.strip():
            return None
        return hashlib.sha256(username.encode("utf-8")).hexdigest()

    @classmethod
    def _ui_preference_key(cls) -> str | None:
        digest = cls._ui_preference_digest()
        return f"ui_theme:{digest}" if digest is not None else None

    @classmethod
    def _setup_dismissed_key(cls) -> str | None:
        digest = cls._ui_preference_digest()
        return f"setup_dismissed:{digest}" if digest is not None else None

    async def ui_preferences_get(self):
        digest = self._ui_preference_digest()
        if digest is None:
            return _json_err("unauthorized", 401)
        limited = self._rate_limit("ui_preferences_get", 120)
        if limited is not None:
            return limited
        try:
            value = await self.plugin.get_kv_data(f"ui_theme:{digest}", None)
            dismissed = await self.plugin.get_kv_data(f"setup_dismissed:{digest}", False)
            return _json_ok(
                {
                    "ui": value if value in ("day", "night") else None,
                    "setup_dismissed": bool(dismissed),
                }
            )
        except Exception as exc:
            logger.warning("[ChatDynamics] UI preference read failed type=%s", type(exc).__name__)
            return _json_err("UI preference unavailable", 503)

    async def ui_preferences_save(self):
        digest = self._ui_preference_digest()
        if digest is None:
            return _json_err("unauthorized", 401)
        limited = self._rate_limit("ui_preferences_save", 60)
        if limited is not None:
            return limited
        body = await _json_body()
        if "__invalid_body__" in body:
            return _json_err(str(body["__invalid_body__"]), 400)
        allowed_keys = {"ui", "setup_dismissed"}
        if not body or not set(body).issubset(allowed_keys):
            return _json_err("invalid preference fields", 400)
        if "ui" in body and body.get("ui") not in ("day", "night"):
            return _json_err("ui must be day or night", 400)
        if "setup_dismissed" in body and not isinstance(body.get("setup_dismissed"), bool):
            return _json_err("setup_dismissed must be a boolean", 400)
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            saved_payload: Dict[str, Any] = {"saved": True}
            if "ui" in body:
                await self.plugin.put_kv_data(f"ui_theme:{digest}", body["ui"])
                saved_payload["ui"] = body["ui"]
            if "setup_dismissed" in body:
                await self.plugin.put_kv_data(f"setup_dismissed:{digest}", body["setup_dismissed"])
                saved_payload["setup_dismissed"] = body["setup_dismissed"]
            return _json_ok(saved_payload)
        except Exception as exc:
            logger.warning("[ChatDynamics] UI preference save failed type=%s", type(exc).__name__)
            return _json_err("UI preference save failed", 503)

    async def setup_groups(self):
        if (limited := self._rate_limit("setup_groups", 20)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            discover = getattr(self.plugin, "discover_setup_groups", None)
            if callable(discover):
                data = await discover()
            else:
                from .setup_wizard import discover_setup_groups

                data = await discover_setup_groups(self.plugin)
            return _json_ok(data)
        except Exception as exc:
            logger.error("[ChatDynamics] Setup group discovery failed code=CD_SETUP_GROUPS type=%s", type(exc).__name__)
            return _json_err("setup groups unavailable", 503)

    async def setup_probe(self):
        if (limited := self._rate_limit("setup_probe", 6)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        allowed = {
            "participation_backend",
            "decision_provider",
            "decision_llm_provider",
            "reply_provider",
            "discussion_judgment_mode",
            "discussion_reranker_provider",
            "discussion_llm_provider",
            "probe_discussion",
        }
        if (invalid := self._validate_fields(body, allowed)) is not None:
            return invalid
        from .setup_wizard import probe_setup_providers, validate_setup_probe_payload

        try:
            validated = validate_setup_probe_payload(body)
        except ValueError as exc:
            return _json_err(str(exc), 400)
        try:
            probe = getattr(self.plugin, "probe_setup_providers", None)
            if callable(probe):
                data = await probe(validated)
            else:
                data = await probe_setup_providers(self.plugin, validated)
            return _json_ok(data)
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] Setup provider probe failed code=CD_SETUP_PROBE type=%s", type(exc).__name__)
            return _json_err("setup probe unavailable", 503)

    async def page_nav(self):
        """Return a fresh app-scoped content_path for an internal section.

        Sandboxed plugin iframes cannot change parent hash and cannot reuse
        arbitrary page-scoped asset_token. Parent-authenticated bridge.apiGet hits
        this endpoint; the host signs the single app scope before selecting a section.
        """
        limited = self._rate_limit("page_nav", 60)
        if limited is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)

        page_name = (query_value("page") or query_value("page_name") or "").strip()
        allowed = set(SECTIONS)
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

        plugin_name = PLUGIN_NAME
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
            get_entry = page_service.get_plugin_page_entry_config
            parameters = inspect.signature(get_entry).parameters
            name_key = "view_name" if "view_name" in parameters else "page_name"
            entry = await get_entry(
                plugin_name=plugin_name,
                **{name_key: PAGE_ENTRY},
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
        try:
            content_path = section_content_path(content_path, page_name)
        except ValueError:
            return _json_err("invalid app content_path", 503)

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
        if (limited := self._rate_limit("presets", 60)) is not None:
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
        if (limited := self._rate_limit("apply_preset", 20)) is not None:
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
        if (limited := self._rate_limit("read_air", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        try:
            data = snapshot_overview(self.plugin)
            from .turn_latency import summarize_turn_latencies
            provider_budget = getattr(getattr(self.plugin, 'llm', None), 'provider_budget', None)
            data['latency'] = summarize_turn_latencies(getattr(self.plugin, 'dags', {}))
            data['provider_budget'] = provider_budget.diagnostics() if provider_budget is not None else {}
            air = dict(data.get("read_air") or {})
            umo = (_query_param("umo") or _query_param("session_key") or "").strip()
            if len(umo) > _MAX_SESSION_ID_LENGTH:
                # Same bound as the sibling endpoints: an arbitrary id reaches the
                # gates, and the rhythm gate keeps per-session state keyed by it.
                return _json_err("umo too long", 400)
            if umo:
                from .dashboard import _read_air_summary

                air = _read_air_summary(self.plugin, data.get("sessions") or [], session_key=umo)
                air["presence_knob"] = air.get("presence_knob") or data.get("presence_knob")
            air['latency'] = data['latency']
            air['provider_budget'] = data['provider_budget']
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
        if (limited := self._rate_limit("replay", 60)) is not None:
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

    async def annotations_get(self):
        if (limited := self._rate_limit("annotations_get", 60)) is not None:
            return limited
        session = (_query_param("session_key") or "").strip()
        if not session or len(session) > _MAX_SESSION_ID_LENGTH:
            return _json_err("session_key required", 400)
        try:
            data = await self.topic_annotations.read(session)
            data['records'] = [self._annotation_record(row) for row in data['records']]
            return _json_ok(data)
        except Exception as exc:
            logger.error("[ChatDynamics] annotations read failed code=CD_ANNOTATIONS type=%s", type(exc).__name__)
            return _json_err("annotations unavailable", 503)


    async def annotations_post(self):
        if (limited := self._rate_limit("annotations_post", 20)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if "__invalid_body__" in body:
            return _json_err(str(body["__invalid_body__"]), 400)
        try:
            result = await self.topic_annotations.save(body)
            result['record'] = self._annotation_record(result['record'])
            return _json_ok(result)
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] annotation write failed code=CD_ANNOTATIONS type=%s", type(exc).__name__)
            return _json_err("annotation write failed", 503)

    def _annotation_record(self, row):
        if getattr(self.plugin, 'console_show_message_content', False):
            return row
        from .routing_trace import redact_trace_identifiers
        clean = {key: value for key, value in row.items() if key != 'text'}
        if isinstance(clean.get('decision_trace'), dict):
            clean['decision_trace'] = redact_trace_identifiers(clean['decision_trace'])
        clean['hidden_fields'] = []
        for key in ('recipient_ids', 'subject_ids'):
            if key in clean:
                clean[key] = []
                clean['hidden_fields'].append(key)
        return clean

    async def notebook_get(self):
        if (limited := self._rate_limit("notebook_get", 60)) is not None:
            return limited
        if self.plugin._shutting_down:
            return _json_err("plugin is shutting down", 503)
        umo = (_query_param("umo") or _query_param("session_key") or "").strip()
        if not umo:
            return _json_err("umo required", 400)
        if len(umo) > _MAX_SESSION_ID_LENGTH:
            return _json_err("umo too long", 400)
        try:
            # Kept on the event loop on purpose: the notebook cache is shared
            # with the message path, so moving only the panel onto a worker
            # thread would race that cache (see notebook_mutate_async).
            return _json_ok(self.plugin.notebook_list(umo))
        except Exception as exc:
            logger.error("[ChatDynamics] notebook get failed code=CD_NOTEBOOK type=%s", type(exc).__name__)
            return _json_err("notebook unavailable", 503)

    async def notebook_post(self):
        if (limited := self._rate_limit("notebook_post", 20)) is not None:
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

    async def series_matrix_get(self):
        if (limited := self._rate_limit("series_matrix", 60)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        scope = (_query_param("scope") or _query_param("umo") or "").strip()
        persona = (_query_param("persona") or "").strip()
        refresh = (_query_param("refresh") or "").strip().lower() in {"1", "true", "yes"}
        if len(scope) > _MAX_SESSION_ID_LENGTH:
            return _json_err("scope too long", 400)
        if len(persona) > 128:
            return _json_err("persona too long", 400)
        try:
            from .series_hub import build_series_matrix

            data = await build_series_matrix(
                self.plugin, scope=scope, persona=persona, force_refresh=refresh
            )
            return _json_ok(data)
        except Exception as exc:
            logger.error("[ChatDynamics] series_matrix failed code=CD_SERIES_MATRIX type=%s", type(exc).__name__)
            return _json_err("series matrix unavailable", 503)

    async def series_matrix_toggle_post(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_matrix_toggle", 30)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if (invalid := self._validate_fields(body, {"umo", "plugin_id", "enabled", "expected_version"})) is not None:
            return invalid
        try:
            from .series_hub import toggle_series_matrix

            status_code, payload = await toggle_series_matrix(
                self.plugin,
                umo=str(body.get("umo") or ""),
                plugin_id=str(body.get("plugin_id") or ""),
                enabled=body.get("enabled"),
                expected_version=body.get("expected_version"),
            )
            if status_code == 200:
                return _json_ok(payload)
            return _json_err(
                str(payload.get("message") or "toggle failed"),
                status_code,
                data=payload if payload.get("conflict") else None,
            )
        except Exception as exc:
            logger.error("[ChatDynamics] series_matrix_toggle failed code=CD_SERIES_TOGGLE type=%s", type(exc).__name__)
            return _json_err("series matrix toggle failed", 503)

    async def series_matrix_batch_post(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_matrix_batch", 20)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if (invalid := self._validate_fields(body, {"umo", "expected_version", "states", "template_id"})) is not None:
            return invalid
        try:
            from .series_hub import batch_toggle_series_matrix

            status_code, payload = await batch_toggle_series_matrix(
                self.plugin,
                umo=str(body.get("umo") or ""),
                expected_version=body.get("expected_version"),
                states=body.get("states"),
                template_id=body.get("template_id"),
            )
            if status_code == 200:
                return _json_ok(payload)
            return _json_err(
                str(payload.get("message") or "batch toggle failed"),
                status_code,
                data=payload if payload.get("conflict") else None,
            )
        except Exception as exc:
            logger.error("[ChatDynamics] series_matrix_batch failed code=CD_SERIES_BATCH type=%s", type(exc).__name__)
            return _json_err("series matrix batch failed", 503)

    async def series_telemetry_get(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_telemetry", 60)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        window = (_query_param("window") or "24h").strip().lower()
        try:
            from .series_telemetry import SeriesTelemetryStore, VALID_WINDOWS

            if window not in VALID_WINDOWS:
                return _json_err("window must be one of 1h, 24h, 7d, 30d", 400)
            store = getattr(self.plugin, "series_telemetry", None)
            if store is None:
                store = SeriesTelemetryStore(db_path=None)
            budget = getattr(getattr(self.plugin, "llm", None), "provider_budget", None)
            live_queue = (
                budget.live_snapshot()
                if budget is not None and callable(getattr(budget, "live_snapshot", None))
                else None
            )
            return _json_ok(await asyncio.to_thread(store.snapshot, window=window, live_queue=live_queue))
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] series_telemetry failed code=CD_SERIES_TELEMETRY type=%s", type(exc).__name__)
            return _json_err("series telemetry unavailable", 503)

    async def series_health_get(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_health", 60)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            from .series_hub import build_series_health

            return _json_ok(await asyncio.to_thread(build_series_health, self.plugin))
        except Exception as exc:
            logger.error("[ChatDynamics] series_health failed code=CD_SERIES_HEALTH type=%s", type(exc).__name__)
            return _json_err("series health unavailable", 503)

    async def series_preflight_post(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_preflight", 20)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            from .series_hub import build_series_health

            health = await asyncio.to_thread(build_series_health, self.plugin)
            return _json_ok(health)
        except Exception as exc:
            logger.error("[ChatDynamics] series_preflight failed code=CD_SERIES_PREFLIGHT type=%s", type(exc).__name__)
            return _json_err("series preflight unavailable", 503)

    def _template_store(self):
        from .series_templates import SeriesTemplateStore

        store = getattr(self.plugin, "series_templates", None)
        if store is None:
            data_root = getattr(self.plugin, "_data_root", None)
            path = (data_root / "series-templates.json") if data_root is not None else None
            store = SeriesTemplateStore(path)
            self.plugin.series_templates = store
        return store

    async def series_templates_get(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_templates", 60)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        try:
            return _json_ok(self._template_store().catalog_payload())
        except Exception as exc:
            logger.error("[ChatDynamics] series_templates get failed code=CD_SERIES_TEMPLATES type=%s", type(exc).__name__)
            return _json_err("series templates unavailable", 503)

    async def series_templates_post(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_templates_write", 30)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        body = await _json_body()
        if (
            invalid := self._validate_fields(
                body, {"action", "id", "name", "description", "strategy_hint", "states"}
            )
        ) is not None:
            return invalid
        action = str(body.get("action") or "upsert").strip().lower()
        store = self._template_store()
        try:
            from .series_hub import invalidate_series_matrix_cache

            if action == "delete":
                tid = str(body.get("id") or "").strip().lower()
                if not tid:
                    return _json_err("id required", 400)
                deleted = store.delete(tid)
                if not deleted:
                    return _json_err("未找到指定自定义模板", 404)
                invalidate_series_matrix_cache(self.plugin)
                return _json_ok({"deleted": True, "id": tid, "templates": store.list_all()})
            if action != "upsert":
                return _json_err(f"unsupported action: {action}", 400)
            saved = store.upsert(body)
            invalidate_series_matrix_cache(self.plugin)
            return _json_ok({"template": saved, "templates": store.list_all()})
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] series_templates post failed code=CD_SERIES_TEMPLATES type=%s", type(exc).__name__)
            return _json_err("series template write failed", 503)

    async def series_templates_delete(self):
        if not await self._narrative_admin_allowed():
            return _json_err("Dashboard administrator required", 403)
        if (limited := self._rate_limit("series_templates_write", 30)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        tid = (_query_param("id") or "").strip()
        if not tid:
            body = await _json_body()
            if "__invalid_body__" not in body:
                tid = str(body.get("id") or "").strip()
        if not tid:
            return _json_err("id required", 400)
        store = self._template_store()
        try:
            from .series_hub import invalidate_series_matrix_cache

            deleted = store.delete(tid)
            if not deleted:
                return _json_err("未找到指定自定义模板", 404)
            invalidate_series_matrix_cache(self.plugin)
            return _json_ok({"deleted": True, "id": tid.lower(), "templates": store.list_all()})
        except ValueError as exc:
            return _json_err(str(exc), 400)
        except Exception as exc:
            logger.error("[ChatDynamics] series_templates delete failed code=CD_SERIES_TEMPLATES type=%s", type(exc).__name__)
            return _json_err("series template delete failed", 503)

    async def series_search_get(self):
        if (limited := self._rate_limit("series_search", 60)) is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return _json_err("plugin is shutting down", 503)
        query = (_query_param("q") or _query_param("query") or "").strip()
        scope = (_query_param("scope") or _query_param("umo") or "").strip()
        raw_limit = (_query_param("limit") or "20").strip()
        if len(query) > 128:
            return _json_err("query too long", 400)
        if len(scope) > _MAX_SESSION_ID_LENGTH:
            return _json_err("scope too long", 400)
        try:
            limit = int(raw_limit) if raw_limit else 20
        except ValueError:
            return _json_err("limit must be an integer", 400)
        if not 1 <= limit <= 50:
            return _json_err("limit must be between 1 and 50", 400)
        try:
            from .series_hub import federated_series_search

            data = await federated_series_search(
                self.plugin, query=query, scope=scope, limit=limit, timeout_seconds=0.30
            )
            return _json_ok(data)
        except Exception as exc:
            logger.error("[ChatDynamics] series_search failed code=CD_SERIES_SEARCH type=%s", type(exc).__name__)
            return _json_err("series search unavailable", 503)
