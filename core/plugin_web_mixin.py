"""Web API forwarding, config panel, notebook, and session-lifecycle admin mixin for ChatDynamicsPlugin."""

from __future__ import annotations

import asyncio
import math
import sys
from typing import Any, Optional

from astrbot.api import logger

from .config_panel import ConfigPanel
from .native_delivery import _NativeEventContext
from .poke import drop_poke_streaks

_DEFAULT_SESSION_IDLE_SECONDS = 3600.0


class PluginWebAndAdminMixin:
    """Non-decorator administrative, notebook, session-lifecycle, and Web API methods."""

    def preset_catalog(self) -> dict[str, Any]:
        return ConfigPanel(self).preset_catalog()

    async def apply_preset(self, name: str) -> dict[str, Any]:
        return await ConfigPanel(self).apply_preset(name)

    def _config_schema(self) -> dict[str, Any]:
        return ConfigPanel(self)._config_schema()

    def _config_stored_values(self) -> dict[str, Any]:
        return ConfigPanel(self)._config_stored_values()

    def get_effective_config(self) -> dict[str, Any]:
        return ConfigPanel(self).get_effective_config()

    def notebook_list(self, umo: str) -> dict[str, Any]:
        store = getattr(self, "group_memory", None)
        if store is None:
            return {"anniversaries": [], "reminders": [], "slang_trials": [], "mute_until": 0.0}
        return store.list_all(str(umo or ""))

    async def notebook_mutate_async(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Deliberately not offloaded to a worker thread: the notebook keeps an
        # in-process cache that the message path (reminder popping, memory
        # lines) reads and writes on the event loop, so a second thread would
        # add read-modify-write races for a small blocking-IO gain.
        if action != "add_slang":
            return self.notebook_mutate(action, payload)
        umo = str(payload.get("umo") or payload.get("session_key") or "").strip()
        if not umo:
            raise ValueError("umo required")
        item = await self.group_memory.add_slang_trial_async(
            umo, phrase=str(payload.get("phrase") or ""), approved=payload.get("approved") is True
        )
        return {"item": item}

    @staticmethod
    def _notebook_number(
        payload: dict[str, Any],
        key: str,
        *,
        default: Optional[float] = None,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        """Strict numeric field for notebook payloads.

        The panel and the HTTP API share this path, and ``json`` accepts
        ``Infinity``/``NaN`` literals, so a float() coercion alone would let a
        request store a non-finite deadline such as a permanent mute.
        """
        raw = payload.get(key)
        if raw is None or raw == "":
            if default is None:
                raise ValueError(f"{key} required")
            return float(default)
        if isinstance(raw, bool) or isinstance(raw, (dict, list, tuple, set)):
            raise ValueError(f"{key} must be a number")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if minimum is not None and value < minimum:
            raise ValueError(f"{key} must be >= {minimum:g}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{key} must be <= {maximum:g}")
        return value

    def notebook_mutate(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """CRUD-lite for group notebook. Raises ValueError on bad input."""
        store = getattr(self, "group_memory", None)
        mood = getattr(self, "mood_memory", None)
        if store is None:
            raise RuntimeError("group memory unavailable")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        action = str(action or "").strip()
        umo = str(payload.get("umo") or payload.get("session_key") or "").strip()
        if not umo:
            raise ValueError("umo required")
        if len(umo) > 256:
            raise ValueError("umo too long")
        if action == "list":
            return self.notebook_list(umo)
        if action == "mute_tonight":
            hours = self._notebook_number(payload, "hours", default=10.0, minimum=0.0, maximum=720.0)
            until = store.mute_tonight(umo, hours=hours)
            if hours > 0:
                self.reminders.cancel(umo)
            if mood is not None:
                mood.mute_tonight(umo, hours=hours)
            return {"mute_until": until}
        if action == "forget":
            peer = str(payload.get("peer_id") or "")[:128]
            tag = str(payload.get("tag") or "")[:64]
            ok = bool(mood and mood.forget(umo, peer, tag=tag))
            return {"forgotten": ok}
        if action == "add_anniversary":
            month = self._notebook_number(payload, "month", default=0, minimum=1, maximum=12)
            day = self._notebook_number(payload, "day", default=0, minimum=1, maximum=31)
            if not month.is_integer() or not day.is_integer():
                raise ValueError("month and day must be integers")
            item = store.add_anniversary(
                umo,
                title=self._bounded_text(payload.get("title") or "", 200),
                month=int(month),
                day=int(day),
                note=self._bounded_text(payload.get("note") or "", 500),
            )
            return {"item": item}
        if action == "remove_anniversary":
            return {"removed": store.remove_anniversary(umo, str(payload.get("id") or "")[:128])}
        if action == "add_reminder":
            item = store.add_reminder(
                umo,
                text=self._bounded_text(payload.get("text") or "", 500),
                due_at=self._notebook_number(payload, "due_at", minimum=0.0, maximum=253402300800.0),
                created_by=str(payload.get("created_by") or "")[:128],
            )
            self.reminders.start()
            narrative = getattr(self, "narrative", None)
            if narrative is not None:
                effect = getattr(narrative, "queue_reminder_effect", None) or narrative.reminder_effect
                effect(umo, item["id"], "created")
            return {"item": item}
        if action in {"remove_reminder", "mark_done", "done_reminder"}:
            item_id = str(payload.get("id") or "")[:128]
            removed = store.remove_reminder(umo, item_id)
            if removed:
                self.reminders.cancel(umo, item_id)
                narrative = getattr(self, "narrative", None)
                if narrative is not None:
                    effect = getattr(narrative, "queue_reminder_effect", None) or narrative.reminder_effect
                    effect(umo, item_id, "cancelled")
            return {"removed": removed}
        if action == "add_slang":
            item = store.add_slang_trial(
                umo,
                phrase=self._bounded_text(payload.get("phrase") or "", 64),
                approved=payload.get("approved") is True,
            )
            return {"item": item}
        if action == "remove_slang":
            return {"removed": store.remove_slang(umo, str(payload.get("id") or "")[:128])}
        if action == "due_reminders":
            return {"items": store.due_reminders(umo)}
        if action == "due_anniversaries":
            return {"items": store.due_anniversaries(umo)}
        raise ValueError(f"unknown notebook action: {action}")

    def is_narrative_session_enabled(self, umo: str) -> bool:
        """Public early-hook ownership check; character binding is resolved separately."""
        narrative = getattr(self, "narrative", None)
        return bool(narrative and narrative.enabled(umo))

    def get_config_panel(self, *, refresh: bool = True) -> dict[str, Any]:
        return ConfigPanel(self).get_config_panel(refresh=refresh)

    def _normalize_config_update_value(self, key: str, value: Any, field_schema: dict[str, Any]) -> Any:
        return ConfigPanel(self)._normalize_config_update_value(key, value, field_schema)

    async def save_config_values(
        self, updates: dict[str, Any], *, baseline: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await ConfigPanel(self).save_config_values(updates, baseline=baseline)

    def _serialize_provider(self, provider: Any) -> dict[str, Any]:
        return ConfigPanel(self)._serialize_provider(provider)

    def list_available_providers(self) -> dict[str, Any]:
        return ConfigPanel(self).list_available_providers()

    async def discover_setup_groups(self) -> dict[str, Any]:
        from .setup_wizard import discover_setup_groups

        return await discover_setup_groups(self)

    async def probe_setup_providers(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .setup_wizard import probe_setup_providers

        return await probe_setup_providers(self, payload)

    async def apply_stored_config(self) -> dict[str, Any]:
        return await ConfigPanel(self).apply_stored_config()

    async def _apply_preset_locked(self, name: str) -> dict[str, Any]:
        return await ConfigPanel(self)._apply_preset_locked(name)

    def _cancel_embedding_tasks(self, session_id: str) -> list[asyncio.Task]:
        tasks = list(self._embedding_tasks_by_session.pop(session_id, set()))
        for task in tasks:
            if not task.done():
                task.cancel()
        return tasks

    def _cancel_hook_tasks(self, session_id: str) -> list[asyncio.Task]:
        tasks = list(self._hook_tasks_by_session.pop(session_id, set()))
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        return tasks

    def _drop_native_context(self, key: tuple[str, int]) -> Optional[_NativeEventContext]:
        """Drop one native callback context and restore its event wrapper."""
        context = self._native_context_by_event.pop(key, None)
        if context is None:
            return None
        guard = context.guard
        if guard is not None:
            try:
                guard.restore()
            except Exception as exc:
                logger.debug(
                    "[ChatDynamics] Native guard restore skipped code=CD_NATIVE_GUARD_RESTORE type=%s",
                    type(exc).__name__,
                )
        event = context.event
        if event is not None:
            try:
                delattr(event, "_chat_dynamics_native_platform_message_id")
            except Exception:
                pass
        return context

    def _clear_native_context(self, session_id: str) -> None:
        for key in [key for key in self._native_context_by_event if key[0] == session_id]:
            self._drop_native_context(key)

    def _clear_all_native_contexts(self) -> None:
        for key in list(self._native_context_by_event):
            self._drop_native_context(key)

    def _prune_native_contexts(self) -> None:
        """Remove contexts whose UMO was dropped or whose epoch was reset."""
        stale: list[tuple[str, int]] = []
        for key, context in list(self._native_context_by_event.items()):
            runtime = self._sessions.get(key[0])
            if runtime is None or context.epoch != runtime.epoch:
                stale.append(key)
        for key in stale:
            self._drop_native_context(key)

    def _set_native_context(
        self,
        key: tuple[str, int],
        value: _NativeEventContext | tuple[Any, Any],
        *,
        overwrite: bool = False,
    ) -> None:
        if key not in self._native_context_by_event and len(self._native_context_by_event) >= 4096:
            self._drop_native_context(next(iter(self._native_context_by_event)))
            self._metric("native_context_evicted")
        if overwrite or key not in self._native_context_by_event:
            if isinstance(value, _NativeEventContext):
                context = value
            else:
                trigger_node, vibe_mode = value
                context = _NativeEventContext(
                    trigger_node=trigger_node,
                    vibe_mode=vibe_mode,
                )
            self._native_context_by_event[key] = context

    def _session_last_activity(self, session_id: str, *, debounce_activity: dict[str, float] | None = None) -> float:
        last = 0.0
        runtime = self._sessions.get(session_id)
        if runtime is not None:
            last = max(last, runtime.last_activity)
        dag = self.dags.get(session_id)
        if dag is not None:
            last = max(last, dag.last_timestamp())
        last = max(last, self.telemetrics.last_timestamp(session_id))
        last = max(last, self.arbiter.last_spoke_time(session_id))
        last = max(
            last,
            self.debounce.last_activity(session_id)
            if debounce_activity is None
            else debounce_activity.get(session_id, 0.0),
        )
        return last

    def _drop_session(self, session_id: str, *, debounce_active: bool | None = None) -> None:
        with self._capacity_lock:
            runtime = self._sessions.get(session_id)
            if runtime is not None:
                if (
                    runtime.state_lock.locked()
                    or runtime.send_lock.locked()
                    or runtime.active_followup_batches
                    or (self.debounce.has_active_session(session_id) if debounce_active is None else debounce_active)
                ):
                    return
                runtime.epoch += 1
                runtime.latest_pending = None
                runtime.clear_active_followup_batches()
                runtime.followup_queue.clear()
            self._cancel_embedding_tasks(session_id)
            self._cancel_hook_tasks(session_id)
            self._clear_native_context(session_id)
            self._registry.drop(session_id)
            self._last_bot_nodes.pop(session_id, None)
            self._umo_by_session.pop(session_id, None)
            self._vibe_msg_counts.pop(session_id, None)
            self.vibe_analyzer.reset_session(session_id)
            self.arbiter.reset_session(session_id, keep_cooling=True)
            try:
                self.decision_gate.reset_session(session_id, keep_cool=True)
            except Exception:
                pass

    def _prune_idle_sessions_after_generation(self, now: float) -> None:
        last = getattr(self, "_last_idle_sweep_at", None)
        if last is None or now < last or now - last >= 30.0:
            self._prune_idle_sessions(now)

    def _prune_idle_sessions(self, now: float) -> None:
        idle_seconds = getattr(
            sys.modules.get(self.__class__.__module__),
            "_SESSION_IDLE_SECONDS",
            _DEFAULT_SESSION_IDLE_SECONDS,
        )
        self._prune_native_contexts()
        self.arbiter.prune_cooling(current_time=now)
        topics_changed = False
        for runtime in self._sessions.values():
            if not runtime.state_lock.locked():
                runtime.recent_images.prune(now)
                topics_changed = runtime.routing_state.expire_topics(runtime.dag, now) or topics_changed
        if topics_changed:
            self._mark_panel_runtime_dirty()
        debounce_activity, debounce_active = self.debounce.session_activity_snapshot()
        active_telemetry_sessions = set(self._in_flight)
        for session_id, runtime in self._sessions.items():
            if (
                (runtime.generation_task is not None and not runtime.generation_task.done())
                or bool(runtime.owned_turn_tasks)
                or runtime.state_lock.locked()
                or runtime.send_lock.locked()
                or bool(runtime.followup_queue)
                or bool(runtime.active_followup_batches)
                or bool(self._hook_tasks_by_session.get(session_id))
                or any(
                    task is not None and not task.done()
                    for task in self._embedding_tasks_by_session.get(session_id, set())
                )
                or session_id in debounce_active
            ):
                active_telemetry_sessions.add(session_id)
        try:
            self.telemetrics.prune_stale_sessions(
                current_time=now,
                max_idle=idle_seconds,
                exclude_sessions=active_telemetry_sessions,
            )
        except TypeError:
            self.telemetrics.prune_stale_sessions(
                current_time=now,
                max_idle=idle_seconds,
            )
        self.debounce.prune_idle_slots(max_idle_seconds=idle_seconds)
        known = (
            self._registry.known_ids()
            | set(self._umo_by_session)
            | set(self._vibe_msg_counts)
            | set(self._last_bot_nodes)
        )
        for session_id in list(known):
            if session_id in self._in_flight:
                continue
            runtime = self._sessions.get(session_id)
            if runtime is not None and runtime.generation_task is not None and not runtime.generation_task.done():
                continue
            if runtime is not None and runtime.owned_turn_tasks:
                continue
            if any(
                task is not None and not task.done()
                for task in self._embedding_tasks_by_session.get(session_id, set())
            ):
                continue
            if runtime is not None and (runtime.followup_queue or runtime.active_followup_batches):
                continue
            if self._hook_tasks_by_session.get(session_id):
                continue
            if runtime is not None and runtime.state_lock.locked():
                continue
            if runtime is not None and runtime.send_lock.locked():
                continue
            if session_id in debounce_active:
                continue
            last = self._session_last_activity(session_id, debounce_activity=debounce_activity)
            if last <= 0.0:
                dag = self.dags.get(session_id)
                if dag is not None and not dag.nodes:
                    self._drop_session(session_id, debounce_active=False)
                continue
            if (now - last) >= idle_seconds:
                if runtime is not None and (runtime.routing_state.topics or runtime.routing_state.archive.entries):
                    continue
                self._drop_session(session_id, debounce_active=False)
        self._last_idle_sweep_at = now

    def _reset_session_state(
        self,
        session_id: str,
        *,
        invalidate: bool = True,
        cancel_background: bool = True,
    ) -> None:
        key = self._resolve_session_key(session_id) or session_id
        self._clear_native_context(key)
        if cancel_background:
            self._cancel_embedding_tasks(key)
            self._cancel_hook_tasks(key)
        if invalidate:
            self._invalidate_pending_generation(key)
        if key in self.dags:
            self.dags[key].reset()
        self.vibe_analyzer.reset_session(key)
        self.arbiter.reset_session(key)
        try:
            self.decision_gate.reset_session(key)
        except Exception:
            pass
        rhythm_ctrl = getattr(self, "natural_rhythm", None)
        if rhythm_ctrl is not None:
            rhythm_ctrl.reset_session(key)
        self._last_bot_nodes.pop(key, None)
        self._vibe_msg_counts.pop(key, None)
        drop_poke_streaks(self._poke_streaks, key)
        self._poke_replied_ids = {item for item in self._poke_replied_ids if item[0] != key}
        runtime = self._sessions.get(key)
        if runtime is not None:
            runtime.reset_conversation_state()

    async def _reset_session_state_async(self, session_id: str) -> None:
        """Atomically invalidate in-flight work and discard buffered old input."""
        self._metric("reset_triggered")
        key = self._resolve_session_key(session_id) or session_id
        runtime = self._sessions.get(key)
        if runtime is None:
            embedding_tasks = self._cancel_embedding_tasks(key)
            hook_tasks = self._cancel_hook_tasks(key)
            await self.debounce.discard(key)
            self._reset_session_state(key, cancel_background=False)
            current = asyncio.current_task()
            tasks = [
                task
                for task in [*embedding_tasks, *hook_tasks]
                if task is not None and task is not current
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._save_panel_runtime()
            return
        task = None
        embedding_tasks = []
        hook_tasks = []
        async with runtime.state_lock:
            task = runtime.generation_task
            self._invalidate_pending_generation(key)
            embedding_tasks = self._cancel_embedding_tasks(key)
            hook_tasks = self._cancel_hook_tasks(key)
            await self.debounce.discard(key)
            self._reset_session_state(key, invalidate=False, cancel_background=False)
            runtime.touch(self.time_service.time())
        tasks = [candidate for candidate in [task, *embedding_tasks, *hook_tasks] if candidate is not None]
        tasks = [candidate for candidate in tasks if candidate is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._save_panel_runtime()

    async def _cool_session_async(self, session_id: str, minutes: float) -> bool:
        key = self._resolve_session_key(session_id) or session_id
        runtime = self._sessions.get(key)
        if runtime is None:
            return False
        task = None
        hook_tasks: list[asyncio.Task] = []
        async with runtime.state_lock:
            task = runtime.generation_task
            self._invalidate_pending_generation(key)
            await self.debounce.discard(key)
            hook_tasks = self._cancel_hook_tasks(key)
            now = self.time_service.time()
            self.arbiter.trigger_cooling(key, duration_seconds=minutes * 60.0, current_time=now)
            runtime.touch(now)
            self._metric("cooling_triggered")
        tasks = [candidate for candidate in [task, *hook_tasks] if candidate is not None]
        tasks = [candidate for candidate in tasks if candidate is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return True

    async def _uncool_session_async(self, session_id: str) -> bool:
        """Clear deep cooling for one session without wiping DAG or conversation state."""
        key = self._resolve_session_key(session_id) or session_id
        runtime = self._sessions.get(key)
        if runtime is None:
            return self.arbiter.clear_cooling(key)
        async with runtime.state_lock:
            self.arbiter.clear_cooling(key)
            runtime.touch(self.time_service.time())
            self._metric("cooling_cleared")
            self._mark_panel_runtime_dirty()
        return True

    def _clear_pending_gate(self, runtime: Any) -> None:
        if runtime is None:
            return
        runtime._pending_gate_skin = None
        runtime._pending_gate_proactive = None
        runtime._pending_gate_rhythm = None

    def _commit_pending_gate_spoke(self, runtime: Any, session_id: str) -> None:
        """Record quotas after a real send; the gate owns the bookkeeping clock."""
        if runtime is None:
            return
        skin = getattr(runtime, "_pending_gate_skin", None)
        if skin is None:
            return
        try:
            self.decision_gate.note_spoke(
                session_id,
                skin=skin,
                proactive=getattr(runtime, "_pending_gate_proactive", None),
                rhythm=getattr(runtime, "_pending_gate_rhythm", None),
            )
        except Exception:
            pass
        finally:
            self._clear_pending_gate(runtime)

    def _invalidate_pending_generation(self, session_id: str) -> None:
        """Prevent an already-approved response from sending after an admin action."""
        runtime = self._sessions.get(session_id)
        if runtime is None:
            return
        runtime.epoch += 1
        runtime.revision += 1
        runtime.cancel_owned_turns()
        runtime.clear_model_queue()
        runtime.latest_pending = None
        runtime.clear_active_followup_batches()
        runtime.followup_queue.clear()
        runtime.native_trigger_node = None
        runtime.native_vibe_mode = None
        self._clear_native_context(session_id)
        task = runtime.generation_task
        runtime.generation_task = None
        self._in_flight.discard(session_id)
        if task is not None and not task.done():
            task.cancel()

    def _register_web_apis(self) -> None:
        self._web.register()
        self._web_apis_registered = self._web.registered

    async def web_api_overview(self):
        return await self._web.overview()

    async def web_api_sessions(self):
        return await self._web.sessions()

    async def web_api_session(self):
        return await self._web.session()

    async def web_api_cool(self):
        return await self._web.cool()

    async def web_api_reset(self):
        return await self._web.reset()

    async def web_api_presets(self):
        return await self._web.presets()

    async def web_api_apply_preset(self):
        return await self._web.apply_preset()
