"""Opt-in panel operations for the Dynamics workspace on stock AstrBot."""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import import_module

VERSION = 1
_PARAM = re.compile(r"<([a-zA-Z_]\w*)>")
PANEL_API_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _series():
    return import_module(
        f"{__package__}.series_panels" if __package__ else "series_panels"
    )


def register_panel_api(owner, register, route, handler, methods, description):
    """Publish an existing panel handler with its original guards and HTTP methods."""
    register(route, handler, methods, description)
    exports = getattr(owner, "_series_view_api", None)
    if exports is None:
        exports = owner._series_view_api = {}
    for method in methods:
        verb = str(method or "").strip().upper()
        if verb in PANEL_API_METHODS:
            exports[verb, route] = handler


@lru_cache(maxsize=128)
def _pattern(path):
    parts = []
    for segment in path.split("/"):
        parameter = _PARAM.fullmatch(segment)
        parts.append(f"(?P<{parameter[1]}>[^/]+)" if parameter else re.escape(segment))
    return re.compile("/".join(parts))


def _error(message, code):
    return _series()._web_json_response(
        {"status": "error", "message": message}, status_code=code
    )


class PanelWorkspace:
    version = VERSION

    def __init__(self, panel):
        self.panel = panel
        self.closed = False
        prefix = f"/{panel.identity}/series_workspace"
        self.routes = (
            (prefix + "/open/<target>", self.open, ["GET"]),
            (
                prefix + "/panel/<target>/<path:operation>",
                self.call,
                list(PANEL_API_METHODS),
            ),
        )

    def register(self):
        for path, callback, methods in self.routes:
            self.panel.owner.context.register_web_api(
                path, callback, methods, "群间面板工作区"
            )

    def close(self):
        self.closed = True
        routes = getattr(self.panel.owner.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [
                row for row in routes if getattr(row[1], "__self__", None) is not self
            ]

    async def _admin(self, *, check_csrf: bool = False):
        if self.closed:
            return None
        series = _series()
        try:
            req = series._resolve_web_request()
            username = await series.require_series_admin(
                self.panel.owner.context, req, check_csrf=check_csrf
            )
        except Exception:
            return None
        return username if self._target(self.panel.identity) is self else None

    def _target(self, target):
        series = _series()
        if not any(name == target and page for name, _, page, _ in series.CATALOG):
            return None
        rows = []
        for names, row in series._iter_registered_entries(self.panel.owner.context):
            if target in names and not any(row is other for other in rows):
                rows.append(row)
        if len(rows) != 1 or series.value(rows[0], "activated", False) is not True:
            return None
        owner = series._extract_owner(rows[0])
        panel = getattr(owner, "_series_panels", None)
        workspace = getattr(panel, "workspace", None)
        if (
            getattr(panel, "identity", None) == target
            and getattr(panel, "owner", None) is owner
            and getattr(workspace, "version", None) == VERSION
            and not workspace.closed
        ):
            return workspace
        return None

    async def open(self, target):
        username = await self._admin()
        if not username:
            return _error("需要 Dashboard 管理员登录", 403)
        destination = self._target(target)
        if destination is None:
            return _error(
                "请确认目标插件已启用，并将入口和目标插件更新到支持面板切换的版本。",
                409,
            )
        return await destination.entry(username)

    async def entry(self, username):
        # Only ask AstrBot for this participating plugin's own declared view.
        series = _series()
        req = series._resolve_web_request()
        identity = self.panel.identity
        view = next(page for name, _, page, _ in series.CATALOG if name == identity)
        query = getattr(req, "query", None) or {}
        locale = {"zh": "zh-CN", "en": "en-US", "ja": "ja-JP", "ru": "ru-RU"}.get(
            query.get("lang"), "zh-CN"
        )
        try:
            service = req._request.app.state.services.plugin_pages
            page = await service.get_plugin_page_entry_config(
                plugin_name=identity, view_name=view, username=username, locale=locale
            )
            path = page["content_path"]
        except Exception:
            return _error("目标插件面板暂不可用", 503)
        return series._web_json_response(
            {"status": "ok", "data": {"plugin": identity, "view": view, "path": path}}
        )

    async def call(self, target, operation):
        series = _series()
        if not await self._admin(check_csrf=True):
            return _error("需要 Dashboard 管理员登录", 403)
        destination = self._target(target)
        if destination is None:
            return _error("目标插件未就绪或需要更新", 409)
        if (
            len(operation) > 512
            or any(c in operation for c in "\\?#%")
            or any(part in ("", ".", "..") for part in operation.split("/"))
        ):
            return _error("无效的面板操作", 400)
        req = series._resolve_web_request()
        method = str(
            getattr(req, "method", None)
            or getattr(getattr(req, "_request", None), "method", None)
            or ""
        ).strip().upper()
        return await destination.run(method, operation)

    async def run(self, method, operation):
        verb = str(method or "").strip().upper()
        if verb == "GET" and operation == "series_presence":
            return await self.panel.status()
        # The target explicitly registers its own operations. Never look up arbitrary
        # host endpoints or dispatch client-supplied URLs.
        owner = self.panel.owner
        exports = getattr(owner, "_series_view_api", {})
        prefix = f"/{self.panel.identity}/"
        path = prefix + operation
        route = path
        handler = exports.get((verb, path))
        arguments = {}
        if handler is None:
            for (export_verb, route), callback in exports.items():
                if (
                    export_verb == verb
                    and route.startswith(prefix)
                    and "<" in route
                    and (match := _pattern(route).fullmatch(path))
                ):
                    handler, arguments = callback, match.groupdict()
                    break
        if handler is None:
            return _error("目标插件未提供该面板操作", 404)
        # An engine may unregister or replace a handler while the Star remains active.
        if not any(
            row[0] == route
            and row[1] == handler
            and verb in [str(m).strip().upper() for m in row[2]]
            for row in owner.context.registered_web_apis
        ):
            return _error("面板操作已更新，请重试", 409)
        return await handler(**arguments)
