"""One bounded, read-only context completion before the final reply decision."""

import asyncio
import json
import time
from dataclasses import replace

from . import member_boundary
from .context_retrieval import ContextEvidence, clip_text
from .member_identity import quoted_identity
from .message_semantics import describe_message
from .topic_identity import confirmed_topic_id
from .turn_decision import MessageSnapshot

TIMEOUT_SECONDS = 8.0


async def complete_context(plugin, runtime, item, mode):
    """Retrieve bounded source material without changing message ownership.

    Args:
        plugin: Live plugin and its public narrative coordinator.
        runtime: Owning session's graph.
        item: Immutable incoming turn with its original source watermark.
        mode: Model-selected recent, topic or memory lookup.

    Returns:
        A replacement turn carrying completion status and any validated sources.
    """
    if item.observations.get("context_completion", {}).get("attempted"):
        return item
    started = time.monotonic()
    info = {"attempted": True, "mode": mode, "status": "empty", "added_messages": 0}
    turn = item.context
    candidate = item
    try:

        async def retrieve():
            nonlocal candidate
            if not await plugin.persona_engine.valid_async(runtime, item):
                raise member_boundary.BoundaryCancelled("context_completion_invalidated")
            topics = {
                confirmed_topic_id(runtime.dag.get_node(m.message_id))
                for m in turn.messages
                if runtime.dag.get_node(m.message_id) is not None
            }
            topics.discard("")
            cutoff = (
                turn.snapshot_at
                if turn.snapshot_at is not None
                else max((m.timestamp or 0 for m in turn.messages), default=0)
            )
            known = {m.message_id for m in (*turn.messages, *turn.background)}
            additions = []
            budget = 3000
            for node in reversed(runtime.dag.get_recent_nodes(limit=0)):
                if (
                    item.context_node_ids is None
                    or node.msg_id not in item.context_node_ids
                    or node.msg_id in known
                    or not 0 <= cutoff - node.timestamp <= 1800
                ):
                    continue
                owner = str(node.user_id)
                if owner == turn.bot_id and not (
                    node.metadata.get("dialogue_delivered") is True
                    or node.msg_id in getattr(runtime, "sent_id_set", ())
                ):
                    continue
                related = owner == turn.author or (
                    owner == turn.bot_id and node.metadata.get("trigger_user_id") == turn.author
                )
                if mode == "topic" and topics:
                    related = confirmed_topic_id(node) in topics
                if not related:
                    continue
                text = clip_text(node.text, min(400, budget))
                semantics = describe_message(node, runtime.dag, turn.bot_id)
                additions.append(
                    MessageSnapshot(
                        node.msg_id,
                        owner,
                        text,
                        node.reply_to_id or "",
                        semantics=semantics,
                        source_text=node.text,
                        timestamp=node.timestamp,
                        mentioned_users=tuple(node.metadata.get("actual_mentions", node.mentioned_users) or ()),
                        author_name=str(node.metadata.get("sender_name") or ""),
                        platform=str(node.metadata.get("sender_platform") or ""),
                        quoted_author_name=str((quoted_identity(node, runtime.dag) or {}).get("display_name") or ""),
                        context_evidence=(
                            ContextEvidence(
                                "supplemental_context", turn.messages[-1].message_id, 1.0, ("model_requested_context",)
                            ),
                        ),
                    )
                )
                budget -= len(text)
                if len(additions) >= 8 or budget <= 0:
                    break
            frames = []
            for message in additions:
                source_node = runtime.dag.get_node(message.message_id)
                recipient = (
                    str(source_node.metadata.get("trigger_user_id") or "") if message.author == turn.bot_id else ""
                )
                quoted = str(getattr(message.semantics, "quoted_author_id", "") or "")
                frames.append(
                    {
                        "frame_id": message.message_id,
                        "author_uid": message.author,
                        "source_uids": list(dict.fromkeys(uid for uid in (message.author, recipient, quoted) if uid)),
                        "provenance_complete": bool(
                            message.author and (message.author != turn.bot_id or recipient or quoted)
                        ),
                        "kind": "event",
                        "text": message.text,
                    }
                )
            filtered, boundary = member_boundary.filter_frames(plugin, item.events[-1], frames, purpose="role_input")
            if not boundary.available and (member_boundary.fail_closed(boundary) or boundary.service is not None):
                raise member_boundary.BoundaryCancelled("context_completion_boundary_unavailable")
            allowed = {(f.get("frame_id"), f.get("text")) for f in filtered if isinstance(f, dict)}
            additions = [m for m in additions if (m.message_id, m.text) in allowed]
            background = tuple(sorted((*turn.background, *additions), key=lambda m: m.timestamp or 0))
            candidate = replace(item, context=replace(turn, background=background))
            info["added_messages"] = len(additions)
            info["status"] = "added" if additions else "empty"
            if mode == "memory" or not additions:
                coordinator = getattr(plugin, "narrative", None)
                if item.narrative_bundle is not None and coordinator is not None:
                    query = json.dumps(
                        {
                            "speaker_id": turn.author,
                            "current_message": clip_text(turn.text, 600),
                            "pending_question": clip_text(getattr(turn.dialogue, "pending_question", ""), 240),
                            "recent_context": [
                                {"author_id": m.author, "text": clip_text(m.text, 180)} for m in background[-4:]
                            ],
                        },
                        ensure_ascii=False,
                    )
                    bundle, memory_status = await coordinator.supplement_context(item, query)
                    candidate = replace(candidate, narrative_bundle=bundle)
                    info["memory_status"] = memory_status
                    if memory_status == "added":
                        info["status"] = "added"
                else:
                    info["memory_status"] = "unavailable"
            if not await plugin.persona_engine.valid_async(runtime, candidate):
                raise member_boundary.BoundaryCancelled("context_completion_invalidated")

        await asyncio.wait_for(retrieve(), TIMEOUT_SECONDS)
    except member_boundary.BoundaryCancelled:
        raise
    except asyncio.TimeoutError:
        info["status"] = "timeout"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        info["status"] = "error"
        info["error_type"] = type(exc).__name__
    info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return replace(candidate, observations={**item.observations, "context_completion": info})
