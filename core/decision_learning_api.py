"""Decision learning routes behind AstrBot Dashboard/plugin-scope authorization.

Register through Context.register_web_api only: that dispatcher authenticates the
administrator and plugin scope. This module never treats a username as a role.
"""
from __future__ import annotations

import inspect
import logging
from functools import partial

from . import web_api as web

logger = logging.getLogger(__name__)


class DecisionLearningWebAPI(web.ConsoleWebAPI):
    ACTIONS = {
        "jobs/create": {"dataset", "model_id", "epochs", "seed"},
        "jobs/status": {"job_id"},
        "jobs/cancel": {"job_id"},
        "models/evaluate": {"model_id"},
        "models/promote": {"model_id"},
        "models/rollback": set(),
        "models/rollout": set(),
        "models/compare_jev": set(),
        "models/compare_teacher": {"limit"},
        "samples/export": {"session_key", "limit", "cursor"},
        "samples/delete": {"session_key"},
    }

    def register(self):
        context = getattr(self.plugin, "context", None)
        if not context or not hasattr(context, "register_web_api"):
            return
        routes = [("stats", self.stats, ["GET"])] + [
            (action, partial(self.dispatch, action), ["POST"]) for action in self.ACTIONS
        ]
        for endpoint, handler, methods in routes:
            route = f"/{web.PLUGIN_NAME}/learning/{endpoint}"
            key = self._endpoint_key(route, methods)
            if key in self._registered_endpoints:
                continue
            try:
                context.register_web_api(route, handler, methods, "决策学习管理")
            except Exception:
                logger.exception("Decision learning route registration failed: %s", endpoint)
                continue
            self._registered_endpoints.add(key)
        self.registered = len(self._registered_endpoints) == len(routes)

    def _guard(self, action):
        limit = 600 if action == "samples/export" else 60 if action == "stats" else 12
        limited = self._rate_limit("learning/" + action, limit)
        if limited is not None:
            return limited
        if getattr(self.plugin, "_shutting_down", False):
            return web._json_err("plugin is shutting down", 503)
        if getattr(self.plugin, "decision_learning", None) is None:
            return web._json_err("decision learning unavailable", 503)
        return None

    async def stats(self):
        if (error := self._guard("stats")) is not None:
            return error
        try:
            result = self.plugin.decision_learning.snapshot()
            return web._json_ok(await result if inspect.isawaitable(result) else result)
        except Exception:
            logger.exception("Decision learning snapshot failed")
            return web._json_err("decision learning unavailable", 503)

    async def dispatch(self, action):
        if action not in self.ACTIONS:
            return web._json_err("unknown learning action", 404)
        if (error := self._guard(action)) is not None:
            return error
        body = await web._json_body()
        if (error := self._validate_fields(body, self.ACTIONS[action])) is not None:
            return error
        for name in ("session_key", "job_id", "model_id", "dataset", "cursor"):
            if name in body and (not isinstance(body[name], str) or not body[name].strip() or len(body[name]) > 256):
                return web._json_err(f"invalid {name}")
        required = {"jobs/cancel": "job_id", "models/evaluate": "model_id",
                    "models/promote": "model_id", "samples/delete": "session_key"}.get(action)
        if required and required not in body:
            return web._json_err(f"missing {required}")
        if action == "jobs/create" and not body.get("model_id"):
            return web._json_err("model_id required")
        if "epochs" in body and (type(body["epochs"]) is not int or not 1 <= body["epochs"] <= 20):
            return web._json_err("epochs must be between 1 and 20")
        if "seed" in body and (type(body["seed"]) is not int or not 1 <= body["seed"] <= 2**31):
            return web._json_err("seed must be between 1 and 2147483648")
        if "limit" in body and (type(body["limit"]) is not int or not 1 <= body["limit"] <= 1000):
            return web._json_err("limit must be between 1 and 1000")
        if action == "models/compare_teacher" and not 1 <= body.get("limit", 6) <= 6:
            return web._json_err("comparison limit must be between 1 and 6")
        try:
            return web._json_ok(await self.plugin.decision_learning.management(action, body))
        except (ValueError, KeyError):
            return web._json_err("invalid operation or unknown identifier", 400)
        except PermissionError:
            return web._json_err("operation refused", 403)
        except FileNotFoundError:
            return web._json_err("requested artifact not found", 404)
        except NotImplementedError:
            return web._json_err("operation unavailable", 503)
        except Exception:
            logger.exception("Decision learning operation failed: %s", action)
            return web._json_err("decision learning operation failed", 503)
