"""Native Jev status and on-demand read-only export of retained sample history."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
import time
from pathlib import Path

from . import web_api as web

logger = logging.getLogger(__name__)


def history_page(path: Path, *, cursor=0, limit=500, session_key="", show_content=False):
    if type(cursor) is not int or not 0 <= cursor <= 2**63 - 1:
        raise ValueError("invalid cursor")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid limit")
    if not path.is_file():
        return {"records": [], "next_cursor": None, "content_hidden": not show_content}
    # No creation, migration, collection, leasing, or cleanup on a status/export read.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        where, args = "rowid > ?", [cursor]
        if session_key:
            secret = db.execute("SELECT value FROM settings WHERE key='secret_hex'").fetchone()
            anonymous = "anon_" + hmac.new(bytes.fromhex(secret[0]), session_key.encode(), hashlib.sha256).hexdigest()[:32]
            where += " AND session=?"
            args.append(anonymous)
        rows = db.execute(
            f"SELECT rowid,CASE WHEN length(payload)<=262144 THEN payload ELSE NULL END FROM samples WHERE {where} ORDER BY rowid LIMIT ?",
            (*args, limit + 1))
        records, used, last = [], 0, cursor
        more = False
        for index, (rowid, payload) in enumerate(rows):
            if index == limit:
                more = True
                break
            if payload is None:
                record = {"export_warning": "record_too_large", "row": rowid}
            else:
                record = json.loads(payload)
            if not show_content:
                record = {key: record[key] for key in ("id", "task_id", "task_version", "created", "teacher_model", "export_warning", "row") if key in record}
                record["content_hidden"] = True
            size = len(json.dumps(record, ensure_ascii=False).encode())
            if records and used + size > 2 * 1024 * 1024:
                more = True
                break
            records.append(record)
            used += size
            last = rowid
        return {"records": records, "next_cursor": str(last) if more else None,
                "content_hidden": not show_content}
    finally:
        db.close()


class DecisionStatusWebAPI(web.ConsoleWebAPI):
    def register(self):
        context = getattr(self.plugin, "context", None)
        if not context or not hasattr(context, "register_web_api"):
            return
        # AstrBot keeps handlers from older plugin generations after reload.
        old = getattr(context, "registered_web_apis", None)
        if isinstance(old, list):
            old[:] = [entry for entry in old if not str(entry[0]).startswith(f"/{web.PLUGIN_NAME}/learning/")]
        routes = [("status", self.status, ["GET"]), ("history", self.history, ["POST"])]
        for name, handler, methods in routes:
            route = f"/{web.PLUGIN_NAME}/decision/{name}"
            key = self._endpoint_key(route, methods)
            if key not in self._registered_endpoints:
                context.register_web_api(route, handler, methods, "Jev 决策状态")
                self._registered_endpoints.add(key)
        self.registered = len(self._registered_endpoints) == len(routes)

    async def status(self):
        if (error := self._rate_limit("decision/status", 60)) is not None:
            return error
        if getattr(self.plugin, "_shutting_down", False):
            return web._json_err("plugin is shutting down", 503)
        cfg = self.plugin._runtime_config
        client = getattr(self.plugin, "jev", None)
        service = client.snapshot() if client else {}
        return web._json_ok({
            "backend": "jev", "decision_mode": cfg.decision_mode,
            "provider_id": cfg.decision_provider_id,
            "enabled": cfg.enabled and not cfg.shadow_mode and cfg.decision_mode == "persona_model",
            "service": {key: service[key] for key in (
                "configured", "available", "status", "detail", "model", "calls", "failures", "last_latency_ms") if key in service},
        })

    async def history(self):
        if (error := self._rate_limit("decision/history", 30)) is not None:
            return error
        if getattr(self.plugin, "_shutting_down", False):
            return web._json_err("plugin is shutting down", 503)
        body = await web._json_body()
        if (error := self._validate_fields(body, {"cursor", "limit", "session_key"})) is not None:
            return error
        cursor, limit, session = body.get("cursor", "0"), body.get("limit", 500), body.get("session_key", "")
        if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit() or len(cursor) > 19:
            return web._json_err("invalid cursor")
        if type(limit) is not int or not 1 <= limit <= 1000 or not isinstance(session, str) or len(session) > 256:
            return web._json_err("invalid page")
        try:
            return web._json_ok(await asyncio.to_thread(
                history_page, self.plugin._decision_history_path, cursor=int(cursor), limit=limit,
                session_key=session, show_content=self.plugin._runtime_config.console_show_message_content))
        except ValueError:
            return web._json_err("invalid page")
        except Exception:
            logger.exception("Read-only decision history export failed")
            return web._json_err("history unavailable", 503)
