"""Keep bounded image sources available to the speaker's next text turn."""

from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from . import member_boundary
from .deferred_media import preserve_event_media
from .platform_bridge import iter_message_components

WINDOW_SECONDS = 120
MAX_ENTRIES = 8
MAX_IMAGES = 2


@dataclass(frozen=True)
class RecentImage:
    node: Any
    event: Any


class RecentImages:
    def __init__(self):
        self.entries = OrderedDict()

    def prune(self, now):
        """Drop expired references; an in-flight reader keeps its own lease."""
        for mid, source in tuple(self.entries.items()):
            if not 0 <= now - source.node.timestamp <= WINDOW_SECONDS:
                self.entries.pop(mid, None)

    def clear(self):
        self.entries.clear()

    def discard(self, user_id):
        for mid, source in tuple(self.entries.items()):
            if source.node.user_id == user_id:
                self.entries.pop(mid, None)

    def remember(self, event, node, archive, now):
        """Copy direct images independently of the original turn's cleanup.

        Args:
            event: Accepted source event, still owning its temporary files.
            node: Canonical source node in this session's DAG.
            archive: Shared bounded attachment store.
            now: Current monotonic time.
        """
        self.prune(now)
        # Quoted media belongs to its quoted author, not this speaker.
        images = [
            copy.deepcopy(part) for part in iter_message_components(event) if type(part).__name__.lower() == "image"
        ][:MAX_IMAGES]
        if not images or not node.user_id:
            return
        owned = copy.copy(event)
        owned.message_obj = copy.copy(event.message_obj)
        owned.message_obj.message = images
        if hasattr(event, "_extras"):
            owned._extras = dict(event._extras)
        if hasattr(event, "_temporary_local_files"):
            owned._temporary_local_files = []
        # A shallow event copy must not inherit another owner's finalizer.
        for attr in ("_chat_dynamics_media_cleanup", "_chat_dynamics_media_directory", "_chat_dynamics_media_archive"):
            if hasattr(owned, attr):
                delattr(owned, attr)
        owned = preserve_event_media(owned, archive=archive, session=event.unified_msg_origin)
        self.entries[node.msg_id] = RecentImage(node, owned)
        self.entries.move_to_end(node.msg_id)
        while len(self.entries) > MAX_ENTRIES:
            self.entries.popitem(last=False)

    def select(self, runtime, turn, event, host, now):
        """Resolve only the immediately preceding same-speaker image message.

        Args:
            runtime: Owning session and live DAG.
            turn: Frozen, boundary-filtered text turn.
            event: Current trigger event.
            host: Current plugin, used to recheck source boundaries.
            now: Current monotonic time.

        Returns:
            At most one source message containing at most two images.
        """
        self.prune(now)
        if runtime.scope_kind != "group" or not turn.messages or turn.visible_node_ids is None:
            return ()
        if any(message.attachments for message in turn.messages):
            return ()
        if getattr(event, "_chat_dynamics_strip_quote_media", False):
            return ()
        # An explicit reply/mention has its own source; do not add a different
        # adjacent image merely because the same person posted it.
        if any(
            message.reply_to or set(message.mentioned_users) - {turn.bot_id, turn.author} for message in turn.messages
        ):
            return ()
        current_ids = {message.message_id for message in turn.messages}
        preceding = [
            node
            for node in runtime.dag.get_recent_nodes(limit=0)
            if node.msg_id in turn.visible_node_ids and node.msg_id not in current_ids and node.user_id == turn.author
        ]
        if not preceding:
            return ()
        node = preceding[-1]
        allowed_ids = {message.message_id for message in (*turn.background, *turn.nearby_context)}
        source = self.entries.get(node.msg_id)
        if source is None or source.node is not node or node.msg_id not in allowed_ids:
            return ()
        current_topics = {
            message.semantics.topic_id
            for message in turn.messages
            if message.semantics
            and message.semantics.topic_id
            and not message.semantics.topic_ambiguous
            and message.semantics.topic_confidence >= 0.8
        }
        previous = next(
            message for message in (*turn.background, *turn.nearby_context) if message.message_id == node.msg_id
        )
        if (
            current_topics
            and previous.semantics
            and previous.semantics.topic_id
            and not previous.semantics.topic_ambiguous
            and previous.semantics.topic_confidence >= 0.8
            and previous.semantics.topic_id not in current_topics
        ):
            return ()
        if getattr(source.event, "unified_msg_origin", None) != turn.session_key:
            return ()
        token = getattr(source.event, "_chat_dynamics_boundary_token", None)
        policy, current_token, state = member_boundary.peek(host, event, turn.author)
        if (
            not state.available
            and (member_boundary.fail_closed(state) or state.service is not None)
            or member_boundary.blocked(policy, purpose="role_input")
            or token != current_token
        ):
            return ()
        frames, state = member_boundary.filter_frames(
            host,
            event,
            [
                {
                    "frame_id": node.msg_id,
                    "author_uid": node.user_id,
                    "source_uids": [node.user_id],
                    "provenance_complete": True,
                    "kind": "event",
                    "text": node.text,
                }
            ],
            purpose="role_input",
        )
        if not state.available and (member_boundary.fail_closed(state) or state.service is not None):
            return ()
        return (source,) if any(isinstance(row, dict) and row.get("frame_id") == node.msg_id for row in frames) else ()
