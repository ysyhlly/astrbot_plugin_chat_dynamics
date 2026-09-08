"""Session-scoped state used by the Chat Dynamics orchestrator."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from .debounce import DebounceResult
from .graph import ConversationDAG, ConversationNode
from .semantics import semantic_match


@dataclass
class TopicState:
    """State of an active conversation topic cluster within a session."""

    topic_id: str
    message_ids: list[str] = field(default_factory=list)
    participants: set[str] = field(default_factory=set)
    updated_at: float = 0.0
    label: str = ""
    created_at: float = 0.0
    exemplar_messages: list[tuple[str, float]] = field(default_factory=list)
    centroid_vector: Optional[list[float]] = None

    @property
    def last_activity(self) -> float:
        return self.updated_at

    @last_activity.setter
    def last_activity(self, value: float) -> None:
        self.updated_at = float(value)

    @property
    def message_count(self) -> int:
        return len(self.message_ids)

    @property
    def participant_ids(self) -> set[str]:
        return self.participants

    @participant_ids.setter
    def participant_ids(self, value: set[str]) -> None:
        self.participants = value


@dataclass
class RoutingState:
    """Session-isolated routing state managing active topics."""

    topics: dict[str, TopicState] = field(default_factory=dict)
    last_bot_topic_id: Optional[str] = None
    last_topic_id: Optional[str] = None

    @property
    def active_topics(self) -> dict[str, TopicState]:
        return self.topics

    def clear(self) -> None:
        self.topics.clear()
        self.last_bot_topic_id = None
        self.last_topic_id = None

    def prune(
        self,
        dag: Any,
        now: float,
        window_seconds: float = 300.0,
        window_nodes: int = 80,
    ) -> None:
        if dag is None:
            return
        allowed = {
            n.msg_id
            for n in dag.get_recent_nodes(window_nodes)
            if 0 <= now - n.timestamp <= window_seconds
        }
        for key, topic in list(self.topics.items()):
            topic.message_ids = [mid for mid in topic.message_ids if mid in allowed]
            if not topic.message_ids or (now - topic.updated_at > window_seconds):
                del self.topics[key]
                if self.last_topic_id == key:
                    self.last_topic_id = None
                if self.last_bot_topic_id == key:
                    self.last_bot_topic_id = None
                continue
            topic.participants = {
                dag.nodes[mid].user_id for mid in topic.message_ids if mid in dag.nodes
            }
            if topic.message_ids:
                topic.updated_at = max(
                    dag.nodes[mid].timestamp for mid in topic.message_ids if mid in dag.nodes
                )


@dataclass
class PendingTurn:
    result: DebounceResult
    revision: int
    node: Any = None
    vibe_mode: Any = None
    raw_event: Any = None
    addressivity_level: Any = None
    native_pipeline: bool = False
    epoch: int = 0
    # Legacy generation is invalidated by a member stop through the
    # per-user revision, while the session revision/epoch remain shared.
    owner_user_id: str = ""
    owner_revision: int = 0


@dataclass
class FollowupBatch:
    """FIFO tail fragments produced by one native response."""

    fragments: Deque[str] = field(default_factory=deque)
    epoch: int = 0
    trigger_node: Optional[ConversationNode] = None
    vibe_mode: Any = None
    event_id: int = 0
    batch_id: str = ""
    sent_count: int = 0
    # Native tail delivery belongs to the user whose turn produced it.  Keep
    # this identity on the batch so a later explicit request from that same
    # user can interrupt only its own queued/active tail.
    trigger_user_id: str = ""
    # A per-batch token lets an active sender detect invalidation after an
    # await without retaining a process-wide per-user registry.
    delivery_token: int = 0
    invalidated: bool = False

    def remaining(self) -> list[str]:
        return list(self.fragments)

    @property
    def source_user_id(self) -> str:
        """Return the user that generated this batch, including legacy batches."""
        if self.trigger_user_id:
            return str(self.trigger_user_id)
        return str(getattr(self.trigger_node, "user_id", "") or "")

    def invalidate(self) -> None:
        """Invalidate and release all unsent fragments in this batch."""
        self.delivery_token += 1
        self.invalidated = True
        self.fragments.clear()


@dataclass
class SessionRuntime:
    session_key: str
    group_id: str
    umo: str
    bot_id: str = ""
    dag: Optional[ConversationDAG] = None
    last_bot_node: Optional[ConversationNode] = None
    pending_hover: Optional[ConversationNode] = None
    pending_hovers: Deque[ConversationNode] = field(default_factory=lambda: deque(maxlen=8))
    vibe_message_count: int = 0
    revision: int = 0
    # Monotonic per-session identity for flushed turns.  It is deliberately
    # independent of wall-clock time so two flushes at the same virtual time
    # still receive distinct fallback node IDs.
    turn_sequence: int = 0
    epoch: int = 0
    last_activity: float = 0.0
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    owned_send_depth: int = 0
    generation_task: Optional[asyncio.Task] = None
    latest_pending: Optional[PendingTurn] = None
    sent_message_ids: Deque[str] = field(default_factory=lambda: deque(maxlen=512))
    sent_id_set: Set[str] = field(default_factory=set)
    seen_message_ids: Deque[str] = field(default_factory=lambda: deque(maxlen=1024))
    seen_id_set: Set[str] = field(default_factory=set)
    fallback_fingerprints: Deque[str] = field(default_factory=lambda: deque(maxlen=256))
    fallback_fingerprint_set: Set[str] = field(default_factory=set)
    generation_level: Any = None
    # A native result contributes at most three fragments. Bound queued
    # results so a host that delays after_message_sent cannot grow memory
    # without limit; oldest batches are dropped first by deque semantics.
    followup_queue: Deque[FollowupBatch] = field(default_factory=lambda: deque(maxlen=64))
    native_trigger_node: Optional[ConversationNode] = None
    native_vibe_mode: Any = None
    # Native callbacks can overlap while they wait for the per-session send
    # lock.  Keep every callback batch registered by its own token so one
    # callback cannot invalidate another callback merely by starting later.
    active_followup_batches: Dict[int, FollowupBatch] = field(default_factory=dict)
    # Compatibility view for older integrations.  It is informational only;
    # active_followup_batches is the authoritative lifecycle registry.
    active_followup_batch: Optional[FollowupBatch] = None
    followup_delivery_sequence: int = 0
    model_queue: Deque[Any] = field(default_factory=deque)
    model_admission: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(9), repr=False)
    active_model_turn: Any = None
    user_revisions: Dict[str, int] = field(default_factory=dict)
    interaction_state: str = "observing"
    model_diagnostic: Dict[str, Any] = field(default_factory=dict)
    ambient_openings: Deque[float] = field(default_factory=lambda: deque(maxlen=2))
    tool_executions: Deque[dict] = field(default_factory=lambda: deque(maxlen=32))
    last_interlocutor: str = ""
    last_model_send: float = 0.0
    last_length_hint: str = "normal"
    last_delay_scale: float = 1.0
    last_rhythm_action: str = ""
    routing_state: RoutingState = field(default_factory=RoutingState)

    @property
    def last_bot_topic_id(self) -> Optional[str]:
        return self.routing_state.last_bot_topic_id

    @last_bot_topic_id.setter
    def last_bot_topic_id(self, value: Optional[str]) -> None:
        self.routing_state.last_bot_topic_id = value

    def clear_model_queue(self) -> None:
        while self.model_queue:
            self.model_queue.popleft()
            self.model_admission.release()

    @staticmethod
    def _model_turn_owner(item: Any) -> str:
        context = getattr(item, "context", None)
        owner = getattr(context, "author", None) if context is not None else None
        if owner is None:
            owner = getattr(item, "owner_user_id", None)
        return str(owner or "")

    def clear_model_queue_for_user(self, user_id: str) -> int:
        """Drop queued Persona turns owned by one member and release admission."""
        target = str(user_id or "")
        if not target or not self.model_queue:
            return 0
        survivors = deque()
        removed = 0
        while self.model_queue:
            item = self.model_queue.popleft()
            if self._model_turn_owner(item) == target:
                removed += 1
                self.model_admission.release()
            else:
                survivors.append(item)
        self.model_queue.extend(survivors)
        return removed

    @property
    def pending_followup_fragments(self) -> list[str]:
        """Compatibility view for older integrations and tests."""
        fragments: list[str] = []
        for batch in self.followup_queue:
            fragments.extend(batch.remaining())
        return fragments

    @pending_followup_fragments.setter
    def pending_followup_fragments(self, value: list[str]) -> None:
        self.clear_active_followup_batches()
        self.followup_queue.clear()
        if value:
            self.followup_queue.append(FollowupBatch(fragments=deque(str(item) for item in value), epoch=self.epoch))

    def next_followup_delivery_token(self) -> int:
        """Allocate a per-session identity for a native follow-up batch."""
        self.followup_delivery_sequence += 1
        return self.followup_delivery_sequence

    def register_active_followup_batch(self, batch: FollowupBatch) -> int:
        """Register a callback batch before it waits for ``send_lock``."""
        token = int(batch.delivery_token or 0)
        while token <= 0 or (
            token in self.active_followup_batches
            and self.active_followup_batches[token] is not batch
        ):
            token = self.next_followup_delivery_token()
        batch.delivery_token = token
        if token > 0:
            self.followup_delivery_sequence = max(self.followup_delivery_sequence, token)
        self.active_followup_batches[token] = batch
        # Preserve the legacy observation field without using it for guards.
        self.active_followup_batch = batch
        return token

    def unregister_active_followup_batch(self, token: int, batch: FollowupBatch) -> None:
        """Remove exactly ``batch`` from the active registry by its own token."""
        if self.active_followup_batches.get(token) is batch:
            self.active_followup_batches.pop(token, None)
        if self.active_followup_batch is batch:
            self.active_followup_batch = None

    def clear_active_followup_batches(self, *, invalidate: bool = True) -> None:
        """Invalidate and remove all callback batches owned by this runtime."""
        if invalidate:
            for batch in self.followup_queue:
                batch.invalidate()
            for batch in self.active_followup_batches.values():
                batch.invalidate()
        self.active_followup_batches.clear()
        self.active_followup_batch = None

    def invalidate_followups_for_user(self, user_id: str) -> int:
        """Drop queued/active native tails produced by ``user_id`` only."""
        target = str(user_id or "")
        if not target:
            return 0

        invalidated = 0
        survivors: Deque[FollowupBatch] = deque(maxlen=self.followup_queue.maxlen)
        for batch in self.followup_queue:
            if batch.source_user_id == target:
                if not batch.invalidated:
                    batch.invalidate()
                    invalidated += 1
            else:
                survivors.append(batch)
        self.followup_queue.clear()
        self.followup_queue.extend(survivors)

        for active in self.active_followup_batches.values():
            if active.source_user_id == target and not active.invalidated:
                active.invalidate()
                invalidated += 1
        return invalidated

    def remember_seen(self, message_id: str) -> bool:
        """Return False for a duplicate incoming event."""
        if not message_id:
            return True
        if message_id in self.seen_id_set:
            return False
        if len(self.seen_message_ids) == self.seen_message_ids.maxlen:
            self.seen_id_set.discard(self.seen_message_ids[0])
        self.seen_message_ids.append(message_id)
        self.seen_id_set.add(message_id)
        return True

    def remember_fingerprint(self, fingerprint: str, max_items: int = 256) -> bool:
        """Remember a short-lived fallback deduplication fingerprint."""
        if not fingerprint:
            return True
        if fingerprint in self.fallback_fingerprint_set:
            return False
        limit = max(1, min(int(max_items), self.fallback_fingerprints.maxlen or int(max_items)))
        if len(self.fallback_fingerprints) >= limit:
            old = self.fallback_fingerprints.popleft()
            self.fallback_fingerprint_set.discard(old)
        self.fallback_fingerprints.append(fingerprint)
        self.fallback_fingerprint_set.add(fingerprint)
        return True

    def remember_sent(self, message_id: str) -> None:
        if not message_id:
            return
        if len(self.sent_message_ids) == self.sent_message_ids.maxlen:
            self.sent_id_set.discard(self.sent_message_ids[0])
        self.sent_message_ids.append(message_id)
        self.sent_id_set.add(message_id)

    def expire_hovers(self, now: float, ttl: float = 120.0) -> None:
        alive = [node for node in self.pending_hovers if 0.0 <= now - node.timestamp <= ttl]
        self.pending_hovers.clear()
        self.pending_hovers.extend(alive)
        self.pending_hover = self.pending_hovers[-1] if self.pending_hovers else None

    def remember_hover(self, node: ConversationNode, now: float, ttl: float = 120.0) -> None:
        self.expire_hovers(now, ttl=ttl)
        self.pending_hovers.append(node)
        self.pending_hover = node

    def clear_hovers(self) -> None:
        self.pending_hovers.clear()
        self.pending_hover = None

    def clear_hovers_for_user(self, user_id: str) -> None:
        """Remove only safe-hover turns authored by ``user_id``."""
        target = str(user_id or "")
        if not target:
            return
        remaining = [
            node
            for node in self.pending_hovers
            if str(getattr(node, "user_id", "") or "") != target
        ]
        self.pending_hovers.clear()
        self.pending_hovers.extend(remaining)
        self.pending_hover = self.pending_hovers[-1] if self.pending_hovers else None

    def reset_conversation_state(self) -> None:
        self.routing_state.clear()
        self.clear_model_queue()
        self.user_revisions.clear()
        self.interaction_state = "observing"
        self.model_diagnostic.clear()
        self.ambient_openings.clear()
        self.last_interlocutor = ""
        self.last_model_send = 0.0
        self.last_length_hint = "normal"
        self.last_delay_scale = 1.0
        self.last_rhythm_action = ""
        self.last_bot_node = None
        self.clear_hovers()
        self.vibe_message_count = 0
        self.latest_pending = None
        self.generation_level = None
        self.clear_active_followup_batches()
        self.followup_queue.clear()
        self.native_trigger_node = None
        self.native_vibe_mode = None
        self.sent_message_ids.clear()
        self.sent_id_set.clear()
        self.seen_message_ids.clear()
        self.seen_id_set.clear()
        self.fallback_fingerprints.clear()
        self.fallback_fingerprint_set.clear()
        if self.dag is not None:
            self.dag.reset()

    def touch(self, timestamp: float) -> None:
        self.last_activity = max(self.last_activity, float(timestamp))

    def invalidate(self) -> int:
        self.clear_model_queue()
        self.epoch += 1
        self.revision += 1
        self.latest_pending = None
        self.clear_active_followup_batches()
        self.followup_queue.clear()
        self.native_trigger_node = None
        self.native_vibe_mode = None
        return self.epoch


class SessionRegistry:
    """UMO-keyed store for live SessionRuntime objects."""

    def __init__(
        self,
        time_service: Any,
        *,
        max_nodes: int = 500,
        ttl_seconds: float = 3600.0,
        semantic_match_fn: Any = None,
        max_sessions: int = 1000,
    ) -> None:
        self.time_service = time_service
        self.max_nodes = max_nodes
        self.ttl_seconds = ttl_seconds
        self.semantic_match_fn = semantic_match_fn or semantic_match
        self.max_sessions = max(1, int(max_sessions))
        self.runtimes: Dict[str, SessionRuntime] = {}
        self.group_keys: Dict[str, Set[str]] = {}
        self.dags: Dict[str, ConversationDAG] = {}

    def bind_time_service(self, time_service: Any) -> None:
        self.time_service = time_service
        for dag in self.dags.values():
            dag.time_service = time_service

    def bind_semantic_match(self, semantic_match_fn: Any) -> None:
        self.semantic_match_fn = semantic_match_fn or semantic_match
        for dag in self.dags.values():
            dag.semantic_match_fn = self.semantic_match_fn

    def get(self, session_key: str) -> Optional[SessionRuntime]:
        return self.runtimes.get(session_key)

    def get_or_create(
        self,
        session_key: str,
        *,
        group_id: Optional[str] = None,
        umo: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> SessionRuntime:
        key = str(session_key).strip()
        if not key:
            raise ValueError("session_key must not be empty")
        runtime = self.runtimes.get(key)
        if runtime is None:
            runtime = SessionRuntime(
                session_key=key,
                group_id=str(group_id or key),
                umo=str(umo or key),
                bot_id=str(bot_id or ""),
            )
            runtime.dag = ConversationDAG(
                session_id=key,
                max_nodes=self.max_nodes,
                ttl_seconds=self.ttl_seconds,
                time_service=self.time_service,
                semantic_match_fn=self.semantic_match_fn,
            )
            self.runtimes[key] = runtime
            self.dags[key] = runtime.dag
        if group_id:
            old_group = runtime.group_id
            runtime.group_id = str(group_id)
            if old_group != runtime.group_id:
                old_keys = self.group_keys.get(old_group)
                if old_keys is not None:
                    old_keys.discard(key)
                    if not old_keys:
                        self.group_keys.pop(old_group, None)
        if umo:
            runtime.umo = str(umo)
        if bot_id:
            runtime.bot_id = str(bot_id)
        self.group_keys.setdefault(runtime.group_id, set()).add(key)
        return runtime

    def capacity_reached(self) -> bool:
        return len(self.runtimes) >= self.max_sessions

    def oldest_idle(self, active_keys: Optional[Set[str]] = None) -> Optional[str]:
        active = active_keys or set()
        candidates = [runtime for key, runtime in self.runtimes.items() if key not in active]
        if not candidates:
            return None
        return min(candidates, key=lambda runtime: runtime.last_activity).session_key

    def resolve(self, identifier: str) -> Optional[str]:
        candidate = str(identifier or "").strip()
        if not candidate:
            return None
        if candidate in self.runtimes or candidate in self.dags:
            return candidate
        matches = self.group_keys.get(candidate, set())
        return next(iter(matches)) if len(matches) == 1 else None

    def drop(self, session_key: str) -> Optional[SessionRuntime]:
        runtime = self.runtimes.pop(session_key, None)
        self.dags.pop(session_key, None)
        if runtime is not None:
            keys = self.group_keys.get(runtime.group_id)
            if keys is not None:
                keys.discard(session_key)
                if not keys:
                    self.group_keys.pop(runtime.group_id, None)
        return runtime

    def clear(self) -> None:
        self.runtimes.clear()
        self.group_keys.clear()
        self.dags.clear()

    def known_ids(self) -> Set[str]:
        return set(self.runtimes) | set(self.dags)
