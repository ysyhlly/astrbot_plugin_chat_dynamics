"""Reversible AstrBot 4.28.2 native builder bridge for provider-only history views."""
from __future__ import annotations

import copy
import asyncio
import functools
import hashlib
import inspect
import json
import logging
import math
import sys
import weakref
from collections.abc import Mapping

logger = logging.getLogger(__name__)


class _ResetDelegate:
    """Await reset before decorating its runner, while forwarding close()."""

    def __init__(self, reset_coro, after_reset):
        self._reset_coro = reset_coro
        self._after_reset = after_reset
        self._awaited = False

    def __await__(self):
        async def run():
            self._awaited = True
            await self._reset_coro
            self._after_reset()
        return run().__await__()

    def close(self):
        close = getattr(self._reset_coro, "close", None)
        if callable(close):
            close()


def _message_dict(message):
    if isinstance(message, dict):
        return copy.deepcopy(message)
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        try:
            result = dump()
            return copy.deepcopy(result) if isinstance(result, dict) else None
        except Exception:
            return None
    return None


def _native_scope_key(scope):
    """Return the public v1 scope fingerprint used by both memory plugins."""
    if not isinstance(scope, dict):
        return ""
    keys = ("platform", "umo", "persona_id", "instance_id")
    values = {key: scope.get(key) for key in keys}
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        return ""
    encoded = json.dumps(
        {key: value.strip() for key, value in values.items()},
        sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")
    return "narrative:v1:" + hashlib.sha256(encoded).hexdigest()


def _make_native_memory_validator(bundle, callback, request, event):
    """Bind the LM callback to the immutable request/source identity captured here."""
    try:
        frozen_bundle = copy.deepcopy(bundle)
    except Exception:
        frozen_bundle = None
    try:
        frozen_fingerprint = hashlib.sha256(json.dumps(
            frozen_bundle, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
    except Exception:
        frozen_fingerprint = None

    def identity_is_current():
        if frozen_fingerprint is None or frozen_bundle is None:
            return False
        try:
            current_bundle = getattr(request, "_lm_native_memory_v1", None)
            current_fingerprint = hashlib.sha256(json.dumps(
                current_bundle, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest()
        except Exception:
            return False
        return (current_fingerprint == frozen_fingerprint
                and _native_bundle_identity(frozen_bundle, request, event))

    async def validate():
        if not callable(callback) or not identity_is_current():
            return False
        try:
            result = callback()
            if not inspect.isawaitable(result):
                return False
            result = await result
        except Exception:
            return False
        return result is True and identity_is_current()

    return validate


def _json_value(value):
    """Convert an immutable JSON receipt to a fresh canonical JSON shape."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non_finite_receipt_number")
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in result:
                raise ValueError("invalid_receipt_key")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise ValueError("non_json_receipt_value")


def _json_fingerprint(value):
    try:
        normalized = _json_value(value)
        encoded = json.dumps(
            normalized, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return normalized, hashlib.sha256(encoded).hexdigest()
    except Exception:
        return None, None


class _ToolMemorySourceRegistry:
    """Private, bounded registry for memory-tool sources selected mid-request."""

    _MAX_VALIDATORS = 32
    _TOOL_MODULES = frozenset({
        # Unit-test namespace and the exact AstrBot plugin loader namespace.
        "astrbot_plugin_livingmemory.core.tools.memory_search_tool",
        "data.plugins.astrbot_plugin_livingmemory.core.tools.memory_search_tool",
    })
    _PERMISSION_WRAPPER_MODULE = "astrbot.core.provider.func_tool_manager"
    _PERMISSION_WRAPPER_NAME = "_PermissionGuardedTool"
    _TOOL_NAME = "recall_long_term_memory"

    def __init__(self, runner, request, event):
        self._runner = runner
        self._request = request
        self._event = event
        self._runner_req_attr_present = hasattr(runner, "req")
        self._run_context = getattr(runner, "run_context", None)
        self._tool_schema_mode = getattr(runner, "tool_schema_mode", None)
        self._request_tool_set = getattr(request, "func_tool", None)
        self._dispatch_tool_set = self._current_dispatch_tool_set()
        self._permission_manager = None
        binding_ok, dispatch_tool = self._get_registered_tool(self._dispatch_tool_set)
        owner_ok, source_tool, permission_manager = self._resolve_source_owner(dispatch_tool)
        self._permission_manager = permission_manager
        self._binding_ok = (
            binding_ok and owner_ok
            and (self._runner_req_attr_present or dispatch_tool is None)
            and (not self._runner_req_attr_present or runner.req is request)
            and ((self._run_context is None and dispatch_tool is None)
                 or (self._run_context is not None
                     and getattr(getattr(self._run_context, "context", None), "event", None) is event))
        )
        self._bound_dispatch_tool = dispatch_tool if self._binding_ok else None
        self._source_tool = source_tool if self._binding_ok else None
        self._validators = ()
        self._validator_ids = ()
        self._sealed = False
        self._active = None
        self._failed = False
        self._registrar = self.register

    def _current_dispatch_tool_set(self):
        # AstrBot's skills_like mode replaces req.func_tool with schema-only
        # clones, while its real executor deliberately dispatches from the
        # preserved raw set. All other modes dispatch from req.func_tool.
        if getattr(self._runner, "tool_schema_mode", None) == "skills_like":
            return getattr(self._runner, "_skill_like_raw_tool_set", None)
        return getattr(self._request, "func_tool", None)

    def _get_registered_tool(self, tool_set):
        if tool_set is None:
            return True, None
        get_tool = getattr(tool_set, "get_tool", None)
        if not callable(get_tool):
            return False, None
        try:
            return True, get_tool(self._TOOL_NAME)
        except Exception:
            return False, None

    def _raw_source_tool_is_trusted(self, tool):
        return (tool is not None
                and type(tool).__module__ in self._TOOL_MODULES
                and type(tool).__name__ == "MemorySearchTool"
                and getattr(tool, "name", None) == self._TOOL_NAME
                and getattr(tool, "active", None) is True
                and getattr(tool, "memory_engine", None) is not None
                and getattr(tool, "context", None) is not None
                and getattr(tool, "config_manager", None) is not None)

    def _resolve_source_owner(self, dispatch_tool):
        if dispatch_tool is None:
            return True, None, None
        if self._raw_source_tool_is_trusted(dispatch_tool):
            return True, dispatch_tool, None
        sdk_module = sys.modules.get(self._PERMISSION_WRAPPER_MODULE)
        expected_wrapper = getattr(sdk_module, self._PERMISSION_WRAPPER_NAME, None)
        expected_manager = getattr(sdk_module, "FunctionToolManager", None)
        if (type(dispatch_tool).__module__ != self._PERMISSION_WRAPPER_MODULE
                or type(dispatch_tool).__name__ != self._PERMISSION_WRAPPER_NAME
                or expected_wrapper is None or type(dispatch_tool) is not expected_wrapper
                or getattr(dispatch_tool, "name", None) != self._TOOL_NAME
                or getattr(dispatch_tool, "active", None) is not True):
            return False, None, None
        owner = getattr(dispatch_tool, "_wrapped", None)
        manager = getattr(dispatch_tool, "_mgr", None)
        get_func = getattr(manager, "get_func", None)
        if (not self._raw_source_tool_is_trusted(owner)
                or expected_manager is None or type(manager) is not expected_manager
                or not callable(get_func)):
            return False, None, None
        if self._permission_manager is not None and manager is not self._permission_manager:
            return False, None, None
        try:
            if get_func(self._TOOL_NAME) is not owner:
                return False, None, None
        except Exception:
            return False, None, None
        return True, owner, manager

    def _binding_is_intact(self):
        runner = self._runner
        request = self._request
        event = self._event
        if (not self._binding_ok
                or hasattr(runner, "req") != self._runner_req_attr_present
                or (self._runner_req_attr_present and runner.req is not request)
                or getattr(runner, "run_context", None) is not self._run_context
                or (self._run_context is None and self._bound_dispatch_tool is not None)
                or getattr(request, "func_tool", None) is not self._request_tool_set
                or getattr(runner, "tool_schema_mode", None) != self._tool_schema_mode
                or getattr(runner, "_cd_tool_memory_sources_registry_v1", None) is not self
                or getattr(event, "_cd_tool_memory_sources_register_v1", None) is not self._registrar
                or getattr(event, "_cd_owned_original_input_v1", False) is not True
                or (self._run_context is not None
                    and getattr(getattr(self._run_context, "context", None), "event", None) is not event)):
            return False
        current_set = self._current_dispatch_tool_set()
        if current_set is not self._dispatch_tool_set:
            return False
        valid, current_dispatch_tool = self._get_registered_tool(current_set)
        if not valid or current_dispatch_tool is not self._bound_dispatch_tool:
            return False
        owner_ok, current_owner, current_manager = self._resolve_source_owner(current_dispatch_tool)
        return (owner_ok and current_owner is self._source_tool
                and current_manager is self._permission_manager)

    def _trusted_tool(self, tool, run_context):
        owner_ok, owner, permission_manager = self._resolve_source_owner(tool)
        return (self._binding_is_intact()
                and self._bound_dispatch_tool is not None and tool is self._bound_dispatch_tool
                and owner_ok and owner is self._source_tool
                and permission_manager is self._permission_manager
                and (self._run_context is None or run_context is self._run_context)
                and getattr(getattr(run_context, "context", None), "event", None) is self._event)

    def _reject_begin(self, tool, reason):
        self._failed = True
        mode = self._tool_schema_mode
        mode_enum = mode if type(mode) is str and mode in {"full", "skills_like"} else "other"
        if tool is None:
            tool_kind = "none"
        elif (type(tool).__module__ == self._PERMISSION_WRAPPER_MODULE
              and type(tool).__name__ == self._PERMISSION_WRAPPER_NAME):
            tool_kind = "permission_wrapper"
        elif (type(tool).__module__ in self._TOOL_MODULES
              and type(tool).__name__ == "MemorySearchTool"):
            tool_kind = "raw"
        else:
            tool_kind = "other"
        reason_enum = reason if reason in {"sealed", "prior_failure", "active", "binding_invalid"} else "binding_invalid"
        logger.warning(
            "Native memory tool source rejected mode=%s tool_kind=%s reason=%s",
            mode_enum, tool_kind, reason_enum,
        )
        return False

    def begin_tool(self, tool, run_context):
        if getattr(tool, "name", None) != self._TOOL_NAME:
            return None
        if self._sealed:
            return self._reject_begin(tool, "sealed")
        if self._failed:
            return self._reject_begin(tool, "prior_failure")
        if self._active is not None:
            return self._reject_begin(tool, "active")
        if not self._trusted_tool(tool, run_context):
            return self._reject_begin(tool, "binding_invalid")
        self._active = (tool, run_context, self._source_tool)
        return True

    def end_tool(self, tool, run_context):
        if (self._active is None or self._active[0] is not tool
                or self._active[1] is not run_context):
            self._failed = True
            self._active = None
            return False
        self._active = None
        return True

    def register(self, validator, *, tool=None):
        active = self._active
        if self._sealed:
            return False
        if (self._failed or active is None
                or tool is not active[2] or not self._trusted_tool(active[0], active[1])
                or not callable(validator)):
            self._failed = True
            return False
        if any(existing is validator for existing in self._validators):
            return True
        if len(self._validators) >= self._MAX_VALIDATORS:
            self._failed = True
            return False
        self._validators = (*self._validators, validator)
        self._validator_ids = (*self._validator_ids, id(validator))
        return True

    def seal(self):
        if self._active is not None:
            self._failed = True
        self._sealed = True

    def intact(self, request, event, runner):
        return (self._request is request and self._event is event and self._runner is runner
                and self._binding_is_intact()
                and not self._failed and self._active is None
                and type(self._sealed) is bool
                and isinstance(self._validators, tuple)
                and isinstance(self._validator_ids, tuple)
                and len(self._validators) <= self._MAX_VALIDATORS
                and len(self._validator_ids) == len(self._validators)
                and self._validator_ids == tuple(id(item) for item in self._validators)
                and all(callable(item) for item in self._validators))

    async def validate_all(self):
        if self._failed or self._active is not None or not isinstance(self._validators, tuple):
            return False
        # All registered sources share the caller's single deadline.
        for validator in self._validators:
            result = validator()
            if not inspect.isawaitable(result) or await result is not True:
                return False
        return True


def _make_native_sources_validator(request, event, runner, tool_registry):
    """Freeze and revalidate every selected native source for one owned request."""
    native_bundle = getattr(request, "_lm_native_memory_v1", None)
    native_callback = getattr(request, "_lm_native_memory_validate_v1", None)
    summary_sources = getattr(request, "_lm_private_summary_sources_v1", None)
    summary_callback = getattr(request, "_lm_private_summary_validate_v1", None)
    injection_getter = getattr(request, "_lm_private_injection_v1", None)
    persona_callback = getattr(request, "_cd_private_persona_current_v1", None)
    persona_required = getattr(event, "_chat_dynamics_owned_request", False) is True
    tool_registry_required = getattr(event, "_cd_owned_original_input_v1", False) is True
    persona_owner = getattr(persona_callback, "_cd_private_persona_owner_v1", None)
    native_present = native_bundle is not None
    summary_present = summary_sources is not None
    sources_present = native_present or summary_present

    frozen_native, native_fingerprint = _json_fingerprint(native_bundle) if native_present else (None, None)
    frozen_summary, summary_fingerprint = _json_fingerprint(summary_sources) if summary_present else (None, None)
    native_valid = _make_native_memory_validator(
        frozen_native, native_callback, request, event) if native_present else None
    identity_valid = ((not native_present or (frozen_native is not None and native_fingerprint is not None))
                      and (not summary_present or
                           (isinstance(frozen_summary, dict) and bool(frozen_summary)
                            and summary_fingerprint is not None and callable(summary_callback)))
                      and (not persona_required or
                           (callable(persona_callback) and isinstance(persona_owner, tuple)
                            and len(persona_owner) == 2 and persona_owner[0] is request
                            and persona_owner[1] is event)))

    def same_receipts():
        if getattr(request, "_lm_native_memory_validate_v1", None) is not native_callback:
            return False
        if getattr(request, "_lm_private_summary_validate_v1", None) is not summary_callback:
            return False
        if getattr(request, "_cd_private_persona_current_v1", None) is not persona_callback:
            return False
        if tool_registry_required and not tool_registry.intact(request, event, runner):
            return False
        if getattr(request, "_lm_private_injection_v1", None) is not injection_getter:
            return False
        if persona_required:
            current_owner = getattr(persona_callback, "_cd_private_persona_owner_v1", None)
            if (not isinstance(current_owner, tuple) or len(current_owner) != 2
                    or current_owner[0] is not request or current_owner[1] is not event):
                return False
        if native_present:
            current_native, current_fingerprint = _json_fingerprint(
                getattr(request, "_lm_native_memory_v1", None))
            if current_fingerprint != native_fingerprint or current_native != frozen_native:
                return False
            if not isinstance(frozen_native, dict) or not _native_bundle_identity(frozen_native, request, event):
                return False
        elif getattr(request, "_lm_native_memory_v1", None) is not None:
            return False
        if summary_present:
            current_summary, current_fingerprint = _json_fingerprint(
                getattr(request, "_lm_private_summary_sources_v1", None))
            if current_fingerprint != summary_fingerprint or current_summary != frozen_summary:
                return False
        elif getattr(request, "_lm_private_summary_sources_v1", None) is not None:
            return False
        return True

    def injection_is_safe():
        if not callable(injection_getter):
            return True
        try:
            receipt = injection_getter(request, event)
        except Exception:
            return False
        if inspect.isawaitable(receipt):
            close = getattr(receipt, "close", None)
            if callable(close):
                close()
            return False
        if receipt is None:
            return True
        if not isinstance(receipt, Mapping) or type(receipt.get("schema_version")) is not int \
                or receipt.get("schema_version") != 1:
            return False
        segments = receipt.get("segments")
        if not isinstance(segments, (tuple, list)):
            return False
        # A receipt that says content reached the prompt must be backed by at
        # least one frozen source validator; summary text is not a quote proof.
        return not segments or sources_present

    failure_state = {'stage': 'identity', 'logged': False}

    def mark_invalidated(reason='source_rejected'):
        try:
            setattr(event, "_cd_native_memory_source_invalidated_v1", True)
            setattr(event, "_cd_native_memory_failure_reason_v1", reason)
        except Exception:
            pass
        if not failure_state['logged']:
            failure_state['logged'] = True
            logger.warning('Native memory validation failed reason=%s stage=%s native=%s summary=%s tool_registry=%s',
                reason, failure_state['stage'], native_present, summary_present, tool_registry_required)
        sink = getattr(event, "_cd_native_memory_source_failure_sink_v1", None)
        if callable(sink):
            try:
                sink()
            except Exception:
                pass

    async def validate():
        try:
            failure_state['stage'] = 'identity'
            if not identity_valid:
                mark_invalidated('source_identity_invalid')
                return False
            if not same_receipts():
                mark_invalidated('tool_binding_invalid' if tool_registry_required and not tool_registry.intact(request,event,runner)
                                 else 'receipt_identity_changed')
                return False
            if not injection_is_safe():
                mark_invalidated('injection_not_proven')
                return False
            if not sources_present and not persona_required and not tool_registry_required:
                return True
            async with asyncio.timeout(1.0):
                if persona_required:
                    failure_state['stage'] = 'persona_before'
                    result = persona_callback()
                    if not inspect.isawaitable(result) or await result is not True:
                        mark_invalidated('persona_not_current')
                        return False
                if native_present:
                    failure_state['stage'] = 'native_memory'
                    if native_valid is None or await native_valid() is not True:
                        mark_invalidated('native_sources_not_current')
                        return False
                if summary_present:
                    failure_state['stage'] = 'summary_memory'
                    result = summary_callback()
                    if not inspect.isawaitable(result) or await result is not True:
                        mark_invalidated('summary_sources_not_current')
                        return False
                if persona_required:
                    failure_state['stage'] = 'persona_after'
                    result = persona_callback()
                    if not inspect.isawaitable(result) or await result is not True:
                        mark_invalidated('persona_not_current')
                        return False
                if tool_registry_required:
                    failure_state['stage'] = 'tool_memory'
                    if not tool_registry.intact(request, event, runner):
                        mark_invalidated('tool_binding_invalid')
                        return False
                    if await tool_registry.validate_all() is not True:
                        mark_invalidated('tool_sources_not_current')
                        return False
                if not same_receipts() or not injection_is_safe():
                    failure_state['stage'] = 'final_identity'
                    mark_invalidated('receipt_identity_changed')
                    return False
            return True
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            mark_invalidated('validation_timeout')
            return False
        except Exception:
            mark_invalidated('validation_exception')
            return False

    return validate, (sources_present or callable(injection_getter)
                      or getattr(event, "_cd_owned_original_input_v1", False) is True)


def _install_native_memory_guard(runner, request, event, bundle):
    """Install a fail-closed async guard even if host history cannot be projected."""
    if getattr(runner, "_cd_native_memory_validation_v1", False):
        owner = getattr(runner, "_cd_native_memory_validator_owner_v1", None)
        valid_owner = (isinstance(owner, tuple) and len(owner) == 2
                       and owner[0] is request and owner[1] is event
                       and callable(getattr(runner, "_cd_native_memory_validator_v1", None)))
        if not valid_owner:
            return False
        capability = getattr(runner, "_cd_owned_memory_sources_capability_v1", None)
        base_ok = (callable(capability)
                   and getattr(event, "_cd_owned_memory_sources_validate_v1", None) is capability)
        registry = getattr(runner, "_cd_tool_memory_sources_registry_v1", None)
        if getattr(event, "_cd_owned_original_input_v1", False) is True:
            return (base_ok and isinstance(registry, _ToolMemorySourceRegistry)
                    and registry.intact(request, event, runner))
        return base_ok
    tool_registry = (_ToolMemorySourceRegistry(runner, request, event)
                     if getattr(event, "_cd_owned_original_input_v1", False) is True else None)
    validate, should_install = _make_native_sources_validator(request, event, runner, tool_registry)
    if not should_install:
        return False
    if should_install:
        frozen_validate = validate
        capability = None

        def mark_invalidated():
            try:
                setattr(event, "_cd_native_memory_source_invalidated_v1", True)
            except Exception:
                pass
            sink = getattr(event, "_cd_native_memory_source_failure_sink_v1", None)
            if callable(sink):
                try:
                    sink()
                except Exception:
                    pass

        async def guarded_validate():
            if (capability is None
                    or getattr(event, "_cd_owned_memory_sources_validate_v1", None) is not capability):
                mark_invalidated()
                return False
            owner = getattr(capability, "_cd_owned_memory_sources_owner_v1", None)
            if (not isinstance(owner, tuple) or len(owner) != 3
                    or owner[0] is not request or owner[1] is not event
                    or owner[2] is not frozen_validate):
                mark_invalidated()
                return False
            try:
                result = frozen_validate()
                if not inspect.isawaitable(result):
                    mark_invalidated()
                    return False
                return await result is True
            except asyncio.CancelledError:
                raise
            except Exception:
                mark_invalidated()
                return False

        async def guarded_capability(candidate_event):
            if candidate_event is not event:
                return False
            owner = getattr(guarded_capability, "_cd_owned_memory_sources_owner_v1", None)
            if (not isinstance(owner, tuple) or len(owner) != 3
                    or owner[0] is not request or owner[1] is not event
                    or owner[2] is not frozen_validate
                    or getattr(event, "_cd_owned_memory_sources_validate_v1", None) is not guarded_capability):
                mark_invalidated()
                return False
            return await guarded_validate() is True

        capability = guarded_capability
        capability._cd_owned_memory_sources_owner_v1 = (request, event, frozen_validate)
        try:
            setattr(event, "_cd_owned_memory_sources_validate_v1", capability)
        except Exception:
            mark_invalidated()
            return False
        validate = guarded_validate
    try:
        setattr(runner, "_cd_native_memory_validator_v1", validate)
        setattr(runner, "_cd_native_memory_validator_owner_v1", (request, event))
        setattr(runner, "_cd_owned_memory_sources_capability_v1", capability)
    except Exception:
        return False
    provider = getattr(runner, "provider", None)
    fallbacks = list(getattr(runner, "fallback_providers", ()) or ())
    if provider is None and not fallbacks:
        return False
    try:
        if provider is not None:
            runner.provider = _NativeMemoryValidationProvider(provider, validate, event)
        runner.fallback_providers = [
            _NativeMemoryValidationProvider(fallback, validate, event)
            for fallback in fallbacks
        ]
    except Exception:
        return False
    # A source may be revoked after the model call but before a tool or send.
    # Keep the same source and policy check at those effect boundaries.
    original_executor = getattr(runner, "tool_executor", None)
    tool_guard_present = (getattr(runner, "_cd_native_memory_tool_guard_v1", False)
                          or getattr(runner, "_cd_native_tool_guard_v1", False))
    if original_executor is not None and tool_guard_present and tool_registry is not None:
        current = original_executor
        registry_wrapped = False
        for _ in range(8):
            if getattr(current, "tool_source_registry", None) is tool_registry:
                registry_wrapped = True
                break
            current = getattr(current, "delegate", None)
            if current is None:
                break
        if not registry_wrapped:
            return False
    if original_executor is not None and not tool_guard_present:
        try:
            from .agent_bridge import OwnedToolExecutor

            async def tool_guard():
                if not await validate():
                    from .member_boundary import BoundaryCancelled
                    raise BoundaryCancelled("native_memory_source_invalidated")

            runner.tool_executor = OwnedToolExecutor(
                original_executor, guard=tool_guard, tool_source_registry=tool_registry)
            setattr(runner, "_cd_native_memory_tool_guard_v1", True)
        except Exception as exc:
            logger.info("Native memory tool guard unavailable type=%s", type(exc).__name__)
            return False
    original_send = getattr(event, "send", None)
    if callable(original_send) and not (
            getattr(event, "_cd_native_memory_send_guard_v1", False)
            or getattr(event, "_cd_native_send_guard_v1", False)):
        async def guarded_send(*args, **kwargs):
            if not await validate():
                from .member_boundary import BoundaryCancelled
                raise BoundaryCancelled("native_memory_source_invalidated")
            result = original_send(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        try:
            setattr(event, "send", guarded_send)
            setattr(event, "_cd_native_memory_send_guard_v1", True)
        except Exception as exc:
            logger.info("Native memory send guard unavailable type=%s", type(exc).__name__)
            return False
    if tool_registry is not None:
        try:
            setattr(runner, "_cd_tool_memory_sources_registry_v1", tool_registry)
            setattr(event, "_cd_tool_memory_sources_register_v1", tool_registry._registrar)
        except Exception:
            return False
    setattr(runner, "_cd_native_memory_validation_v1", True)
    return True


def _install_native_guard_after_reset(plugin, event, request, result, *, allow_history_projection):
    """Defer bundle guards until reset has finished replacing runner providers."""
    reset_coro = getattr(result, "reset_coro", None)
    if reset_coro is None:
        return _install_projection(
            plugin, event, request, result,
            allow_history_projection=allow_history_projection,
        )
    result.reset_coro = _ResetDelegate(
        reset_coro,
        lambda: _install_projection(
            plugin, event, request, result,
            allow_history_projection=(allow_history_projection
                                      and not getattr(plugin, "_shutting_down", False)),
        ),
    )
    return True


def _native_bundle_identity(bundle, request, event):
    """Check source-view identities against this native request receipt."""
    if not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int or bundle.get("schema_version") != 1:
        return False
    purpose = bundle.get("purpose")
    request_scope = bundle.get("request_scope")
    card_version = bundle.get("card_version")
    receipt = getattr(request, "_cd_memory_feedback_v1", None)
    if not isinstance(receipt, dict):
        return False
    receipt_scope = receipt.get("scope")
    if (purpose != "reply" or receipt.get("purpose") != purpose
            or not isinstance(card_version, str) or not card_version
            or receipt.get("card_version") != card_version
            or _native_scope_key(request_scope) != _native_scope_key(receipt_scope)
            or not _native_scope_key(request_scope)):
        return False
    umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
    if not umo or request_scope.get("umo") != umo:
        return False
    source_views = bundle.get("source_views")
    if not isinstance(source_views, list) or not source_views:
        return False
    parent_scope = request_scope
    source_keys = set()
    for row in source_views:
        if not isinstance(row, dict):
            return False
        scope = row.get("scope")
        view = row.get("evidence_view")
        if not isinstance(scope, dict) or not isinstance(view, dict):
            return False
        source_key = _native_scope_key(scope)
        if (not source_key or source_key in source_keys
                or scope.get("platform") != parent_scope.get("platform")
                or scope.get("persona_id") != parent_scope.get("persona_id")
                or scope.get("instance_id") != parent_scope.get("instance_id")
                or row.get("purpose") != purpose or row.get("card_version") != card_version
                or type(view.get("schema_version")) is not int or view.get("schema_version") != 1
                or view.get("available") is not True or view.get("scope_key") != source_key
                or view.get("purpose") != purpose or view.get("card_version") != card_version
                or type(view.get("correction_revision")) is not int
                or view.get("correction_revision") < 0
                or not isinstance(view.get("atoms"), list)
                or not isinstance(view.get("slots"), dict)
                or not isinstance(view.get("history_projection"), dict)
                or not isinstance(view.get("revocations"), list)):
            return False
        source_keys.add(source_key)
    return True


class _NativeMemoryValidationProvider:
    """Await source revalidation immediately before each provider request."""

    _source_failure_diagnostics_v1 = True

    def __init__(self, provider, validate, event=None):
        self.provider = provider
        self.validate = validate
        self.event = event

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def _check(self):
        try:
            valid = await self.validate()
        except Exception:
            valid = False
        if valid is not True:
            from .member_boundary import BoundaryCancelled
            raise BoundaryCancelled("native_memory_source_invalidated")

    async def text_chat(self, *args, **kwargs):
        await self._check()
        if self.event is not None:
            self.event._cd_memory_provider_started_v1 = True
        return await self.provider.text_chat(*args, **kwargs)

    async def text_chat_stream(self, *args, **kwargs):
        await self._check()
        if self.event is not None:
            self.event._cd_memory_provider_started_v1 = True
        stream = self.provider.text_chat_stream(*args, **kwargs)
        try:
            async for item in stream:
                yield item
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                await close()


def _install_projection(plugin, event, request, result, *, allow_history_projection=True):
    runner = getattr(result, "agent_runner", None)
    if runner is None:
        return False
    native_bundle = getattr(request, "_lm_native_memory_v1", None)
    native_present = native_bundle is not None
    summary_present = getattr(request, "_lm_private_summary_sources_v1", None) is not None
    owned_original_input = getattr(event, "_cd_owned_original_input_v1", False) is True
    native_guard_needed = native_present or summary_present or owned_original_input
    native_guard_ready = (
        _install_native_memory_guard(runner, request, event, native_bundle)
        if native_guard_needed else False
    )
    if native_guard_needed and not native_guard_ready:
        logger.info("Native source guard unavailable for owned request")
        return False
    if getattr(runner, "_cd_native_memory_projection_v1", False):
        return not native_guard_needed or native_guard_ready
    shutting_down = bool(getattr(plugin, "_shutting_down", False))
    managed = bool(getattr(event, "_chat_dynamics_owned_request", False))
    extras = getattr(event, "_extras", {})
    managed = managed or (isinstance(extras, dict) and bool(extras.get("_narrative_managed_v1")))
    if shutting_down or not allow_history_projection or managed:
        return native_guard_ready
    contexts = getattr(getattr(runner, "run_context", None), "messages", None)
    if not isinstance(contexts, list) or not contexts:
        return native_guard_ready
    # reset appends the current request after pre-existing contexts; the fixed
    # final user object is captured once and is never re-discovered downstream.
    request_user = contexts[-1]
    if getattr(request_user, "role", None) != "user":
        return native_guard_ready
    from .character_view import (HistoryViewProvider, match_host_history_prefix,
                                 resolve_history_boundary)
    from .narrative import _narrative_scope_key

    receipt = getattr(request, "_cd_memory_feedback_v1", None)
    receipt = receipt if isinstance(receipt, dict) else {}
    snapshot = getattr(request, "_cd_memory_feedback_snapshot_v1", None)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    expected_rows = snapshot.get("rows")
    evidence_view = receipt.get("evidence_view")
    purpose = receipt.get("purpose")
    expected_snapshot = snapshot.get("snapshot_fingerprint")
    scope = receipt.get("scope")
    card_version = receipt.get("card_version")
    expected_scope_key = _narrative_scope_key(scope)
    host_projection_ready = bool(expected_snapshot and isinstance(expected_rows, list)
                                 and isinstance(scope, dict) and isinstance(evidence_view, dict))
    if not host_projection_ready:
        return native_guard_ready
    base_boundary = resolve_history_boundary(runner, request_user)

    def boundary_callback(provider_contexts, provider):
        boundary = base_boundary(provider_contexts, provider)
        raw_prefix = boundary.get("raw_prefix") if isinstance(boundary, dict) else None
        identity_match, offset = match_host_history_prefix(
            raw_prefix, expected_rows, boundary.get("protected_raw_indices", ()))
        identity_ok = (boundary.get("state") == "aligned" and expected_snapshot and
                       identity_match and isinstance(evidence_view, dict) and
                       type(evidence_view.get("schema_version")) is int and evidence_view.get("schema_version") == 1 and purpose in {"reply", "history_quote"} and
                       evidence_view.get("available") is True and evidence_view.get("purpose") == purpose and
                       evidence_view.get("card_version") == card_version and expected_scope_key and
                       evidence_view.get("scope_key") == expected_scope_key and
                       type(evidence_view.get("correction_revision")) is int and
                       evidence_view.get("correction_revision") >= 0 and
                       isinstance(evidence_view.get("atoms"), list) and isinstance(evidence_view.get("slots"), dict) and
                       isinstance(evidence_view.get("history_projection"), dict) and
                       isinstance(evidence_view.get("revocations"), list))
        boundary["evidence_required"] = True
        if identity_ok:
            boundary["evidence_identity"] = "matched"
            boundary["evidence_view"] = evidence_view
            boundary["evidence_position_offset"] = offset
            state["evidence_identity"] = "matched"
        else:
            boundary["evidence_identity"] = "unavailable"
            boundary["evidence_reason"] = "native_history_snapshot_mismatch_or_view_unavailable"
            state["evidence_identity"] = "unavailable"
        return boundary

    narrative = getattr(plugin, "narrative", None)
    revision_check = None
    native_validate = None
    if isinstance(evidence_view, dict):
        def revision_check():
            valid = bool(narrative and narrative.evidence_current(
                {"scope": scope, "evidence_view": evidence_view}))
            if not valid:
                try:
                    setattr(event, "_chat_dynamics_evidence_failed_closed", True)
                except Exception:
                    pass
            return valid

        async def revision_check_async():
            """异步消费方专用的离线程包装。

            revision_check 本身保持同步（下方还有同步消费方持有它），
            这里只让两个异步防守护栏不阻塞事件循环。
            """
            return await asyncio.to_thread(revision_check)

        if native_present:
            native_validate = _make_native_memory_validator(
                native_bundle, getattr(request, "_lm_native_memory_validate_v1", None), request, event)

        async def tool_guard():
            if not await revision_check_async() or (native_validate is not None and not await native_validate()):
                from .member_boundary import BoundaryCancelled
                raise BoundaryCancelled("native_memory_source_invalidated" if native_validate is not None
                                        else "evidence_revision_changed")

        if not getattr(runner, "_cd_native_tool_guard_v1", False):
            try:
                from .agent_bridge import OwnedToolExecutor
                runner.tool_executor = OwnedToolExecutor(runner.tool_executor, guard=tool_guard)
                setattr(runner, "_cd_native_tool_guard_v1", True)
            except Exception as exc:
                logger.info("Native evidence tool guard unavailable type=%s", type(exc).__name__)

        original_send = getattr(event, "send", None)
        if callable(original_send) and not getattr(event, "_cd_native_send_guard_v1", False):
            async def guarded_send(*args, **kwargs):
                # Requests that already captured an evidence view keep its
                # revision gate through plugin shutdown/reload.  A retired
                # native bridge must never turn an invalidated draft into a
                # sendable one by bypassing this check.
                if not await revision_check_async() or (native_validate is not None and not await native_validate()):
                    from .member_boundary import BoundaryCancelled
                    raise BoundaryCancelled("native_memory_source_invalidated" if native_validate is not None
                                            else "evidence_revision_changed")
                result = original_send(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            try:
                setattr(event, "send", guarded_send)
                setattr(event, "_cd_native_send_guard_v1", True)
            except Exception as exc:
                logger.info("Native evidence send guard unavailable type=%s", type(exc).__name__)
    state = {"native_history_covered": True, "evidence_identity": "pending"}

    def record_provider_projection(diagnostic):
        bypassed = int(diagnostic.get("bypassed_rounds", 0) or 0)
        degraded = int(diagnostic.get("degraded_rounds", 0) or 0)
        evidence_state = str(diagnostic.get("evidence_projection") or "unavailable")
        coverage = ("degraded" if evidence_state.startswith("degraded") or degraded
                    else "partial" if bypassed else "projected")
        current = getattr(request, "_cd_memory_feedback_v1", None)
        if isinstance(current, dict):
            diagnostics = dict(current.get("diagnostics", {}), native_history_covered=True,
                               provider_wrapper_installed=True, coverage_status=coverage,
                               provider_projection=diagnostic)
            setattr(request, "_cd_memory_feedback_v1", dict(current, diagnostics=diagnostics))

    runner.provider = HistoryViewProvider(runner.provider, boundary_callback, state,
                                          revision_check=revision_check,
                                          diagnostic_callback=record_provider_projection)
    runner.fallback_providers = [HistoryViewProvider(provider, boundary_callback, state,
                                                       revision_check=revision_check,
                                                       diagnostic_callback=record_provider_projection)
                                 for provider in getattr(runner, "fallback_providers", ())]
    if native_guard_needed and not getattr(runner, "_cd_native_memory_validation_v1", False):
        return False
    setattr(runner, "_cd_native_memory_projection_v1", True)
    if receipt:
        updated = dict(receipt, status="applied", reason="", diagnostics=dict(
            receipt.get("diagnostics", {}), native_history_covered=True,
            provider_wrapper_installed=True, coverage_status="awaiting_provider_call"))
        try:
            setattr(request, "_cd_memory_feedback_v1", updated)
        except Exception:
            pass
        state["evidence_identity"] = "prepared"
    else:
        state["evidence_identity"] = "unavailable"
    return True


def install_native_builder_bridge(plugin):
    """Patch only the two AstrBot 4.28.2 builder aliases; restore by identity."""
    try:
        import astrbot.core.astr_main_agent as main_agent
        import astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal as internal
    except Exception as exc:
        logger.info("Native memory projection bridge unavailable type=%s", type(exc).__name__)
        return None
    active = {"value": True}
    patches = []
    for module, attribute in ((main_agent, "build_main_agent"), (internal, "build_main_agent")):
        original = getattr(module, attribute, None)
        if not callable(original):
            continue
        identity = getattr(original, "_cd_native_memory_wrapper_identity_v1", None)
        is_our_wrapper = (getattr(original, "_cd_native_memory_bridge_v1", False)
                          and isinstance(identity, weakref.ReferenceType)
                          and identity() is original)
        if is_our_wrapper:
            previous_active = getattr(original, "_cd_native_memory_active_ref", None)
            if isinstance(previous_active, dict) and previous_active.get("value"):
                continue
            retired_original = getattr(original, "_cd_native_memory_original_v1", None)
            if callable(retired_original):
                setattr(module, attribute, retired_original)
                original = retired_original

        def make_wrapper(original_builder):
            async def wrapped(*args, __original=original_builder, **kwargs):
                result = await __original(*args, **kwargs)
                if result is None:
                    return result
                event = kwargs.get("event")
                request = getattr(result, "provider_request", None) or kwargs.get("req")
                if event is None or request is None:
                    return result
                native_bundle = getattr(request, "_lm_native_memory_v1", None)
                if (not active["value"] or getattr(plugin, "_shutting_down", False)
                        or getattr(event, "_chat_dynamics_owned_request", False)):
                    # Owned agents dispatch OnLLMRequest after this builder
                    # returns. Re-read the bundle after reset, once LM's hook
                    # has had a chance to attach its source proof.
                    if native_bundle is not None or getattr(result, "reset_coro", None) is not None:
                        _install_native_guard_after_reset(
                            plugin, event, request, result, allow_history_projection=False)
                    return result
                extras = getattr(event, "_extras", {})
                if isinstance(extras, dict) and extras.get("_narrative_managed_v1"):
                    if native_bundle is not None:
                        _install_native_guard_after_reset(
                            plugin, event, request, result, allow_history_projection=False)
                    return result
                prepare = getattr(plugin, "prepare_memory_request", None)
                if callable(prepare) and not isinstance(getattr(request, "_cd_memory_feedback_v1", None), dict):
                    try:
                        await prepare(event, request)
                    except Exception as exc:
                        logger.info("Native memory view preparation unavailable type=%s", type(exc).__name__)
                reset_coro = getattr(result, "reset_coro", None)
                if reset_coro is not None:
                    result.reset_coro = _ResetDelegate(
                        reset_coro, lambda: _install_projection(
                            plugin, event, request, result,
                            allow_history_projection=active["value"] and not getattr(plugin, "_shutting_down", False)))
                else:
                    _install_projection(plugin, event, request, result,
                                        allow_history_projection=active["value"] and not getattr(plugin, "_shutting_down", False))
                return result

            wrapped = functools.wraps(original_builder, updated=())(wrapped)
            wrapped._cd_native_memory_bridge_v1 = True
            wrapped._cd_native_memory_original_v1 = original_builder
            wrapped._cd_native_memory_active_ref = active
            wrapped._cd_native_memory_wrapper_identity_v1 = weakref.ref(wrapped)
            return wrapped

        wrapped = make_wrapper(original)
        setattr(module, attribute, wrapped)
        patches.append((module, attribute, original, wrapped))

    class Handle:
        def uninstall(self):
            active["value"] = False
            for module, attribute, original, wrapped in reversed(patches):
                if getattr(module, attribute, None) is wrapped:
                    setattr(module, attribute, original)

    return Handle() if patches else None
