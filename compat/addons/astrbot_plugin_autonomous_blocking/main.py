"""Standalone AstrBot policy plugin; no changes to AstrBot or Chat Dynamics internals."""

from __future__ import annotations

import asyncio
import copy
import inspect
import re
import time
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool, ToolSet, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import (
    EventMessageType,
    event_message_type,
    on_llm_request,
)
from astrbot.api.star import Star, StarTools, register
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.tool import FunctionTool as AstrFunctionTool
from astrbot.core.message.message_event_result import MessageChain

from .core.admin_api import BlockingAdminApi
from .core.filters import filter_frames as _filter_frames
from .core.filters import filter_material as _filter_material
from .core.runtime import BlockingRuntime, PolicyDenied, parse_duration
from .core.runtime import resolve_context as _resolve_context
from .core.store import PolicyStore

# Returned instead of cancelling the turn: the unfiltered output must not reach
# the model, but the agent still has to produce a reply.
WITHHELD_TOOL_OUTPUT = (
    "Tool output withheld: it could not be provenance-filtered under the active "
    "interaction boundaries. Answer without this tool's result."
)


@register("autonomous_blocking", "OpenAI", "独立的用户交互边界控制层", "0.1.1")
class AutonomousBlocking(Star):
    def __init__(self, context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.default_scope = self.config.get("default_scope", "bot")
        self.protected_user_ids = {str(value) for value in self.config.get("protected_user_ids", [])}
        self._store: PolicyStore | None = None
        self.runtime: BlockingRuntime | None = None
        self._event_refs: list[weakref.ReferenceType] = []
        self._message_authors: OrderedDict[tuple[str, str, str], OrderedDict[str, str]] = OrderedDict()
        self._request_tokens = weakref.WeakKeyDictionary()
        self._admin_api = BlockingAdminApi(self)

    async def initialize(self) -> None:
        if not self.enabled:
            return
        data_dir = StarTools.get_data_dir("astrbot_plugin_autonomous_blocking")
        self._store = PolicyStore(Path(data_dir) / "blocking.sqlite3")
        self.runtime = BlockingRuntime(self._store)
        self._admin_api.register_routes()

    async def terminate(self) -> None:
        for reference in self._event_refs:
            event = reference()
            if event is None:
                continue
            event._ablock_retired = True
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
        self._event_refs.clear()
        self._message_authors.clear()
        self._request_tokens.clear()
        if self._store:
            self._store.close()
        self.runtime = None
        self._store = None

    def resolve_context(self, event=None, scope=None):
        context = _resolve_context(event, scope)
        if not self.enabled:
            context["disabled"] = True
            context["ready"] = False
        elif self.runtime is None:
            context["unavailable"] = True
            context["ready"] = False
        return context

    def peek_policy(self, context, target_uid):
        if not self.enabled or self.runtime is None:
            if self.enabled:
                raise PolicyDenied("policy runtime is unavailable")
            return {"mode": None, "active": False, "reply_allowed": True, "read_allowed": True,
                    "owner_revision": 0, "target_revision": 0, "shield_epoch": 0, "rules": []}
        return self.runtime.peek_policy(context, target_uid)

    async def get_policy(self, context, target_uid):
        return self.peek_policy(context, target_uid)

    def policy_snapshot(self, context):
        if not self.enabled or self.runtime is None:
            if self.enabled:
                raise PolicyDenied("policy runtime is unavailable")
            return {"schema_version": 1, "owner": context.get("owner"), "umo": context.get("umo", ""),
                    "owner_revision": 0, "shield_epoch": 0, "targets": {}, "rules": [], "active": False}
        return self.runtime.policy_snapshot(context)

    @asynccontextmanager
    async def send_lease(self, context, target_uid, token=None):
        if not self.enabled or self.runtime is None:
            if self.enabled:
                raise PolicyDenied("policy runtime is unavailable")
            yield True
            return
        async with self.runtime.send_lease(context, target_uid, token):
            yield True

    def filter_frames(self, context, frames, purpose=""):
        if not self.enabled:
            return copy.deepcopy(frames)
        if self.runtime is None:
            raise PolicyDenied("policy runtime is unavailable")
        return _filter_frames(self.runtime, context, frames, purpose)

    def filter_material(self, context, bundle, purpose=""):
        if not self.enabled:
            return copy.deepcopy(bundle)
        if self.runtime is None:
            raise PolicyDenied("policy runtime is unavailable")
        return _filter_material(self.runtime, context, bundle, purpose)

    @event_message_type(EventMessageType.ALL, priority=100000)
    async def _policy_entry(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        if self.runtime is None:
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        command = self._parse_admin_command(event.get_message_str())
        if command is not None and self._is_admin(event):
            await self._handle_admin_command(event, command)
            return

        context = self.resolve_context(event)
        if not context["ready"]:
            # Unknown bot identity must never inherit another instance's policy.
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        uid = str(event.get_sender_id() or "").strip()
        if not uid:
            return
        self._record_inbound(context, event, uid)
        try:
            policy = self.runtime.peek_policy(context, uid)
        except Exception as exc:
            self._log_error("policy lookup failed closed", exc)
            event.set_extra("agent_stop_requested", True)
            event.stop_event()
            return
        try:
            self._sanitize_inbound_references(event, context)
            if any(rule.get("mode") == "shield" for rule in self.runtime.policy_snapshot(context).get("rules", [])):
                event.set_extra("_autonomous_blocking_context_v1", {
                    "schema_version": 1,
                    "projection_restricted": True,
                })
            self._install_event_guards(event, context, uid, policy)
        except Exception as exc:
            self._log_error("policy initialization failed closed", exc)
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if policy["mode"] == "shield":
            event.set_extra("agent_stop_requested", True)
            event.set_extra("_ablock_at_arrival_mode", "shield")
            event.should_call_llm(False)
            event.stop_event()
        elif policy["mode"] == "silence":
            # Observers may still inspect the event; the low-priority finalizer
            # stops native collectors and remembers the arrival-time decision.
            event.set_extra("_ablock_at_arrival_mode", "silence")
            event.should_call_llm(False)

    #: 边界运行时未就绪时的告警限流窗口。
    _RUNTIME_WARN_INTERVAL_SECONDS = 300.0

    def _warn_runtime_unavailable(self) -> None:
        """运行时未就绪时按 5 分钟限流打一条 ERROR。

        旧行为是**静默** fail-closed：runtime 为 None（initialize 未跑完/失败，或
        terminate 之后）时对每条消息 set agent_stop_requested + stop_event，但一行
        日志都不打；运维侧只看到「机器人突然对所有消息都不回复」而无线索。
        这里只增加可观测性，不改变拦截语义。
        """
        now = time.monotonic()
        if now - getattr(self, "_runtime_warn_last", 0.0) < self._RUNTIME_WARN_INTERVAL_SECONDS:
            return
        self._runtime_warn_last = now
        logger.error(
            "[autonomous_blocking] 边界运行时未就绪（runtime is None）："
            "所有消息都会被 fail-closed 拦截（不回复）。请检查插件初始化日志。"
        )

    @event_message_type(EventMessageType.ALL, priority=-100000)
    async def _silence_finalizer(self, event: AstrMessageEvent):
        if self.enabled and self.runtime is None:
            self._warn_runtime_unavailable()
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if event.get_extra("_ablock_at_arrival_mode") == "silence":
            event.should_call_llm(False)
            event.stop_event()

    @on_llm_request(priority=100000)
    async def _guard_llm_request(self, event: AstrMessageEvent, req: Any):
        if not self.enabled:
            return
        if self.runtime is None:
            self._warn_runtime_unavailable()
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        context = self.resolve_context(event)
        uid = str(event.get_sender_id() or "").strip()
        if not context["ready"] or not uid:
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if getattr(event, "_ablock_retired", False):
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            policy = self.runtime.peek_policy(context, uid)
        except Exception as exc:
            self._log_error("LLM policy lookup failed closed", exc)
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            self._ensure_event_guards(event, context, uid, policy)
        except Exception as exc:
            self._log_error("LLM event guard unavailable", exc)
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            request_snapshot = self.runtime.policy_snapshot(context)
        except Exception as exc:
            self._log_error("LLM policy snapshot failed closed", exc)
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if any(rule.get("mode") == "shield" for rule in request_snapshot.get("rules", [])):
            event.set_extra("_autonomous_blocking_context_v1", {
                "schema_version": 1,
                "projection_restricted": True,
            })
        if policy["mode"] in {"silence", "shield"} or event.get_extra("_ablock_at_arrival_mode"):
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            self._request_tokens[event] = self.runtime.capture_token(context, uid)
        except TypeError:
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if any(rule.get("mode") == "shield" for rule in request_snapshot.get("rules", [])):
            self._filter_request_history(req, context)

    @on_llm_request(priority=-100000)
    async def _finalize_llm_request(self, event: AstrMessageEvent, req: Any):
        """Run after other request hooks so injected history/tools are guarded too."""
        if not self.enabled:
            return
        if self.runtime is None:
            self._warn_runtime_unavailable()
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        context = self.resolve_context(event)
        uid = str(event.get_sender_id() or "").strip()
        if not context["ready"] or not uid:
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if getattr(event, "_ablock_retired", False):
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            policy = self.runtime.peek_policy(context, uid)
        except Exception as exc:
            self._log_error("final LLM policy lookup failed closed", exc)
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            self._ensure_event_guards(event, context, uid, policy)
        except Exception as exc:
            self._log_error("final LLM event guard unavailable", exc)
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if policy["mode"] in {"silence", "shield"} or event.get_extra("_ablock_at_arrival_mode"):
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            token = self._request_tokens.get(event)
        except TypeError:
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if token is None:
            token = self.runtime.capture_token(context, uid)
        if any(token.get(field) != policy.get(field) for field in ("target_revision", "shield_epoch")):
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        try:
            final_snapshot = self.runtime.policy_snapshot(context)
        except Exception as exc:
            self._log_error("final LLM snapshot failed closed", exc)
            event.set_extra("agent_stop_requested", True)
            event.should_call_llm(False)
            event.stop_event()
            return
        if any(rule.get("mode") == "shield" for rule in final_snapshot.get("rules", [])):
            event.set_extra("_autonomous_blocking_context_v1", {
                "schema_version": 1,
                "projection_restricted": True,
            })
            self._filter_request_history(req, context)
        self._copy_and_guard_tools(req, event, context, uid, token)
        self._append_mutation_guidance(req, event)

    @staticmethod
    def _append_mutation_guidance(req: Any, event: AstrMessageEvent) -> None:
        if event.get_extra("_narrative_internal_v1") or event.get_extra("_ablock_guidance_added"):
            return
        tools = getattr(getattr(req, "func_tool", None), "tools", []) or []
        if not any(getattr(tool, "name", "") in {
            "set_interaction_boundary", "clear_interaction_boundary"
        } for tool in tools):
            return
        guidance = (
            "Interaction boundary authorization: the bot owner permits these tools to set or clear this bot's "
            "existing user boundary when warranted. Set may target only the verified author of the current event; "
            "never block a third party because another user asked. Plain feedback alone does not require a block, "
            "and a rule need not be created every turn. Do not send a blocked-user acknowledgement or ask that "
            "person to confirm. An active boundary takes precedence over @mentions or wake requests."
        )
        req.system_prompt = (str(getattr(req, "system_prompt", "") or "").rstrip() + "\n\n" + guidance).lstrip()
        event.set_extra("_ablock_guidance_added", True)

    def _filter_request_history(self, req: Any, context: dict[str, Any]) -> None:
        # The currently blocked actor is stopped before this hook. For older
        # history, only explicit source metadata is trusted; unknown user/assistant
        # turns are dropped while a shield rule exists. Static instructions remain.
        if not getattr(req, "contexts", None):
            return
        shielded = {
            str(rule["target_uid"])
            for rule in self.runtime.policy_snapshot(context).get("rules", [])
            if rule.get("mode") == "shield"
        }
        filtered = []
        metadata_keys = {"author_uid", "source_uids", "provenance_complete", "subject_id", "source_id",
                         "_ablock_provenance"}
        for item in req.contexts:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "")
            if role in {"system", "developer"}:
                filtered.append(item)
                continue
            # AstrBot's non-persisted persona/example prefix is static host
            # material. Native request history has no trusted speaker identity.
            if item.get("_no_save"):
                filtered.append({key: value for key, value in item.items() if key not in metadata_keys})
                continue
            if not shielded:
                filtered.append(item)
                continue
            sources = set()
            for key in ("author_uid", "subject_id"):
                if item.get(key):
                    sources.add(str(item[key]))
            if isinstance(item.get("source_uids"), (list, tuple, set)):
                sources.update(str(value) for value in item["source_uids"])
            if sources & shielded or item.get("_ablock_provenance") != "trusted" or not item.get("provenance_complete"):
                continue
            clean = {key: value for key, value in item.items() if key not in metadata_keys}
            filtered.append(clean)
        req.contexts = filtered

    def _copy_and_guard_tools(self, req: Any, event: AstrMessageEvent, context: dict[str, Any], uid: str,
                              token: dict[str, int] | None = None):
        if event.get_extra("_narrative_internal_v1"):
            return
        source = getattr(req, "func_tool", None)
        try:
            local = copy.copy(source) if source is not None else ToolSet()
            tools = []
            for original in list(getattr(source, "tools", [])) if source is not None else []:
                try:
                    tools.append(copy.copy(original))
                except Exception as exc:
                    self._log_error("could not copy per-request tool; disabling it", exc)
        except Exception as exc:
            self._log_error("could not copy per-request tool set; disabling tools", exc)
            req.func_tool = None
            return
        guarded = []
        for tool in tools:
            name = str(getattr(tool, "name", ""))
            if isinstance(tool, HandoffTool):
                continue
            handler = getattr(tool, "handler", None)
            try:
                if handler is not None:
                    tool.handler = self._guard_handler(handler, name, event, context, uid, token)
                elif self._has_call_override(tool):
                    original_call = tool.call

                    async def guarded_call(run_context, __call=original_call, __name=name, **kwargs):
                        return await self._execute_guarded_tool(
                            __name, event, context, uid, token,
                            lambda: __call(run_context, **kwargs),
                        )
                    tool.call = guarded_call
                elif callable(getattr(tool, "run", None)):
                    original_run = tool.run

                    async def guarded_run(*args, __run=original_run, __name=name, **kwargs):
                        return await self._execute_guarded_tool(
                            __name, event, context, uid, token,
                            lambda: __run(*args, **kwargs),
                        )
                    tool.run = guarded_run
                else:
                    # No local handler, call override, or run function means the
                    # request-local tool has no guardable execution path.
                    continue
            except Exception as exc:
                self._log_error("could not guard per-request tool; disabling it", exc)
                continue
            guarded.append(tool)
        try:
            local.tools = []
            local.tools = guarded
            req.func_tool = local
        except Exception as exc:
            self._log_error("could not replace per-request tools; disabling tools", exc)
            req.func_tool = None

        # Add the only two supported mutation tools to this request copy. They are
        # never registered into or mutated on the shared tool manager.
        self._add_mutation_tools(req, event, context, uid, token)

    @staticmethod
    def _has_call_override(tool: Any) -> bool:
        return any("call" in base.__dict__ and base.__dict__["call"] is not AstrFunctionTool.call
                   for base in type(tool).mro())

    def _tool_is_blocked(self, event: AstrMessageEvent, context: dict[str, Any], uid: str,
                         token: dict[str, int] | None) -> bool:
        current = self.runtime.peek_policy(context, uid)
        return (bool(event.get_extra("agent_stop_requested")) or current["mode"] in {"silence", "shield"} or
                (token is not None and any(token.get(field) != current.get(field)
                                           for field in ("target_revision", "shield_epoch"))))

    def _cancel_tool_execution(self, event: AstrMessageEvent, context: dict[str, Any], uid: str,
                               token: dict[str, int] | None, reason: str):
        current = self.runtime.peek_policy(context, uid)
        event.set_extra("_autonomous_blocking_tool_abort_v1", {
            "reason": reason,
            "event_id": BlockingRuntime._event_id(event),
            "owner_revision": current["owner_revision"],
            "target_revision": current["target_revision"],
            "shield_epoch": current["shield_epoch"],
            "request_target_revision": token.get("target_revision") if token else None,
        })
        raise asyncio.CancelledError("autonomous blocking cancelled this tool request")

    def _check_tool_actor(self, event: AstrMessageEvent, context: dict[str, Any], uid: str,
                          token: dict[str, int] | None) -> None:
        if self._tool_is_blocked(event, context, uid, token):
            self._cancel_tool_execution(event, context, uid, token, "actor_blocked_or_request_token_stale")

    def _record_send_denial(self, event: AstrMessageEvent, context: dict[str, Any], uid: str, reason: str) -> None:
        details = {
            "reason": reason,
            "event_id": BlockingRuntime._event_id(event),
        }
        try:
            current = self.runtime.peek_policy(context, uid)
            details.update({
                "owner_revision": current["owner_revision"],
                "target_revision": current["target_revision"],
                "shield_epoch": current["shield_epoch"],
            })
        except Exception:
            pass
        event.set_extra("_autonomous_blocking_send_denial_v1", details)

    def _guard_tool_output(self, name: str, result: Any, context: dict[str, Any]) -> Any:
        shield_active = any(rule.get("mode") == "shield"
                            for rule in self.runtime.policy_snapshot(context).get("rules", []))
        if isinstance(result, dict):
            if self._looks_like_retrieval(name) and shield_active:
                source_keys = {"author_uid", "source_uids", "provenance_complete", "subject_id", "source_id"}
                if source_keys.intersection(result):
                    filtered = self.filter_frames(context, [result], purpose="tool_result")
                    return filtered[0] if filtered else {}
                if not {"frames", "context_items", "history", "retrieval", "materials"}.intersection(result):
                    raise PolicyDenied("opaque retrieval output cannot be provenance-filtered")
                return self.filter_material(context, result, purpose="tool_result")
            return self.filter_material(context, result, purpose="tool_result")
        if isinstance(result, list) and all(isinstance(item, dict) for item in result):
            return self.filter_frames(context, result, purpose="tool_result")
        if isinstance(result, list) and shield_active and self._looks_like_retrieval(name):
            raise PolicyDenied("opaque retrieval output cannot be provenance-filtered")
        if shield_active and self._looks_like_retrieval(name):
            raise PolicyDenied("opaque retrieval output cannot be provenance-filtered")
        return result

    def _guard_handler(self, handler, name, event, context, uid, token):
        if inspect.isasyncgenfunction(handler):
            async def guarded(*args, **kwargs):
                self._check_tool_actor(event, context, uid, token)
                output = []
                size = 0
                async for item in handler(*args, **kwargs):
                    if len(output) >= 256:
                        self._cancel_tool_execution(event, context, uid, token, "tool_output_item_limit")
                    size += len(repr(item).encode("utf-8", "replace"))
                    if size > 8 * 1024 * 1024:
                        self._cancel_tool_execution(event, context, uid, token, "tool_output_size_limit")
                    output.append(item)
                self._check_tool_actor(event, context, uid, token)
                for item in output:
                    try:
                        yield self._guard_tool_output(name, item, context)
                    except PolicyDenied:
                        yield self._withhold_tool_output(event, context, uid, token, name)
                        return
            return guarded

        async def guarded(*args, **kwargs):
            self._check_tool_actor(event, context, uid, token)
            value = handler(*args, **kwargs)
            result = await value if inspect.isawaitable(value) else value
            self._check_tool_actor(event, context, uid, token)
            try:
                return self._guard_tool_output(name, result, context)
            except PolicyDenied:
                return self._withhold_tool_output(event, context, uid, token, name)
        return guarded

    async def _execute_guarded_tool(self, name, event, context, uid, token, call):
        self._check_tool_actor(event, context, uid, token)
        result = call()
        result = await result if inspect.isawaitable(result) else result
        self._check_tool_actor(event, context, uid, token)
        try:
            return self._guard_tool_output(name, result, context)
        except PolicyDenied:
            return self._withhold_tool_output(event, context, uid, token, name)

    def _withhold_tool_output(self, event: AstrMessageEvent, context: dict[str, Any], uid: str,
                              token: dict[str, int] | None, name: str) -> str:
        event.set_extra("_autonomous_blocking_tool_withheld_v1", {
            "reason": "unfilterable_tool_output",
            "tool": name,
            "event_id": BlockingRuntime._event_id(event),
        })
        logger = getattr(self, "logger", None)
        if logger:
            logger.info("withheld unfilterable output of tool %s", name)
        return WITHHELD_TOOL_OUTPUT

    def _add_mutation_tools(self, req: Any, event: AstrMessageEvent, context: dict[str, Any], uid: str,
                            token: dict[str, int] | None = None) -> None:
        if event.get_extra("_narrative_internal_v1"):
            return
        local = getattr(req, "func_tool", None)
        if local is None or not hasattr(local, "add_tool"):
            return

        async def set_boundary(event_arg, mode, duration, scope=None, target_user_id="", reason=""):
            """Set a boundary for the current message author.

            Args:
                mode(string): silence or shield.
                duration(string): Duration such as 30m, 2h, or permanent.
                scope(string): bot or session.
                target_user_id(string): Optional UID; it must equal the current author.
                reason(string): Optional subjective audit note.
            """
            try:
                self._check_tool_actor(event_arg, context, uid, token)
                if str(event_arg.get_sender_id() or "") in self.protected_user_ids:
                    raise PolicyDenied("current user is protected")
                await self.runtime.set_interaction_boundary(event_arg, mode, duration,
                                                            scope or self.default_scope,
                                                            target_user_id, reason,
                                                            expected_token=token)
            except Exception as exc:
                return f"Boundary was not changed: {type(exc).__name__}."
            self._cancel_tool_execution(event_arg, context, uid, token, "boundary_set")

        async def clear_boundary(event_arg, target_user_id, scope="all", reason=""):
            """Clear existing boundaries owned by this bot for one user.

            Args:
                target_user_id(string): User UID whose existing boundaries should be cleared.
                scope(string): all, bot, or session.
                reason(string): Optional subjective audit note.
            """
            try:
                self._check_tool_actor(event_arg, context, uid, token)
                receipt = await self.runtime.clear_interaction_boundary(
                    event_arg, target_user_id, scope, reason, expected_token=token
                )
                if not receipt["changed"]:
                    return "No existing boundary matched; no rule was added."
                effective = receipt["effective"]
                remaining = effective.get("mode") or "none"
                return f"Clear committed. Remaining effective mode: {remaining}."
            except Exception as exc:
                return f"Boundary was not changed: {type(exc).__name__}."

        definitions = [
            FunctionTool(
                name="set_interaction_boundary",
                description="Restrict the current message author only.",
                parameters={"type": "object", "properties": {
                    "mode": {"type": "string", "enum": ["silence", "shield"]},
                    "duration": {"type": "string"}, "scope": {"type": "string", "enum": ["bot", "session"]},
                    "target_user_id": {"type": "string"}, "reason": {"type": "string"},
                }, "required": ["mode", "duration"]}, handler=set_boundary,
            ),
            FunctionTool(
                name="clear_interaction_boundary",
                description="Clear an existing boundary owned by this bot.",
                parameters={"type": "object", "properties": {
                    "target_user_id": {"type": "string"},
                    "scope": {"type": "string", "enum": ["all", "bot", "session"]},
                    "reason": {"type": "string"},
                }, "required": ["target_user_id"]}, handler=clear_boundary,
            ),
        ]
        for tool in definitions:
            local.add_tool(tool)

    def _install_event_guards(self, event: AstrMessageEvent, context: dict[str, Any], uid: str, policy: dict[str, Any]):
        if getattr(event, "_ablock_retired", False):
            raise PolicyDenied("event guard has been retired")
        runtime_id = id(self.runtime)
        existing_runtime_id = getattr(event, "_ablock_guard_runtime_id", None)
        if existing_runtime_id == runtime_id and self._event_has_current_guards(event):
            return
        if existing_runtime_id is not None and existing_runtime_id != runtime_id:
            raise PolicyDenied("event belongs to another blocking runtime generation")
        originals = []
        original_send = event.send
        original_stream = event.send_streaming
        snapshot = self.runtime.policy_snapshot(context)
        tokens = copy.deepcopy(snapshot.get("targets", {}))
        arrival_owner_revision = snapshot.get("owner_revision", 0)
        arrival_blocked = {
            str(rule["target_uid"])
            for rule in snapshot.get("rules", [])
            if rule.get("mode") in {"silence", "shield"}
        }
        tokens.setdefault(uid, {"target_revision": policy["target_revision"], "shield_epoch": policy["shield_epoch"]})
        event._ablock_target_snapshot = tokens
        event._ablock_arrival_blocked = arrival_blocked

        async def send_if_allowed(original, payload, *, streaming=False, use_fallback=False,
                                  extra_targets=None, unknown_reply_hint=False):
            if getattr(event, "_ablock_retired", False):
                return False
            if event.get_extra("_ablock_control_authorized"):
                if streaming:
                    return await original(payload, use_fallback=use_fallback)
                return await original(payload)
            if event.get_extra("_ablock_at_arrival_mode"):
                return False
            try:
                # The lease lock protects the author plus all addressed users
                # through the actual platform await. A CD outer lease is reentrant
                # only on this exact runtime, owner, and asyncio task.
                async with self.runtime.send_lease(context, uid, tokens.get(uid)):
                    targets, has_unknown_reply = self._outbound_targets(event, payload)
                    targets.update(extra_targets or ())
                    has_unknown_reply = has_unknown_reply or unknown_reply_hint
                    if has_unknown_reply and self.runtime.policy_snapshot(context).get("active"):
                        self._record_send_denial(event, context, uid, "unknown_reply_target")
                        return False
                    for target in targets:
                        if target == context["owner"]["self_id"]:
                            continue
                        if target in arrival_blocked:
                            self._record_send_denial(event, context, uid, "recipient_blocked_at_arrival")
                            return False
                        captured = tokens.get(target, {"target_revision": 0, "shield_epoch": 0})
                        target_policy = self.runtime.peek_policy(context, target)
                        if target in tokens and any(captured.get(field) != target_policy.get(field)
                                                     for field in ("target_revision", "shield_epoch")):
                            self._record_send_denial(event, context, uid, "recipient_token_stale")
                            return False
                        if target not in tokens and self.runtime.policy_snapshot(context).get("owner_revision") != arrival_owner_revision:
                            self._record_send_denial(event, context, uid, "owner_revision_changed")
                            return False
                        if not target_policy["reply_allowed"]:
                            self._record_send_denial(event, context, uid, "recipient_policy_blocked")
                            return False
                    if streaming:
                        return await original(payload, use_fallback=use_fallback)
                    return await original(payload)
            except PolicyDenied:
                self._record_send_denial(event, context, uid, "author_policy_or_token_denied")
                return False

        async def guarded_send(message):
            return await send_if_allowed(original_send, message)

        async def guarded_stream(generator, use_fallback=False):
            if getattr(event, "_ablock_retired", False):
                return False
            if event.get_extra("_ablock_at_arrival_mode"):
                return False
            buffered = []
            size = 0
            try:
                async for chain in generator:
                    if len(buffered) >= 4096:
                        self._record_send_denial(event, context, uid, "stream_buffer_item_limit")
                        return False
                    frozen = copy.deepcopy(chain)
                    size += len(repr(frozen).encode("utf-8", "replace"))
                    if size > 8 * 1024 * 1024:
                        self._record_send_denial(event, context, uid, "stream_buffer_size_limit")
                        return False
                    buffered.append(frozen)
                addressed = set()
                unknown_reply = False
                for chunk in buffered:
                    chunk_targets, chunk_unknown = self._outbound_targets(event, chunk)
                    addressed.update(chunk_targets)
                    unknown_reply = unknown_reply or chunk_unknown
                async def replay():
                    for chunk in buffered:
                        yield chunk
                return await send_if_allowed(original_stream, replay(), streaming=True, use_fallback=use_fallback,
                                             extra_targets=addressed, unknown_reply_hint=unknown_reply)
            except Exception as exc:
                self._log_error("stream buffering failed closed", exc)
                self._record_send_denial(event, context, uid, "stream_buffer_error")
                return False

        event.send = guarded_send
        event.send_streaming = guarded_stream
        originals.extend([("send", original_send, guarded_send), ("send_streaming", original_stream, guarded_stream)])
        event._ablock_original_methods = originals
        event._ablock_guard_runtime_id = runtime_id
        try:
            self._event_refs = [ref for ref in self._event_refs if ref() is not None]
            self._event_refs.append(weakref.ref(event))
        except TypeError:
            pass

    def _ensure_event_guards(self, event: AstrMessageEvent, context: dict[str, Any], uid: str,
                             policy: dict[str, Any]) -> None:
        if getattr(event, "_ablock_retired", False):
            raise PolicyDenied("event guard has been retired")
        runtime_id = id(self.runtime)
        existing = getattr(event, "_ablock_guard_runtime_id", None)
        if existing == runtime_id and self._event_has_current_guards(event):
            return
        if existing is not None and existing != runtime_id:
            raise PolicyDenied("event belongs to another blocking runtime generation")
        self._record_inbound(context, event, uid)
        self._install_event_guards(event, context, uid, policy)

    @staticmethod
    def _event_has_current_guards(event: AstrMessageEvent) -> bool:
        methods = {name: wrapper for name, _, wrapper in getattr(event, "_ablock_original_methods", [])}
        return bool(methods) and all(getattr(event, name, None) is wrapper for name, wrapper in methods.items())

    def _record_inbound(self, context: dict[str, Any], event: AstrMessageEvent, uid: str) -> None:
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or
                         getattr(event, "event_id", "") or "").strip()
        if not message_id:
            return
        owner = context["owner"]
        key = (owner["platform_instance_id"], owner["self_id"], context["umo"])
        ledger = self._message_authors.get(key)
        if ledger is None:
            ledger = self._message_authors[key] = OrderedDict()
        if isinstance(self._message_authors, OrderedDict):
            self._message_authors.move_to_end(key)
            while len(self._message_authors) > 128:
                self._message_authors.popitem(last=False)
        ledger[message_id] = uid
        ledger.move_to_end(message_id)
        while len(ledger) > 256:
            ledger.popitem(last=False)

    def _sanitize_inbound_references(self, event: AstrMessageEvent, context: dict[str, Any]) -> None:
        snapshot = self.runtime.policy_snapshot(context)
        shielded = {
            str(rule["target_uid"])
            for rule in snapshot.get("rules", [])
            if rule.get("mode") == "shield"
        }
        if not shielded:
            return
        messages = list(event.get_messages() or [])
        clean_messages = []
        removed_text = []
        ledger_key = (context["owner"]["platform_instance_id"], context["owner"]["self_id"], context["umo"])
        ledger = self._message_authors.get(ledger_key, {})
        for component in messages:
            kind = str(getattr(getattr(component, "type", None), "value", getattr(component, "type", ""))).lower()
            if kind == "reply" or (hasattr(component, "sender_id") and hasattr(component, "message_str")):
                author = str(getattr(component, "sender_id", "") or "").strip()
                if author in {"0", "None"}:
                    author = ledger.get(str(getattr(component, "id", "") or ""), "")
                if not author or author in shielded:
                    quoted = str(getattr(component, "message_str", "") or "")
                    if quoted:
                        removed_text.append(quoted)
                    continue
            if kind == "forward":
                # Native forwarded bundles do not carry verifiable per-frame
                # authors consistently across adapters, so omit them while a
                # shield policy is active.
                continue
            clean_messages.append(component)
        if len(clean_messages) == len(messages):
            return
        try:
            event.message_obj.message = clean_messages
        except Exception:
            pass
        # Native event.message_str is the prompt source on several adapters.
        # Remove only the exact quoted fragment supplied by the Reply component.
        current = str(getattr(event, "message_str", "") or "")
        for quoted in removed_text:
            current = current.replace(quoted, "")
        if current != getattr(event, "message_str", None):
            event.message_str = current
        event.set_extra("_ablock_reference_material_removed", True)

    def _outbound_targets(self, event: AstrMessageEvent, payload: Any) -> tuple[set[str], bool]:
        chain = getattr(payload, "chain", payload)
        if not isinstance(chain, (list, tuple)):
            return set(), False
        context = self.resolve_context(event)
        owner = context.get("owner") or {}
        key = (owner.get("platform_instance_id", ""), owner.get("self_id", ""), context.get("umo", ""))
        ledger = self._message_authors.get(key, {})
        targets: set[str] = set()
        unknown_reply = False
        for component in chain:
            kind = str(getattr(getattr(component, "type", None), "value", getattr(component, "type", "")))
            for attribute in ("qq", "user_id", "target_id", "receiver_id"):
                value = getattr(component, attribute, None)
                if value is not None and str(value).strip():
                    targets.add(str(value).strip())
            if kind.lower() == "reply" or (hasattr(component, "id") and hasattr(component, "sender_id")):
                sender = str(getattr(component, "sender_id", "") or "").strip()
                if sender and sender not in {"0", "None"}:
                    targets.add(sender)
                else:
                    reply_id = str(getattr(component, "id", "") or "").strip()
                    known = ledger.get(reply_id)
                    if known:
                        targets.add(known)
                    else:
                        unknown_reply = True
        return targets, unknown_reply

    async def _handle_admin_command(self, event: AstrMessageEvent, command: list[str]):
        context = self.resolve_context(event)
        if not context["ready"]:
            await self._control_result(event, "Policy owner identity is unavailable.")
            return
        owner = context["owner"]
        try:
            if not command:
                raise ValueError("Usage: /ablock list|status [uid]|set <uid> <silence|shield> <duration> [bot|session] [reason...]|remove <uid> [all|bot|session]")
            action = command[0]
            if action == "list" and len(command) == 1:
                rows = self._store.list_rules(owner, limit=1000, include_inactive=False)
                now = datetime.now(timezone.utc)
                rows = [row for row in rows if not row["expires_at"] or
                        datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) > now]
                lines = [f"{row['target_uid']} | {row['mode']} | {row['expires_at'] or 'permanent'}" for row in rows]
                text = "Active rules:\n" + ("\n".join(lines) if lines else "(none)")
            elif action == "status" and len(command) in {1, 2}:
                target = command[1] if len(command) == 2 else None
                rows = self._store.list_rules(owner, limit=1000, include_inactive=False)
                active = [row for row in rows if (target is None or row["target_uid"] == target) and
                          (not row["expires_at"] or datetime.fromisoformat(
                              row["expires_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc))]
                text = f"Active rules: {len(active)}" + (f" for {target}" if target else "")
            elif action == "set" and len(command) >= 4:
                _, uid, mode, duration, *rest = command
                scope = rest.pop(0) if rest and rest[0] in {"bot", "session"} else self.default_scope
                reason = " ".join(rest)
                if mode not in {"silence", "shield"} or scope not in {"bot", "session"}:
                    raise ValueError("usage: /ablock set <uid> <silence|shield> <duration> <bot|session>")
                permanent, seconds = parse_duration(duration)
                if uid in self.protected_user_ids:
                    raise ValueError("target is protected")
                async with self.runtime._owner_lock(owner):
                    receipt = self._store.set_rule(owner, uid, mode, scope_type=scope, umo=context["umo"],
                                                   duration_seconds=seconds, permanent=permanent,
                                                   reason=reason, actor_kind="admin",
                                                   actor_uid=str(event.get_sender_id()),
                                                   event_id=BlockingRuntime._event_id(event))
                text = f"Rule {'set' if receipt['changed'] else 'unchanged'}: {uid} | {mode} | {receipt['effective']['expires_at'] or 'permanent'}"
            elif action == "remove" and len(command) in {2, 3}:
                _, uid, *rest = command
                scope = rest[0] if rest else "all"
                if scope not in {"all", "bot", "session"}:
                    raise ValueError("scope must be all, bot, or session")
                async with self.runtime._owner_lock(owner):
                    receipt = self._store.clear_rule(owner, uid, scope_type=scope, umo=context["umo"],
                                                     reason="admin command", actor_kind="admin",
                                                     actor_uid=str(event.get_sender_id()),
                                                     event_id=BlockingRuntime._event_id(event))
                text = f"Existing rule {'removed' if receipt['changed'] else 'not found'}: {uid}"
            else:
                text = "Usage: /ablock list|status|set <uid> <silence|shield> <duration> [bot|session]|remove <uid> [all|bot|session]"
        except Exception as exc:
            text = f"Command failed: {type(exc).__name__}: {exc}"
        event.set_extra("_ablock_control_authorized", True)
        await self._control_result(event, text)

    async def _control_result(self, event: AstrMessageEvent, text: str):
        event.set_extra("_ablock_control_authorized", True)
        await event.send(MessageChain().message(text))
        event.stop_event()

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @staticmethod
    def _parse_admin_command(message: str) -> list[str] | None:
        parts = str(message or "").strip().split()
        if not parts or parts[0] != "/ablock":
            return None
        return parts[1:]

    @staticmethod
    def _looks_like_retrieval(name: str) -> bool:
        lower = name.lower()
        return any(marker in lower for marker in ("search", "retriev", "memory", "history", "knowledge", "mcp"))

    def _log_error(self, message: str, error: Exception):
        logger = getattr(self, "logger", None)
        if logger:
            logger.warning("%s: %s", message, error)
