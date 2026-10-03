"""Read semantic topic identity without borrowing explicit conversation threads."""

from collections.abc import Mapping
from typing import Any


def node_topic_id(node: Any) -> str:
    """Routing is authoritative, including an intentionally empty topic.

    Older nodes without routing may still carry the top-level topic mirror.
    A thread ID only identifies explicit message ancestry and is never a topic.
    """
    metadata = getattr(node, "metadata", None)
    if not isinstance(metadata, Mapping):
        return ""
    if "routing" in metadata:
        routing = metadata["routing"]
        value = routing.get("topic_id") if isinstance(routing, Mapping) else getattr(routing, "topic_id", None)
    else:
        value = metadata.get("topic_id")
    return str(value) if value else ""


def confirmed_topic_id(node: Any) -> str:
    """An independent committed topic can exclude unrelated interruptions."""
    routing = getattr(node, "metadata", {}).get("routing", {})
    if isinstance(routing, Mapping) and (routing.get("topic_ambiguous")
            or routing.get("topic_status") in {"unknown", "pending", "unformed"}):
        return ""
    topic = node_topic_id(node)
    # Minimal/legacy records have no separate confidence; their topic remains
    # usable for scoped lookup, but cannot exclude interruptions below.
    return topic
