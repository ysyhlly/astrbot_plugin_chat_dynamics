"""Narrow Web API compatibility layer for supported AstrBot 4.x releases."""

from __future__ import annotations

from typing import Any, Optional

try:
    from astrbot.api.web import json_response, request
except ImportError:  # AstrBot releases before astrbot.api.web
    from quart import jsonify, request

    def json_response(  # type: ignore[no-redef]
        data: Any = None,
        *,
        status_code: int = 200,
        headers: Optional[dict] = None,
    ):
        payload = {} if data is None else data
        response = jsonify(payload)
        if headers:
            for key, value in headers.items():
                response.headers[key] = value
        return response, status_code


try:
    from astrbot.api.web import error_response
except ImportError:

    def error_response(  # type: ignore[no-redef]
        message: str,
        *,
        status_code: int = 400,
        data: Any = None,
        headers: Optional[dict] = None,
    ):
        return json_response(
            {"status": "error", "message": message, "data": data, "ok": False, "error": message},
            status_code=status_code,
            headers=headers,
        )


async def request_json(default: Any = None) -> Any:
    json_loader = getattr(request, "json", None)
    if callable(json_loader):
        try:
            value = json_loader(default=default)
        except TypeError:
            # Older AstrBot/Quart request objects expose ``json()`` without
            # the optional default keyword. Retry the same official method
            # shape before falling back to ``get_json``.
            value = json_loader()
        return await value if hasattr(value, "__await__") else value
    if json_loader is not None:
        return await json_loader if hasattr(json_loader, "__await__") else json_loader
    if hasattr(request, "get_json"):
        value = request.get_json(force=True, silent=True)
        value = await value if hasattr(value, "__await__") else value
        return value or default
    return default


def query_value(name: str) -> str:
    query = getattr(request, "query", None) or getattr(request, "args", None)
    if query is None or not hasattr(query, "get"):
        return ""
    value = query.get(name)
    return str(value).strip() if value is not None else ""
