"""Instance-scoped observation of an AstrBot native message delivery.

AstrBot's respond stage keeps the :class:`MessageEventResult` on the event,
but sends a ``MessageChain`` derived from it.  The guard therefore uses object
identity at the event-result/chain/component boundary; it deliberately never
matches message text or other lossy representations.
"""

from __future__ import annotations

# The installed guard and the completion callback share one delivery contract.
import asyncio
from collections import deque
from dataclasses import dataclass
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from .platform_bridge import chain_plain_text
from .session_runtime import FollowupBatch
from .turn_limits import MAX_TURN_CHARS as _MAX_TURN_CHARS
from .vibe_analyzer import GroupChatMode

import functools
import inspect
import weakref
from typing import Any, Callable, Optional

from .platform_bridge import SendResult, _coerce_success


_MISSING = object()


def _message_id(value: Any) -> Optional[str]:
    """Extract a platform id using the same field names as ``send_plain``."""
    if isinstance(value, dict):
        for key in ("message_id", "id", "msg_id"):
            if value.get(key):
                return str(value[key])
        return None
    for key in ("message_id", "id", "msg_id"):
        try:
            candidate = getattr(value, key, None)
        except Exception:
            candidate = None
        if candidate:
            return str(candidate)
    return None


def _as_send_result(value: Any) -> SendResult:
    """Mirror ``platform_bridge.send_plain``'s raw return interpretation."""
    if value is False:
        return SendResult(False, error="platform rejected the message")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return SendResult(bool(value), error=None if value else "platform rejected the message")
    if value is None:
        # AstrMessageEvent.send() commonly returns no value on success.
        return SendResult(True)
    if isinstance(value, SendResult):
        return value
    if isinstance(value, dict) and any(key in value for key in ("success", "ok", "status")):
        raw_success = value.get("success", value.get("ok", value.get("status")))
        if (
            isinstance(raw_success, (int, float))
            and not isinstance(raw_success, bool)
            and "status" in value
            and "success" not in value
            and "ok" not in value
        ):
            success = 200 <= raw_success < 300
        elif isinstance(raw_success, (int, float)) and not isinstance(raw_success, bool):
            success = bool(raw_success)
        else:
            success = _coerce_success(raw_success)
        error = str(value.get("error")) if value.get("error") else None
        return SendResult(success, _message_id(value), error)
    if isinstance(value, dict) and value.get("error"):
        return SendResult(False, _message_id(value), str(value["error"]))
    try:
        has_success = hasattr(value, "success")
    except Exception:
        has_success = False
    if has_success:
        try:
            success = _coerce_success(getattr(value, "success"))
        except Exception:
            success = False
        try:
            error_value = getattr(value, "error", None)
        except Exception:
            error_value = None
        return SendResult(success, _message_id(value), str(error_value) if error_value else None)
    if isinstance(value, str) and value.strip():
        return SendResult(True, value)
    message_id = _message_id(value)
    if message_id:
        return SendResult(True, message_id)
    return SendResult(True)


class NativeDeliveryGuard:
    """Observe and gate sends for one event/result pair.

    ``event.send`` is wrapped on the event instance only.  A call is observed
    when the event still owns the exact result supplied to this guard and the
    message argument is that result (or an SDK-derived chain sharing its
    chain/components by identity).  Unrelated calls continue through the
    original callable untouched.
    """

    def __init__(
        self,
        event: Any,
        result: Any,
        is_current: Callable[[], bool],
        bypass: Callable[[], bool],
    ) -> None:
        self._result = result
        self._is_current = is_current if callable(is_current) else (lambda: False)
        self._bypass = bypass if callable(bypass) else (lambda: False)
        self._event_ref: Optional[weakref.ReferenceType[Any]] = None
        self._event_fallback: Any = None
        try:
            self._event_ref = weakref.ref(event)
        except TypeError:
            self._event_fallback = event

        self._result_chain = self._read_attr(result, "chain")
        self._component_ids = self._component_identity_set(self._result_chain)
        self._original_send: Any = None
        self._wrapper: Any = None
        self._installed = False
        self._outcome: Optional[SendResult] = None
        self._started = False
        self._cancelled = False

    @property
    def outcome(self) -> Optional[SendResult]:
        """The first observed send outcome, or ``None`` while unobserved."""
        return self._outcome

    @property
    def started(self) -> bool:
        """Whether a bound, non-bypassed send has entered the original call."""
        return self._started

    @property
    def cancelled(self) -> bool:
        """Whether a bound send was found stale before or during delivery."""
        return self._cancelled

    def install(self) -> bool:
        """Install an instance wrapper, returning ``False`` when impossible."""
        event = self._event()
        if event is None or self._result is None:
            return False

        try:
            current_send = getattr(event, "send")
        except Exception:
            return False
        if not callable(current_send):
            return False
        if self._installed and current_send is self._wrapper:
            return True

        original_send = current_send
        wrapper = self._make_wrapper(original_send)
        try:
            setattr(event, "send", wrapper)
        except Exception:
            return False
        self._original_send = original_send
        self._wrapper = wrapper
        self._installed = True
        return True

    def restore(self) -> None:
        """Restore only this guard's wrapper when it remains installed."""
        event = self._event()
        wrapper = self._wrapper
        original_send = self._original_send
        if event is not None and wrapper is not None:
            try:
                current_send = getattr(event, "send")
            except Exception:
                current_send = _MISSING
            if current_send is wrapper:
                try:
                    setattr(event, "send", original_send)
                except Exception:
                    # Keep the ownership record so a later restore can retry.
                    return
        self._original_send = None
        self._wrapper = None
        self._installed = False

    def _event(self) -> Any:
        if self._event_ref is not None:
            return self._event_ref()
        return self._event_fallback

    @staticmethod
    def _read_attr(value: Any, name: str) -> Any:
        try:
            return getattr(value, name)
        except Exception:
            return _MISSING

    @staticmethod
    def _component_identity_set(chain: Any) -> set[int]:
        if chain is _MISSING or chain is None or isinstance(chain, (str, bytes, bytearray)):
            return set()
        try:
            return {id(component) for component in chain}
        except (TypeError, RuntimeError):
            return set()

    @staticmethod
    def _call_message(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if args:
            return args[0]
        for name in ("message", "chain", "result"):
            if name in kwargs:
                return kwargs[name]
        return _MISSING

    def _candidate_related(self, candidate: Any) -> bool:
        if candidate is self._result:
            return True
        if candidate is self._result_chain and candidate is not _MISSING:
            return True
        candidate_chain = self._read_attr(candidate, "chain")
        if candidate_chain is _MISSING:
            return False
        if candidate_chain is self._result_chain and candidate_chain is not _MISSING:
            return True
        return bool(self._component_ids.intersection(self._component_identity_set(candidate_chain)))

    def _is_bound_send(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        event = self._event()
        if event is None:
            return False

        getter = self._read_attr(event, "get_result")
        current_result_known = False
        current_result = _MISSING
        if callable(getter):
            try:
                current_result = getter()
            except Exception:
                pass
            else:
                current_result_known = True
                if current_result is not self._result:
                    return False

        candidate = self._call_message(args, kwargs)
        if candidate is _MISSING:
            return current_result_known
        return self._candidate_related(candidate)

    def _current_now(self) -> bool:
        try:
            return bool(self._is_current())
        except Exception:
            return False

    def _bypass_now(self) -> bool:
        try:
            return bool(self._bypass())
        except Exception:
            # An ownership predicate must not make an unrelated host send
            # raise.  The guarded path remains fail-closed below.
            return False

    def _record(self, outcome: SendResult) -> None:
        if self._outcome is None:
            self._outcome = outcome

    def _record_exception(self, exc: BaseException) -> None:
        self._record(SendResult(False, error=str(exc) or type(exc).__name__))

    def _mark_cancelled_if_stale(self) -> None:
        if not self._current_now():
            self._cancelled = True

    def _make_wrapper(self, original_send: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(original_send):

            @functools.wraps(original_send)
            async def wrapped_async(*args: Any, **kwargs: Any) -> Any:
                if not self._is_bound_send(args, kwargs) or self._bypass_now():
                    return await self._await_original(original_send, *args, **kwargs)
                if not self._current_now():
                    self._cancelled = True
                    self._record(SendResult(False))
                    return None
                self._started = True
                try:
                    value = await self._await_original(original_send, *args, **kwargs)
                except BaseException as exc:
                    self._record_exception(exc)
                    raise
                self._record(_as_send_result(value))
                self._mark_cancelled_if_stale()
                return value

            return wrapped_async

        @functools.wraps(original_send)
        def wrapped_sync(*args: Any, **kwargs: Any) -> Any:
            if not self._is_bound_send(args, kwargs) or self._bypass_now():
                return original_send(*args, **kwargs)
            if not self._current_now():
                self._cancelled = True
                self._record(SendResult(False))
                return None
            self._started = True
            try:
                value = original_send(*args, **kwargs)
            except BaseException as exc:
                self._record_exception(exc)
                raise
            if inspect.isawaitable(value):
                return self._finish_awaitable(value)
            self._record(_as_send_result(value))
            self._mark_cancelled_if_stale()
            return value

        return wrapped_sync

    async def _await_original(
        self,
        original_send: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        value = original_send(*args, **kwargs)
        if inspect.isawaitable(value):
            return await value
        return value

    async def _finish_awaitable(self, value: Any) -> Any:
        try:
            value = await value
        except BaseException as exc:
            self._record_exception(exc)
            raise
        self._record(_as_send_result(value))
        self._mark_cancelled_if_stale()
        return value


@dataclass
class _NativeEventContext:
    """Delivery state retained until the host completes one native result."""

    event: Any = None
    trigger_node: Any = None
    vibe_mode: Any = None
    guard: Optional[NativeDeliveryGuard] = None
    result: Any = None
    fragments: tuple[str, ...] = ()
    epoch: int = 0
    owner_user_id: str = ""
    owner_revision: int = 0
    platform_message_id: Optional[str] = None
    installed: bool = False


async def after_message_sent(host, event: AstrMessageEvent) -> None:
    if host._persona_mode():
        return
    if host._shutting_down or host.shadow_mode:
        return
    if host._is_command_event(event):
        return
    session_key = host._event_in_scope(event)
    if not session_key:
        return
    if host._is_owned_send(event, session_key):
        return
    host._track_hook_task(session_key)
    runtime = host._sessions.get(session_key)
    if runtime is None or runtime.dag is None:
        return
    context_key = (session_key, id(event))
    native_context = host._native_context_by_event.get(context_key)
    # A native result must have an installed guard and an observed host
    # outcome before it can mutate DAG, sent IDs, arbiter state, or tails.
    # This also makes an unsupported event.send wrapper fail open for the
    # host while keeping this plugin's bookkeeping side-effect free.
    if native_context is None:
        return
    if not native_context.installed or native_context.guard is None:
        host._drop_native_context(context_key)
        return
    guard = native_context.guard
    outcome = guard.outcome
    if outcome is None:
        host._drop_native_context(context_key)
        return
    if not outcome.success:
        host._metric("send_failed")
        host._clear_pending_gate(runtime)
        host._drop_native_context(context_key)
        return

    # A reset/cool changes the session epoch and must discard even a
    # successful late native callback.  A member stop only changes the
    # owner revision, so a first segment that already entered send() is
    # retained below while its unsent tail is suppressed.
    event_epoch = getattr(event, "_chat_dynamics_epoch", None)
    try:
        stale_epoch = (
            event_epoch is not None and int(event_epoch) != runtime.epoch
        ) or native_context.epoch != runtime.epoch
    except (TypeError, ValueError):
        stale_epoch = True
    if stale_epoch:
        host._metric("stale_hook_ignored")
        host._drop_native_context(context_key)
        return

    getter = getattr(event, "get_result", None)
    result = getter() if callable(getter) else None
    text = host._bounded_text(chain_plain_text(result), _MAX_TURN_CHARS) if result is not None else ""
    if not (text or "").strip() and native_context.fragments:
        text = host._bounded_text(native_context.fragments[0], _MAX_TURN_CHARS)
    batch: Optional[FollowupBatch] = None
    rest: list[str] = []
    delivery_token = 0
    previous_bot_msg_id: Optional[str] = None
    previous_platform_msg_id: Optional[str] = None
    try:
        async with runtime.state_lock:
            if host._sessions.get(session_key) is not runtime or runtime.epoch != native_context.epoch:
                host._metric("stale_hook_ignored")
                return

            trigger_node = native_context.trigger_node or runtime.native_trigger_node
            platform_msg_id = outcome.message_id or native_context.platform_message_id
            if (text or "").strip():
                bot_msg_id = platform_msg_id or host._next_outgoing_id()
                trigger = trigger_node
                bot_node = runtime.dag.add_message(
                    msg_id=bot_msg_id,
                    user_id=runtime.bot_id or "bot",
                    text=host._bounded_text(text.strip()),
                    timestamp=host.time_service.time(),
                    reply_to_id=trigger.msg_id if trigger is not None else None,
                    metadata={"platform_message_id": bool(platform_msg_id),
                              "trigger_user_id": getattr(trigger, "user_id", "")},
                )
                bot_node.metadata["platform_message_id"] = bool(platform_msg_id)
                host._observe_routed_bot(runtime, bot_node)
                runtime.last_bot_node = bot_node
                runtime.touch(host.time_service.time())
                host._last_bot_nodes[session_key] = bot_node
                host._remember_sent_id(session_key, bot_msg_id)
                host._metric("send_succeeded")
                host._schedule_neural_embed(session_key, bot_node)
                host.arbiter.record_bot_spoke(
                    session_key,
                    timestamp=host.time_service.time(),
                    user_id=trigger.user_id if trigger is not None else None,
                    topic_id=str(getattr(runtime.routing_state, "last_bot_topic_id", "") or ""),
                    msg_id=str(getattr(bot_node, "msg_id", "") or ""),
                )
                host._commit_pending_gate_spoke(runtime, session_key)
                previous_bot_msg_id = bot_msg_id
                previous_platform_msg_id = platform_msg_id

            owner_current = host._user_revision_is_current(
                runtime,
                native_context.owner_user_id,
                native_context.owner_revision,
            )
            # ``cancelled`` covers a stop/reset that raced an in-flight
            # host send.  The successful first segment is already real;
            # only its not-yet-sent tail is invalid in that case.
            if owner_current and not guard.cancelled and len(native_context.fragments) > 1:
                fragments = native_context.fragments[1:]
                queue_full = (
                    runtime.followup_queue.maxlen is not None
                    and len(runtime.followup_queue) >= runtime.followup_queue.maxlen
                )
                followup = FollowupBatch(
                    fragments=deque(fragments),
                    epoch=runtime.epoch,
                    trigger_node=trigger_node,
                    vibe_mode=native_context.vibe_mode,
                    event_id=id(event),
                    batch_id=f"native_{int(host.time_service.time() * 1000)}_{id(event)}",
                    sent_count=1,
                    trigger_user_id=native_context.owner_user_id,
                    delivery_token=runtime.next_followup_delivery_token(),
                )
                runtime.followup_queue.append(followup)
                if queue_full:
                    host._metric("followup_dropped")

            if not runtime.followup_queue and not any(
                key[0] == session_key and key != context_key
                for key in host._native_context_by_event
            ):
                runtime.native_trigger_node = None
                runtime.native_vibe_mode = None

            # A user stop may have arrived after the first send completed.
            # It must preserve the first node but prevent all tail work.
            if not owner_current or guard.cancelled:
                for candidate in list(runtime.followup_queue):
                    if candidate.event_id == id(event):
                        candidate.invalidate()
                runtime.followup_queue = deque(
                    (
                        candidate
                        for candidate in runtime.followup_queue
                        if candidate.event_id != id(event)
                    ),
                    maxlen=runtime.followup_queue.maxlen,
                )

        if not owner_current or guard.cancelled:
            return
        candidate_index = next(
            (
                index
                for index, candidate in enumerate(runtime.followup_queue)
                if candidate.event_id == id(event)
            ),
            None,
        )
        if candidate_index is not None:
            candidate = runtime.followup_queue[candidate_index]
            del runtime.followup_queue[candidate_index]
            if candidate.epoch == runtime.epoch and not candidate.invalidated:
                batch = candidate
                rest = batch.remaining()
                delivery_token = runtime.register_active_followup_batch(batch)
            else:
                host._metric("stale_followup_dropped")
    finally:
        # Restore the exact event.send callable before delivering any
        # plugin-owned tail; _send_owned has its own explicit bypass.
        host._drop_native_context(context_key)
    if batch is None or not rest:
        if batch is not None:
            batch.fragments.clear()
            if delivery_token:
                runtime.unregister_active_followup_batch(delivery_token, batch)
        return
    current_task = asyncio.current_task()
    if current_task is not None:
        def cleanup_cancelled_followup(_done: asyncio.Task) -> None:
            batch.fragments.clear()
            runtime.unregister_active_followup_batch(delivery_token, batch)

        current_task.add_done_callback(cleanup_cancelled_followup)
    async with runtime.send_lock:
        try:
            for fragment in rest:
                if (
                    host._shutting_down
                    or runtime.active_followup_batches.get(delivery_token) is not batch
                    or batch.invalidated
                    or batch.delivery_token != delivery_token
                    or batch.epoch != runtime.epoch
                ):
                    host._metric("followup_dropped")
                    return
                await host.time_service.sleep(
                    host.pacer.calculate_inter_burst_delay(
                        mode=batch.vibe_mode or runtime.native_vibe_mode or GroupChatMode.CHILL_FADE,
                        fragment_text=fragment,
                        mpm=host.vibe_analyzer.get_telemetrics(session_key).mpm,
                        delay_scale=getattr(runtime, "last_delay_scale", 1.0),
                    )
                )
                if (
                    host._shutting_down
                    or runtime.active_followup_batches.get(delivery_token) is not batch
                    or batch.invalidated
                    or batch.delivery_token != delivery_token
                    or batch.epoch != runtime.epoch
                ):
                    host._metric("followup_dropped")
                    return
                send_result = await host._send_owned(
                    runtime,
                    event,
                    fragment,
                    reply_to_id=previous_platform_msg_id,
                )
                if not send_result.success:
                    host._metric("send_failed")
                    logger.error(
                        "[ChatDynamics] Native follow-up send failed for session %s (send_failed)",
                        host._session_label(session_key),
                    )
                    return
                host._metric("send_succeeded")
                batch.sent_count += 1
                platform_msg_id = send_result.message_id
                bot_msg_id = platform_msg_id or host._next_outgoing_id()
                # Delivery already happened, so this identity is real no
                # matter what the guards below decide. Recording it after
                # the guard let an invalidated tail leave the platform echo
                # of a delivered message unrecognized, which is exactly the
                # input the reboot-loop defence is supposed to catch.
                host._remember_sent_id(session_key, bot_msg_id)
                async with runtime.state_lock:
                    if (
                        host._shutting_down
                        or runtime.active_followup_batches.get(delivery_token) is not batch
                        or batch.invalidated
                        or batch.delivery_token != delivery_token
                        or batch.epoch != runtime.epoch
                    ):
                        host._metric("followup_dropped")
                        return
                    bot_node = runtime.dag.add_message(
                        msg_id=bot_msg_id,
                        user_id=runtime.bot_id or "bot",
                        text=host._bounded_text(fragment),
                        timestamp=host.time_service.time(),
                        reply_to_id=previous_bot_msg_id,
                        metadata={"platform_message_id": bool(platform_msg_id),
                                  "trigger_user_id": (getattr(batch.trigger_node,
                                                       "user_id", "")
                                                       or batch.trigger_user_id)},
                    )
                    bot_node.metadata["platform_message_id"] = bool(platform_msg_id)
                    host._observe_routed_bot(runtime, bot_node)
                    runtime.last_bot_node = bot_node
                    runtime.touch(host.time_service.time())
                    host._last_bot_nodes[session_key] = bot_node
                    host._schedule_neural_embed(session_key, bot_node)
                    previous_bot_msg_id = bot_msg_id
                    previous_platform_msg_id = platform_msg_id
        finally:
            batch.fragments.clear()
            runtime.unregister_active_followup_batch(delivery_token, batch)
