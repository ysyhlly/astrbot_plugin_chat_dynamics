"""Optional companion discovery; native hooks and scoped direct APIs are distinct."""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SelfLearningStatus(str, Enum):
    CONNECTED = "connected"
    MISSING = "missing"
    DEGRADED = "degraded"


_METHODS = {
    "memories": (
        "get_approved_memories",
        "list_approved_memories",
        "fetch_memories",
        "query_memories",
    ),
    "relationships": (
        "get_relationship_hints",
        "list_relationships",
        "relationship_snapshot",
    ),
    "slang": (
        "get_slang_candidates",
        "list_slang",
        "approved_slang",
        "list_approved_slang",
    ),
}
_NATIVE_HOOKS = ("inject_diversity_to_llm_request", "handle_memory_recall")


def _get(obj, key, default=None):
    try:
        return (
            obj.get(key, default)
            if isinstance(obj, dict)
            else getattr(obj, key, default)
        )
    except Exception:
        return default


def _matches(name):
    value = "".join(c for c in str(name).lower() if c.isalnum())
    return any(
        hint in value for hint in ("selflearning", "livingmemory", "liveingmemory")
    )


class SelfLearningBridge:
    def __init__(self, context: Any = None, *, enabled: bool = True) -> None:
        self.context = context
        self.enabled = bool(enabled)
        self.status = SelfLearningStatus.MISSING
        self.detail = "not_checked"
        self._providers: list[dict[str, Any]] = []
        self._errors: dict[tuple[int, str], str] = {}
        self._discovery_errors: list[str] = []
        self._generation = 0
        self._pending: set[asyncio.Future] = set()
        self.timeout = 2.0
        # Post-send learning may perform database/model IO in the background.
        # It must not inherit the latency budget for foreground context reads.
        self.delivery_timeout = 60.0

    def configure(self, *, enabled: bool, context: Any = None) -> None:
        if bool(enabled) != self.enabled or (
            context is not None and context is not self.context
        ):
            self._generation += 1
        self.enabled = bool(enabled)
        if context is not None:
            self.context = context
        self.refresh()

    def _find_plugins(self):
        seen = set()
        for attr in (
            "get_all_stars",
            "get_all_plugins",
            "get_plugins",
            "loaded_plugins",
            "_star_registry",
            "plugin_manager",
            "stars",
        ):
            try:
                source = getattr(self.context, attr, None)
                source = source() if callable(source) else source
                entries = (
                    source.items()
                    if isinstance(source, dict)
                    else (("", item) for item in (source or []))
                )
                for key, item in entries:
                    names = [
                        key,
                        _get(item, "name"),
                        _get(item, "plugin_name"),
                        _get(_get(item, "metadata"), "name"),
                        _get(item, "root_dir_name"),
                        _get(item, "module_path"),
                        type(item).__name__,
                    ]
                    if not any(_matches(name) for name in names) or not _get(
                        item, "activated", True
                    ):
                        continue
                    # StarMetadata.star_cls is the live instance, not the class type.
                    fallback = _get(item, "star") or _get(item, "plugin") or item
                    plugin = _get(item, "star_cls", fallback)
                    if plugin is None or id(plugin) in seen:
                        continue
                    seen.add(id(plugin))
                    yield (
                        str(_get(item, "name") or key or type(plugin).__name__),
                        plugin,
                    )
            except Exception as exc:
                self._discovery_errors.append(type(exc).__name__)
                logger.debug("companion discovery failed: %s", type(exc).__name__)

    def refresh(self) -> SelfLearningStatus:
        previous = {id(p["plugin"]) for p in self._providers}
        self._providers = []
        self._discovery_errors = []
        if not self.enabled:
            self._errors.clear()
            self.status, self.detail = (
                SelfLearningStatus.MISSING,
                "integration_disabled",
            )
            return self.status
        for name, plugin in self._find_plugins():
            targets = [plugin]
            for attr in ("selflearning_api", "memory_api", "api", "bridge"):
                api = _get(plugin, attr)
                if api is not None and all(api is not t for t in targets):
                    targets.append(api)
            capabilities = {
                cap: [
                    (t, method)
                    for t in targets
                    for method in methods
                    if callable(_get(t, method))
                ]
                for cap, methods in _METHODS.items()
            }
            hooks = [hook for hook in _NATIVE_HOOKS if callable(_get(plugin, hook))]
            ready = True
            if "inject_diversity_to_llm_request" in hooks:
                ready = _get(plugin, "_hook_handler") is not None
            if "handle_memory_recall" in hooks:
                ready = ready and bool(
                    _get(_get(plugin, "initializer"), "is_initialized", False)
                )
            self._providers.append(
                dict(
                    name=name,
                    plugin=plugin,
                    capabilities=capabilities,
                    hooks=hooks,
                    delivery_hooks=["on_bot_message_sent"] if callable(_get(plugin, "on_bot_message_sent")) else [],
                    ready=ready,
                )
            )
        current = {id(p["plugin"]) for p in self._providers}
        self._errors = {
            key: value
            for key, value in self._errors.items()
            if key[0] in current & previous
        }
        self._update_status()
        return self.status

    def _update_status(self):
        if not self._providers:
            if self._discovery_errors:
                self.status = SelfLearningStatus.DEGRADED
                self.detail = "detect_error:" + self._discovery_errors[0]
            else:
                self.status, self.detail = (
                    SelfLearningStatus.MISSING,
                    "plugin_not_found",
                )
        elif self._errors:
            self.status, self.detail = (
                SelfLearningStatus.DEGRADED,
                next(iter(self._errors.values())),
            )
        elif any(
            not any(p["capabilities"].values()) and not (p["hooks"] and p["ready"])
            for p in self._providers
        ):
            self.status = SelfLearningStatus.DEGRADED
            self.detail = (
                "plugin_initializing"
                if any(p["hooks"] and not p["ready"] for p in self._providers)
                else "plugin_found_no_api"
            )
        elif any(
            any(p["capabilities"].values()) or (p["hooks"] and p["ready"])
            for p in self._providers
        ):
            self.status = SelfLearningStatus.CONNECTED
            self.detail = (
                "native_hooks"
                if any(p["hooks"] and p["ready"] for p in self._providers)
                else "api_detected"
            )
        else:
            self.status = SelfLearningStatus.DEGRADED
            self.detail = (
                "plugin_initializing"
                if any(p["hooks"] for p in self._providers)
                else "plugin_found_no_api"
            )

    def snapshot(self) -> dict[str, Any]:
        self.refresh()
        providers = []
        for p in self._providers:
            errors = {
                cap: error
                for (pid, cap), error in self._errors.items()
                if pid == id(p["plugin"])
            }
            providers.append(
                {
                    "name": p["name"],
                    "mode": "native_hooks" if p["hooks"] else "direct_api",
                    "ready": p["ready"],
                    "native_hooks": p["hooks"],
                    "delivery_hooks": p["delivery_hooks"],
                    "input_hooks": ["on_message"] if callable(_get(p["plugin"], "on_message")) else [],
                    "native_commands": [name for name in (
                        "learning_status_command", "start_learning_command", "stop_learning_command",
                        "force_learning_command", "remember_command", "affection_status_command", "set_mood_command"
                    ) if callable(_get(p["plugin"], name))],
                    "direct_methods": {cap: sorted({method for _, method in calls})
                                       for cap, calls in p["capabilities"].items() if calls},
                    "capabilities": [
                        cap for cap, calls in p["capabilities"].items() if calls
                    ],
                    "errors": errors,
                    "detail": "plugin_found_no_api"
                    if not p["hooks"] and not any(p["capabilities"].values())
                    else "",
                }
            )
        native = any(p["hooks"] and p["ready"] for p in self._providers)
        weakened = (
            []
            if native
            else [
                label
                for cap, label in (
                    ("memories", "无直连记忆接口"),
                    ("relationships", "无直连关系接口"),
                    ("slang", "无直连黑话审查接口"),
                )
                if not any(p["capabilities"][cap] for p in self._providers)
            ]
        )
        return {
            "status": self.status.value,
            "detail": self.detail,
            "enabled": self.enabled,
            "providers": providers,
            "weakened": weakened,
            "lamp": "已关闭"
            if not self.enabled
            else (
                "原生钩子"
                if native and self.status == SelfLearningStatus.CONNECTED
                else {
                    SelfLearningStatus.CONNECTED: "接口已发现",
                    SelfLearningStatus.MISSING: "未安装",
                    SelfLearningStatus.DEGRADED: "降级中",
                }[self.status]
            ),
        }

    def uses_native_hooks(self) -> bool:
        self.refresh()
        # Even initializing companions own their native injection; do not duplicate it.
        return any(p["hooks"] for p in self._providers)

    @staticmethod
    def _arguments(fn, kwargs):
        params = inspect.signature(fn).parameters
        wildcard = any(p.kind == p.VAR_KEYWORD for p in params.values())
        if not kwargs.get("umo"):
            raise ValueError("missing_umo")
        if not wildcard and not any(
            k in params for k in ("umo", "unified_msg_origin", "session_id")
        ):
            raise ValueError("unscoped_api")
        if (
            kwargs.get("peer_id")
            and not wildcard
            and not any(k in params for k in ("peer_id", "user_id"))
        ):
            raise ValueError("unscoped_peer_api")
        aliases = dict(
            kwargs,
            user_id=kwargs.get("peer_id", ""),
            unified_msg_origin=kwargs["umo"],
            session_id=kwargs["umo"],
        )
        args = {k: v for k, v in aliases.items() if k in params}
        if wildcard:
            args.update(kwargs)
            if "peer_id" in kwargs:
                args["user_id"] = kwargs["peer_id"]
        inspect.signature(fn).bind(**args)
        return args

    @staticmethod
    def _normalize(result, method, kwargs, capability):
        if isinstance(result, dict):
            if (
                result.get("ok") is False
                or result.get("status") in ("error", "failed")
                or result.get("error")
            ):
                raise ValueError("api_error_result")
            result = next(
                (
                    result[k]
                    for k in ("items", "memories", "relationships", "slang", "candidates", "data", "results")
                    if isinstance(result.get(k), list)
                ),
                [result],
            )
        if result is None:
            return []
        if not isinstance(result, (list, tuple)):
            raise ValueError("invalid_result")
        rows = []
        approved_method = "approved" in method
        for item in result:
            if isinstance(item, str):
                item = {"text": item[:120], "tag": item[:32]}
            if not isinstance(item, dict):
                continue
            if any(
                item.get(k) is not None and str(item[k]) != kwargs["umo"]
                for k in ("umo", "unified_msg_origin", "session_id")
            ):
                continue
            if kwargs.get("peer_id") and any(
                item.get(k) is not None and str(item[k]) != kwargs["peer_id"]
                for k in ("peer_id", "user_id")
            ):
                continue
            if capability in ("memories", "slang"):
                if item.get("approved") is False or item.get("status") in (
                    "pending",
                    "rejected",
                ):
                    continue
                if (
                    not approved_method
                    and item.get("approved") is not True
                    and item.get("status") != "approved"
                ):
                    continue
            rows.append(item)
        return rows[: max(0, min(int(kwargs.get("limit", 8)), 20))]

    def _record(self, provider, cap, error=None):
        key = (id(provider["plugin"]), cap)
        if error:
            # Never expose upstream exception messages (may include memory text or credentials).
            self._errors[key] = "call_error:" + type(error).__name__
        else:
            self._errors.pop(key, None)
        self._update_status()

    def _safe_call(self, cap, **kwargs):
        self.refresh()
        rows = []
        for p in self._providers:
            for target, method in p["capabilities"][cap]:
                try:
                    fn = getattr(target, method)
                    if inspect.iscoroutinefunction(fn):
                        raise ValueError("async_api_requires_await")
                    result = fn(**self._arguments(fn, kwargs))
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise ValueError("async_api_requires_await")
                    rows.extend(self._normalize(result, method, kwargs, cap))
                    self._record(p, cap)
                    break
                except Exception as exc:
                    self._record(p, cap, exc)
        return rows[: kwargs.get("limit", 8)]

    async def close(self) -> None:
        """Cancel and await companion IO owned by this bridge during unload."""
        self.configure(enabled=False)
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _safe_call_async(self, cap, **kwargs):
        exclude_native = kwargs.pop("exclude_native", False)
        self.refresh()
        generation = self._generation
        deadline = asyncio.get_running_loop().time() + self.timeout
        rows = []
        for p in list(self._providers):
            if exclude_native and p["hooks"]:
                continue
            for target, method in p["capabilities"][cap]:
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError()
                    fn = getattr(target, method)
                    result = fn(**self._arguments(fn, kwargs))
                    if inspect.isawaitable(result):
                        task = asyncio.ensure_future(result)
                        self._pending.add(task)
                        try:
                            result = await asyncio.wait_for(task, timeout=remaining)
                        finally:
                            self._pending.discard(task)
                    self.refresh()
                    if not self.enabled or generation != self._generation:
                        return []
                    if not any(
                        live["plugin"] is p["plugin"] for live in self._providers
                    ):
                        break
                    rows.extend(self._normalize(result, method, kwargs, cap))
                    self._record(p, cap)
                    break
                except Exception as exc:
                    if self.enabled and generation == self._generation:
                        self._record(p, cap, exc)
        return rows[: kwargs.get("limit", 8)]

    def fetch_approved_memories(
        self, *, umo: str = "", peer_id: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        return self._safe_call("memories", umo=umo, peer_id=peer_id, limit=limit)

    def fetch_relationship_hints(
        self, *, umo: str = "", peer_id: str = "", limit: int = 4
    ) -> list[dict[str, Any]]:
        return self._safe_call("relationships", umo=umo, peer_id=peer_id, limit=limit)

    def fetch_slang_candidates(
        self, *, umo: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        return self._safe_call("slang", umo=umo, limit=limit)

    async def fetch_approved_memories_async(
        self, *, umo: str = "", peer_id: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        return await self._safe_call_async(
            "memories", umo=umo, peer_id=peer_id, limit=limit
        )

    async def fetch_relationship_hints_async(
        self, *, umo: str = "", peer_id: str = "", limit: int = 4, exclude_native: bool = False
    ) -> list[dict[str, Any]]:
        return await self._safe_call_async(
            "relationships", umo=umo, peer_id=peer_id, limit=limit, exclude_native=exclude_native
        )

    async def relationship_context(self, *, umo: str, peer_id: str) -> list[dict]:
        """Only direct-only companions need supplementary model injection."""
        rows = await self.fetch_relationship_hints_async(umo=umo, peer_id=peer_id, exclude_native=True)
        allowed = ("text", "hint", "relationship", "description", "label", "target_id", "peer_id", "user_id")
        return [clean for row in rows if (clean := {
            key: str(row[key])[:240] for key in allowed
            if isinstance(row.get(key), (str, int, float)) and not isinstance(row.get(key), bool)
        })]

    async def model_context(self, *, umo: str, peer_id: str, allow_memories: bool = True) -> dict:
        """Read all supported direct capabilities once, excluding native owners."""
        async def read(cap):
            if cap == "memories" and not allow_memories:
                return []
            args = dict(umo=umo, limit=4, exclude_native=True)
            if cap != "slang":
                args["peer_id"] = peer_id
            return await self._safe_call_async(cap, **args)

        caps = ("memories", "relationships", "slang")
        results = await asyncio.gather(*(read(cap) for cap in caps))
        fields = {
            "memories": ("text", "content", "memory", "tag", "label"),
            "relationships": ("text", "hint", "relationship", "description", "label", "target_id"),
            "slang": ("text", "phrase", "term", "meaning", "definition", "usage", "context"),
        }
        payload = {}
        for cap, rows in zip(caps, results):
            cleaned = [{key: row[key][:400] for key in fields[cap] if isinstance(row.get(key), str)} for row in rows]
            cleaned = [row for row in cleaned if row]
            if cleaned:
                payload[cap] = cleaned
        return payload

    def note_delivered(self, event: Any, content: Any) -> None:
        """Notify the verified upstream post-send hook for manual sends only.

        Schedule after confirmed delivery so slow learning IO cannot turn a
        successful send into an unrecorded/cancelled send. Never retry writes.
        """
        self.refresh()
        if not self.enabled or event is None or len(self._pending) >= 32:
            return
        from .platform_bridge import build_plain_chain
        try:
            delivered_event = copy.copy(event)
            chain = build_plain_chain(content) if isinstance(content, str) else copy.copy(content)
            chain.chain = list(chain.chain)
            delivered_event.get_result = lambda: chain
            if hasattr(event, "_extras"):
                delivered_event._extras = dict(event._extras)
        except Exception:
            return
        generation = self._generation
        providers = [p for p in self._providers if p["delivery_hooks"] and p["ready"]]
        if not providers:
            return

        async def notify():
            for provider in providers:
                self.refresh()
                if not self.enabled or generation != self._generation:
                    return
                if not any(p["plugin"] is provider["plugin"] for p in self._providers):
                    continue
                try:
                    await asyncio.wait_for(
                        provider["plugin"].on_bot_message_sent(delivered_event),
                        self.delivery_timeout,
                    )
                except Exception as exc:
                    error = exc
                else:
                    error = None
                self.refresh()
                if not self.enabled or generation != self._generation:
                    return
                if any(p["plugin"] is provider["plugin"] for p in self._providers):
                    self._record(provider, "delivered_messages", error)

        task = asyncio.create_task(notify())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def fetch_slang_candidates_async(
        self, *, umo: str = "", limit: int = 8
    ) -> list[dict[str, Any]]:
        return await self._safe_call_async("slang", umo=umo, limit=limit)
