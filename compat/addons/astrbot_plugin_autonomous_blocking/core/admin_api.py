"""Narrow Dashboard API for owner-scoped, permanent bot-wide shields."""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

from astrbot.api.web import json_response, request

PLUGIN_ID = "astrbot_plugin_autonomous_blocking"
API_PREFIX = f"/{PLUGIN_ID}/admin"
_UID = re.compile(r"^[0-9]{1,32}$")
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
OWNER_LOOKUP_TIMEOUT_SECONDS = 5


class BlockingAdminApi:
    """Dashboard-only mutation facade using the active plugin runtime/store."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        register(f"{API_PREFIX}/owners", self.get_owners, ["GET"], "Blocking admin owners")
        register(f"{API_PREFIX}/rules", self.get_rules, ["GET"], "Blocking admin rules")
        register(f"{API_PREFIX}/rules", self.post_rule, ["POST"], "Set bot-wide permanent shield")

    def _authorized(self) -> bool:
        # AstrBot's legacy /api/plug route has already authenticated the Dashboard
        # JWT. Reject the v1 plugin-scope route until it can provide an equivalent
        # admin-only identity guarantee.
        expected = f"/api/plug/{PLUGIN_ID}/admin/"
        path = str(getattr(request, "path", "") or "")
        username = str(getattr(request, "username", "") or "").strip()
        return bool(username and path.startswith(expected))

    def _ready(self) -> bool:
        return bool(
            self.plugin.enabled
            and self.plugin.runtime is not None
            and self.plugin._store is not None
        )

    @staticmethod
    def _error(message: str, status: int):
        return json_response({"status": "error", "message": message, "data": {}}, status_code=status)

    def _guard(self):
        if not self._authorized():
            return self._error("Dashboard administrator authentication required", 401)
        if not self._ready():
            return self._error("Blocking runtime is unavailable", 503)
        return None

    async def get_owners(self):
        error = self._guard()
        if error:
            return error
        owners = []
        manager = getattr(self.plugin.context, "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        if not callable(getter):
            return self._error("Platform manager is unavailable", 503)
        unavailable = []
        for platform in getter():
            platform_id = ""
            try:
                meta = platform.meta()
                if str(getattr(meta, "name", "")) != "aiocqhttp":
                    continue
                platform_id = str(getattr(meta, "id", "") or "").strip()
                status = getattr(getattr(platform, "status", None), "value", "")
                if status != "running":
                    unavailable.append({"platform_instance_id": platform_id, "reason": "not_running"})
                    continue
                owner = await self._read_owner(platform)
                owners.append(owner)
            except Exception:
                # A disconnected or malformed adapter is never guessed from its
                # configuration or from a stale message event.
                unavailable.append({"platform_instance_id": platform_id, "reason": "identity_unavailable"})
                continue
        return json_response({"status": "ok", "data": {"owners": owners, "unavailable": unavailable}})

    async def get_rules(self):
        error = self._guard()
        if error:
            return error
        owner, error = await self._requested_owner(request.query.get("platform_instance_id"),
                                                   request.query.get("self_id"))
        if error:
            return error
        try:
            async with self.plugin.runtime._owner_lock(owner):
                snapshot = self.plugin._store.snapshot(owner, "")
            rules = [row for row in snapshot["rules"] if row.get("scope_type") == "bot"]
            return json_response({"status": "ok", "data": {
                "owner": owner,
                "owner_revision": snapshot["owner_revision"],
                "rules": rules,
            }})
        except Exception:
            return self._error("Could not read blocking rules", 503)

    async def post_rule(self):
        error = self._guard()
        if error:
            return error
        body = await request.json(default=None)
        allowed = {"platform_instance_id", "self_id", "target_uid", "action_id", "mode", "scope_type", "permanent", "reason"}
        if not isinstance(body, dict) or set(body) - allowed:
            return self._error("Invalid request fields", 400)
        if (body.get("mode") != "shield" or body.get("scope_type") != "bot"
                or body.get("permanent") is not True):
            return self._error("Only permanent bot-wide shield rules are supported", 400)
        target = str(body.get("target_uid") or "").strip()
        action_id = str(body.get("action_id") or "").strip()
        if not _UID.fullmatch(target):
            return self._error("target_uid must be a numeric platform UID", 400)
        if not _ACTION_ID.fullmatch(action_id):
            return self._error("action_id must be a stable operation identifier", 400)
        if target in self.plugin.protected_user_ids:
            return self._error("target is protected", 403)
        raw_reason = body.get("reason", "Dashboard administrator shield")
        if not isinstance(raw_reason, str):
            return self._error("reason must be a string", 400)
        reason = "".join(ch for ch in raw_reason if ch >= " " and ch != "\x7f")
        if len(reason) > 500:
            return self._error("reason must be at most 500 characters", 400)
        owner, error = await self._requested_owner(body.get("platform_instance_id"), body.get("self_id"))
        if error:
            return error
        actor = f"dashboard:{str(request.username).strip()}"
        reason = reason or "Dashboard administrator shield"
        store = self.plugin._store
        runtime = self.plugin.runtime
        try:
            async with runtime._owner_lock(owner):
                verified_owner, owner_error = await self._requested_owner(
                    owner["platform_instance_id"], owner["self_id"]
                )
                if owner_error:
                    return owner_error
                if verified_owner != owner:
                    return self._error("Selected owner changed before the rule was written", 409)
                prior = store.get_action(owner, action_id)
                if prior is not None:
                    audit = prior.get("audit") or {}
                    same = (
                        audit.get("action") == "set"
                        and audit.get("target_uid") == target
                        and audit.get("mode") == "shield"
                        and audit.get("scope_type") == "bot"
                        and audit.get("actor_kind") == "admin"
                        and audit.get("actor_uid") == actor
                        and audit.get("reason") == reason
                    )
                    if not same:
                        return self._error("action_id was already used with different parameters", 409)
                    return json_response({"status": "ok", "data": prior["receipt"]})
                receipt = store.set_rule(
                    owner,
                    target,
                    "shield",
                    scope_type="bot",
                    umo="",
                    permanent=True,
                    reason=reason,
                    actor_kind="admin",
                    actor_uid=actor,
                    action_id=action_id,
                )
            return json_response({"status": "ok", "data": receipt})
        except Exception as exc:
            message = str(exc)
            if "self-targeting" in message:
                return self._error("Bot self-targeting is forbidden", 403)
            return self._error("Could not set blocking rule", 400)

    async def _requested_owner(self, platform_id: Any, self_id: Any):
        platform_id = str(platform_id or "").strip()
        self_id = str(self_id or "").strip()
        if not platform_id or not self_id:
            return None, self._error("Select a live owner from the owners list", 400)
        try:
            platform = self.plugin.context.get_platform_inst(platform_id)
            if platform is None or str(getattr(platform.meta(), "name", "")) != "aiocqhttp":
                return None, self._error("Selected owner is unavailable", 409)
            owner = await self._read_owner(platform)
            if owner["platform_instance_id"] != platform_id or owner["self_id"] != self_id:
                return None, self._error("Selected owner no longer matches the live client", 409)
            return {"platform_instance_id": platform_id, "self_id": self_id}, None
        except Exception:
            return None, self._error("Selected owner is disconnected or unverifiable", 503)

    @staticmethod
    async def _read_owner(platform: Any) -> dict[str, str]:
        meta = platform.meta()
        if str(getattr(getattr(platform, "status", None), "value", "")) != "running":
            raise RuntimeError("platform is not active")
        client_getter = getattr(platform, "get_client", None)
        client = client_getter() if callable(client_getter) else None
        login_getter = getattr(client, "get_login_info", None)
        if not callable(login_getter):
            raise RuntimeError("OneBot login info API is unavailable")
        result = login_getter()
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=OWNER_LOOKUP_TIMEOUT_SECONDS)
        if not isinstance(result, dict):
            raise RuntimeError("OneBot returned invalid login info")
        self_id = str(result.get("user_id") or "").strip()
        platform_id = str(getattr(meta, "id", "") or "").strip()
        if not platform_id or not self_id or not _UID.fullmatch(self_id):
            raise RuntimeError("OneBot owner identity is unavailable")
        return {
            "platform_instance_id": platform_id,
            "self_id": self_id,
            "nickname": str(result.get("nickname") or ""),
        }
