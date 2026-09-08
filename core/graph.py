"""Conversation DAG (Directed Acyclic Graph) for AstrBot Group Chat Dynamics.

Tracks concurrent, interleaved group chat messages, models reply relationships,
user mentions, and reconstructs threaded sub-conversations.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .semantics import SemanticMatch, semantic_match

logger = logging.getLogger("astrbot_plugin_chat_dynamics.graph")


@dataclass
class ConversationNode:
    """Represents a discrete message turn in the conversation DAG."""

    msg_id: str
    user_id: str
    text: str
    timestamp: float
    reply_to_id: Optional[str] = None
    mentioned_users: List[str] = field(default_factory=list)
    parent_ids: Set[str] = field(default_factory=set)
    child_ids: Set[str] = field(default_factory=set)
    thread_id: str = ""
    edge_kinds: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_reply(self) -> bool:
        """Returns True if this message explicitly replies to another."""
        return bool(self.reply_to_id)

    def has_mentions(self) -> bool:
        """Returns True if this message explicitly mentions any user."""
        return bool(self.mentioned_users)


class ConversationDAG:
    """Directed Acyclic Graph storing multi-party message relationships."""

    def __init__(
        self,
        session_id: str = "default",
        max_nodes: int = 500,
        ttl_seconds: float = 3600.0,
        time_service: Optional[Any] = None,
        semantic_match_fn: Optional[Callable[[str, str], SemanticMatch]] = None,
    ):
        self.session_id: str = session_id
        self.max_nodes: int = max_nodes
        self.ttl_seconds: float = ttl_seconds
        self.time_service = time_service
        self.semantic_match_fn = semantic_match_fn or semantic_match
        self.nodes: Dict[str, ConversationNode] = {}
        # Chronological ordering of message IDs
        self.chronological_ids: List[str] = []
        # Children whose explicit reply target has not arrived yet.  Several
        # adapters can deliver quoted messages out of order after reconnects.
        self._waiting_children: Dict[str, Set[str]] = {}

    def _now(self, timestamp: Optional[float] = None) -> float:
        if timestamp is not None:
            return timestamp
        if self.time_service is not None:
            return float(self.time_service.time())
        return time.time()

    def add_message(
        self,
        msg_id: str,
        user_id: str,
        text: str,
        timestamp: Optional[float] = None,
        reply_to_id: Optional[str] = None,
        mentioned_users: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ConversationNode:
        """Adds a new message node to the DAG and links edges.

        Args:
            msg_id: Unique message identifier.
            user_id: Sender identifier.
            text: Message text.
            timestamp: Monotonic or epoch timestamp (defaults to time.time()).
            reply_to_id: Optional message_id being quoted or replied to.
            mentioned_users: Optional list of user IDs explicitly @mentioned.
            metadata: Optional arbitrary payload.

        Returns:
            The newly created and linked ConversationNode.
        """
        # Platform adapters may redeliver the same event after a reconnect.  A
        # message id is the identity of a node, so repeated delivery must be
        # idempotent instead of replacing the node and leaving stale edges.
        existing = self.nodes.get(msg_id)
        if existing is not None:
            return existing

        ts = self._now(timestamp)
        mentions = list(mentioned_users) if mentioned_users else []
        meta = dict(metadata) if metadata else {}
        meta.setdefault("topic_id", "")

        node = ConversationNode(
            msg_id=msg_id,
            user_id=user_id,
            text=text,
            timestamp=ts,
            reply_to_id=reply_to_id,
            mentioned_users=mentions,
            thread_id=msg_id,
            metadata=meta,
        )

        self.nodes[msg_id] = node
        self.chronological_ids.append(msg_id)

        inherited = False
        if reply_to_id:
            if reply_to_id in self.nodes:
                if self._link_parent(msg_id, reply_to_id, kind="reply"):
                    self._adopt_thread(msg_id, self.nodes[reply_to_id].thread_id)
                    inherited = True
            else:
                self._waiting_children.setdefault(reply_to_id, set()).add(msg_id)

        if not inherited:
            mention_parent = self._mention_parent(node)
            if mention_parent is not None and self._link_parent(msg_id, mention_parent.msg_id, kind="mention"):
                # Mentions create a mention edge for addressee resolution, but do NOT merge thread_id.
                inherited = True

        # Reconcile children that arrived before this parent.
        for child_id in list(self._waiting_children.pop(msg_id, set())):
            if self._link_parent(child_id, msg_id, kind="reply"):
                self._adopt_thread(child_id, node.thread_id)

        # Automatic pruning if cap exceeded
        if len(self.nodes) > self.max_nodes:
            self.prune(current_time=ts)

        return node

    def _link_parent(self, child_id: str, parent_id: str, kind: str = "reply") -> bool:
        """Link parent -> child while preserving the DAG invariant."""
        child = self.nodes.get(child_id)
        parent = self.nodes.get(parent_id)
        if child is None or parent is None or child_id == parent_id:
            return False

        # Adding parent -> child would create a cycle if parent is already
        # reachable from child by following child edges.
        stack = [child_id]
        visited: Set[str] = set()
        while stack:
            current = stack.pop()
            if current == parent_id:
                return False
            if current in visited:
                continue
            visited.add(current)
            current_node = self.nodes.get(current)
            if current_node is not None:
                stack.extend(current_node.child_ids - visited)

        child.parent_ids.add(parent_id)
        parent.child_ids.add(child_id)
        child.edge_kinds[parent_id] = kind
        return True

    def _adopt_thread(self, node_id: str, thread_id: str) -> None:
        """Propagate thread_id downward through node_id and all its structural descendants.

        Uses downward DFS traversal along child structural edges ('reply', 'fragment',
        'inferred_reply') to prevent mutating parents or siblings sharing the old thread.
        """
        child = self.nodes.get(node_id)
        if child is None or not thread_id or child.thread_id == thread_id:
            return

        stack = [node_id]
        visited: Set[str] = set()
        while stack:
            curr_id = stack.pop()
            if curr_id in visited:
                continue
            visited.add(curr_id)
            curr_node = self.nodes.get(curr_id)
            if curr_node is not None:
                curr_node.thread_id = thread_id
                for c_id in sorted(curr_node.child_ids):
                    if c_id in self.nodes and c_id not in visited:
                        edge_kind = self.nodes[c_id].edge_kinds.get(curr_id)
                        if edge_kind in {"reply", "fragment", "inferred_reply"}:
                            stack.append(c_id)

    def _mention_parent(self, node: ConversationNode, max_age: float = 180.0) -> Optional[ConversationNode]:
        best: Optional[ConversationNode] = None
        for user_id in node.mentioned_users:
            candidate = self._latest_by_user(user_id, before=node.timestamp, max_age=max_age)
            if candidate is None or candidate.msg_id == node.msg_id:
                continue
            if best is None or candidate.timestamp > best.timestamp:
                best = candidate
        return best

    def _latest_by_user(
        self, user_id: str, *, before: float, max_age: float
    ) -> Optional[ConversationNode]:
        best: Optional[ConversationNode] = None
        for other in self.nodes.values():
            if other.user_id != user_id:
                continue
            if other.timestamp >= before:
                continue
            if before - other.timestamp > max_age:
                continue
            if best is None or other.timestamp > best.timestamp:
                best = other
        return best

    def link_inferred_reply(
        self,
        child_id: str,
        parent_id: str,
        confidence: float = 0.0,
        reason: str = "",
    ) -> bool:
        """Promote an inferred conversational parent link into the DAG.

        Validates self-loops, DAG cycles, temporal causality, confidence bounds,
        and platform reply priority before atomically linking and adopting thread.

        Args:
            child_id: Message ID of the responding child turn.
            parent_id: Message ID of the inferred parent turn.
            confidence: Inference confidence score [0.0, 1.0].
            reason: Diagnostic reason string.

        Returns:
            True if successfully linked; False if validation failed or cycle detected.
        """
        # 1. Self-loop and non-empty string checks
        if not child_id or not parent_id or child_id == parent_id:
            logger.warning(
                "Rejected link_inferred_reply: self-loop or invalid ID (child=%s, parent=%s)",
                child_id,
                parent_id,
            )
            return False

        # 2. Node existence check
        child = self.nodes.get(child_id)
        parent = self.nodes.get(parent_id)
        if child is None or parent is None:
            logger.warning(
                "Rejected link_inferred_reply: missing node (child=%s, parent=%s)",
                child_id,
                parent_id,
            )
            return False

        # 3. Temporal causality check (causes precede effects; 50ms tolerance for clock jitter)
        if child.timestamp < parent.timestamp - 0.05:
            logger.warning(
                "Rejected link_inferred_reply: causal inversion (child=%.3f < parent=%.3f)",
                child.timestamp,
                parent.timestamp,
            )
            return False

        # 4. Confidence validation
        if not isinstance(confidence, (int, float)) or math.isnan(confidence) or math.isinf(confidence):
            logger.warning("Rejected link_inferred_reply: non-finite confidence (%s)", confidence)
            return False
        if not (0.0 <= float(confidence) <= 1.0):
            logger.warning("Rejected link_inferred_reply: confidence out of bounds [0, 1] (%s)", confidence)
            return False

        # 5. Platform reply priority: explicit platform replies cannot be overridden by inference
        if child.reply_to_id or any(kind == "reply" for kind in child.edge_kinds.values()):
            logger.debug(
                "Rejected link_inferred_reply: explicit platform reply already exists on node %s",
                child_id,
            )
            return False

        # 6. Cycle detection: parent_id must not be reachable from child_id via child edges
        stack = [child_id]
        visited: Set[str] = set()
        while stack:
            curr = stack.pop()
            if curr == parent_id:
                logger.warning(
                    "Rejected link_inferred_reply: cycle detected (%s -> ... -> %s)",
                    child_id,
                    parent_id,
                )
                return False
            if curr in visited:
                continue
            visited.add(curr)
            curr_node = self.nodes.get(curr)
            if curr_node is not None:
                stack.extend(curr_node.child_ids - visited)

        # 7. Atomic replacement of previous inferred_reply or semantic edges on child
        for existing_parent_id, kind in list(child.edge_kinds.items()):
            if kind in {"inferred_reply", "semantic"} and existing_parent_id != parent_id:
                child.parent_ids.discard(existing_parent_id)
                child.edge_kinds.pop(existing_parent_id, None)
                if existing_parent_id in self.nodes:
                    self.nodes[existing_parent_id].child_ids.discard(child_id)

        # 8. Create edge and attach edge metadata
        child.parent_ids.add(parent_id)
        parent.child_ids.add(child_id)
        child.edge_kinds[parent_id] = "inferred_reply"

        clean_reason = str(reason).strip() or "inferred_reply"
        child.metadata.setdefault("edge_metadata", {})[parent_id] = {
            "kind": "inferred_reply",
            "confidence": float(confidence),
            "reason": clean_reason,
        }
        child.metadata["inferred_parent_id"] = parent_id
        child.metadata["inferred_parent_confidence"] = float(confidence)
        child.metadata["inferred_parent_reason"] = clean_reason
        child.metadata["inferred_confidence"] = float(confidence)
        child.metadata["inferred_reason"] = clean_reason

        routing = child.metadata.get("routing")
        if isinstance(routing, dict):
            routing["parent_message_id"] = parent_id
            routing["parent_confidence"] = float(confidence)

        # 9. Downward thread adoption
        self._adopt_thread(child_id, parent.thread_id)
        return True

    def unlink_inferred_reply(self, child_id: str, parent_id: Optional[str] = None) -> bool:
        """Remove an inferred reply edge from child and reset thread_id if no structural parents remain."""
        child = self.nodes.get(child_id)
        if child is None:
            return False

        if parent_id is not None:
            targets = [parent_id]
        else:
            targets = [p for p, k in list(child.edge_kinds.items()) if k in {"inferred_reply", "semantic"}]

        removed = False
        for p_id in targets:
            if child.edge_kinds.get(p_id) in {"inferred_reply", "semantic"}:
                child.parent_ids.discard(p_id)
                child.edge_kinds.pop(p_id, None)
                p_node = self.nodes.get(p_id)
                if p_node is not None:
                    p_node.child_ids.discard(child_id)
                if child.metadata.get("inferred_parent_id") == p_id or parent_id is None:
                    child.metadata.pop("inferred_parent_id", None)
                    child.metadata.pop("inferred_parent_confidence", None)
                    child.metadata.pop("inferred_parent_reason", None)
                    child.metadata.pop("inferred_confidence", None)
                    child.metadata.pop("inferred_reason", None)
                routing = child.metadata.get("routing")
                if isinstance(routing, dict) and routing.get("parent_message_id") == p_id:
                    routing["parent_message_id"] = ""
                    routing["parent_confidence"] = 0.0
                edge_meta = child.metadata.get("edge_metadata")
                if isinstance(edge_meta, dict):
                    edge_meta.pop(p_id, None)
                removed = True

        if removed and not any(k in {"reply", "fragment", "inferred_reply"} for k in child.edge_kinds.values()):
            self._adopt_thread(child_id, child.msg_id)

        return removed

    def link_related(self, child_id: str, parent_id: str, kind: str = "fragment") -> bool:
        """Add a non-platform relationship, such as fragments in one turn."""
        if not self._link_parent(child_id, parent_id, kind=kind):
            return False
        parent = self.nodes.get(parent_id)
        if parent is not None:
            self._adopt_thread(child_id, parent.thread_id)
        return True

    def get_node(self, msg_id: str) -> Optional[ConversationNode]:
        """Fetches node by message id."""
        return self.nodes.get(msg_id)

    def get_thread_context(
        self, leaf_msg_id: str, max_depth: int = 10, max_nodes: int = 20
    ) -> List[ConversationNode]:
        """Reconstruct the ancestor chain and nearby sibling branches.

        Walks parents first, then includes children of those ancestors that
        share the leaf thread or arrived close in time. Same-thread nodes
        without an explicit parent hop are also eligible.

        Args:
            leaf_msg_id: The trigger or most recent message id.
            max_depth: Maximum hops upward in DAG.
            max_nodes: Maximum number of nodes in returned context.

        Returns:
            List of ConversationNode instances sorted chronologically.
        """
        leaf = self.nodes.get(leaf_msg_id)
        if leaf is None:
            return []

        visited: Set[str] = set()
        queue: List[Tuple[str, int]] = [(leaf_msg_id, 0)]
        collected_ids: Set[str] = set()

        while queue and len(collected_ids) < max_nodes:
            curr_id, depth = queue.pop(0)
            if curr_id in visited:
                continue
            visited.add(curr_id)
            collected_ids.add(curr_id)

            if depth >= max_depth:
                continue

            node = self.nodes.get(curr_id)
            if not node:
                continue

            for p_id in sorted(node.parent_ids):
                if p_id in self.nodes and p_id not in visited:
                    queue.append((p_id, depth + 1))

        sibling_ids: List[str] = []
        for collected_id in list(collected_ids):
            node = self.nodes.get(collected_id)
            if node is None:
                continue
            for parent_id in node.parent_ids:
                parent = self.nodes.get(parent_id)
                if parent is None:
                    continue
                for child_id in parent.child_ids:
                    if child_id in collected_ids or child_id not in self.nodes:
                        continue
                    child = self.nodes[child_id]
                    close = abs(child.timestamp - leaf.timestamp) <= 180.0
                    same_thread = bool(leaf.thread_id) and child.thread_id == leaf.thread_id
                    if close or same_thread:
                        sibling_ids.append(child_id)

        for sibling_id in sibling_ids:
            if len(collected_ids) >= max_nodes:
                break
            collected_ids.add(sibling_id)

        if leaf.thread_id:
            for node in self.nodes.values():
                if len(collected_ids) >= max_nodes:
                    break
                if node.thread_id != leaf.thread_id or node.msg_id in collected_ids:
                    continue
                if abs(node.timestamp - leaf.timestamp) > 180.0:
                    continue
                collected_ids.add(node.msg_id)

        result = [self.nodes[m_id] for m_id in collected_ids if m_id in self.nodes]
        result.sort(key=lambda n: (n.timestamp, self.chronological_ids.index(n.msg_id) if n.msg_id in self.chronological_ids else 0))
        return result[-max_nodes:] if len(result) > max_nodes else result

    def get_context_for_message(
        self,
        leaf_msg_id: str,
        *,
        bot_id: str = "",
        max_depth: int = 8,
        max_nodes: int = 15,
        max_age_seconds: float = 180.0,
    ) -> List[ConversationNode]:
        """Return thread-safe context without falling back to the whole room.

        Explicit/derived parent chains win.  For a new root message, only the
        same participant, the bot, and messages mentioning either are eligible
        as nearby context candidates.
        """
        thread = self.get_thread_context(leaf_msg_id, max_depth=max_depth, max_nodes=max_nodes)
        if len(thread) > 1:
            return thread
        leaf = self.nodes.get(leaf_msg_id)
        if leaf is None:
            return []

        candidates: List[ConversationNode] = []
        for node in self.get_recent_nodes(limit=max_nodes * 4):
            if node.timestamp > leaf.timestamp or (leaf.timestamp - node.timestamp) > max_age_seconds:
                continue
            mentions = set(node.mentioned_users)
            same_party = node.user_id == leaf.user_id or bool(bot_id and node.user_id == bot_id)
            related_mention = leaf.user_id in mentions or bool(bot_id and bot_id in mentions)
            same_turn = bool(
                leaf.metadata.get("turn_id")
                and node.metadata.get("turn_id") == leaf.metadata.get("turn_id")
            )
            same_thread = bool(leaf.thread_id) and node.thread_id == leaf.thread_id
            if node.msg_id == leaf.msg_id or same_party or related_mention or same_turn or same_thread:
                candidates.append(node)
        candidates.sort(key=lambda n: (n.timestamp, self.chronological_ids.index(n.msg_id)))
        return candidates[-max_nodes:]

    def get_recent_nodes(self, limit: int = 20) -> List[ConversationNode]:
        """Returns the most recent messages in chronological order."""
        selected_ids = self.chronological_ids[-limit:] if limit > 0 else self.chronological_ids
        order = {m_id: index for index, m_id in enumerate(self.chronological_ids)}
        result = [self.nodes[m_id] for m_id in selected_ids if m_id in self.nodes]
        result.sort(key=lambda node: (node.timestamp, order.get(node.msg_id, 0)))
        return result

    def last_timestamp(self) -> float:
        if not self.chronological_ids:
            return 0.0
        node = self.nodes.get(self.chronological_ids[-1])
        return float(node.timestamp) if node is not None else 0.0

    def prune(
        self, max_nodes: Optional[int] = None, ttl_seconds: Optional[float] = None, current_time: Optional[float] = None
    ) -> int:
        """Evicts expired nodes and enforces maximum node limits to bound memory.

        Returns:
            Count of pruned nodes.
        """
        cap = max_nodes if max_nodes is not None else self.max_nodes
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_seconds
        now = self._now(current_time)

        evicted: Set[str] = set()

        # 1. Evict by TTL
        if ttl > 0:
            for m_id, node in self.nodes.items():
                if (now - node.timestamp) > ttl:
                    evicted.add(m_id)

        # 2. Evict oldest if exceeding capacity
        current_active = [m_id for m_id in self.chronological_ids if m_id not in evicted]
        if len(current_active) > cap:
            overflow = len(current_active) - cap
            evicted.update(current_active[:overflow])

        # Execute removal and clean up edge references
        for m_id in evicted:
            node = self.nodes.pop(m_id, None)
            if node:
                for p_id in node.parent_ids:
                    if p_id in self.nodes:
                        self.nodes[p_id].child_ids.discard(m_id)
                for c_id in node.child_ids:
                    if c_id in self.nodes:
                        self.nodes[c_id].parent_ids.discard(m_id)
                        self.nodes[c_id].edge_kinds.pop(m_id, None)

        self.chronological_ids = [m_id for m_id in self.chronological_ids if m_id in self.nodes]
        self._waiting_children = {
            parent_id: {child_id for child_id in children if child_id in self.nodes}
            for parent_id, children in self._waiting_children.items()
            if parent_id not in self.nodes
        }
        self._waiting_children = {key: value for key, value in self._waiting_children.items() if value}
        return len(evicted)

    def reset(self) -> None:
        self.nodes.clear()
        self.chronological_ids.clear()
        self._waiting_children.clear()
