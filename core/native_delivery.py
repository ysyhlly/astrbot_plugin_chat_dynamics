"""Instance-scoped observation of an AstrBot native message delivery.

AstrBot's respond stage keeps the :class:`MessageEventResult` on the event,
but sends a ``MessageChain`` derived from it.  The guard therefore uses object
identity at the event-result/chain/component boundary; it deliberately never
matches message text or other lossy representations.
"""

from __future__ import annotations

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
