"""AnnotationReview operations composed around the plugin runtime host."""
from __future__ import annotations

from typing import Any, Callable
import asyncio
import hashlib
from .annotation_draft import build_batches, build_prompt as build_draft_prompt, parse_drafts
from .llm_adapter import LLMUnavailable

_ANNOTATION_DRAFT_WINDOW = 120
_MAX_TURN_CHARS = 8000
_DRAFT_ACCEPT_TOPICS = {"KEEP": "unreviewed", "CORRECT": "correct", "NEW": "topic_merge", "UNKNOWN": "premature_assignment"}


class AnnotationReview:
    def __init__(self, host: Any) -> None:
        self.host = host

    async def annotation_draft_payload(self, session_key: str, *, refresh: bool = False,
                                       automatic: bool = False,
                                       regenerate_dismissed: bool = False,
                                       is_valid: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Draft reply labels for the current window, for a human to accept.

        The drafts are stored under their own key and never enter the annotation
        list; the human still presses save, and the record then says the accepted
        values came from a draft. Nothing here is applied to routing, and the
        model is not shown what the gate decided — a draft that has seen the
        answer is a rubber stamp, not a second opinion.
        """
        payload: dict[str, Any] = {
            "state": "unavailable",
            "reason": "",
            "session_key": session_key,
            "provider_id": "",
            "generated_at": None,
            "stats": {},
            "drafts": {},
            "invented": [],
            "undecided": [],
            "note": "草稿只写进本插件自己的键，不会成为标注；采纳与否由你在回放页按下保存决定。",
        }
        if not self.host.annotation_draft_enabled:
            payload["state"] = "disabled"
            payload["reason"] = ("AI 预标注默认关闭：它会把该会话的消息正文发给你配置的模型。"
                                 "打开 annotation_draft_enabled 后可用。")
            return payload
        dag = self.host.dags.get(session_key)
        if dag is None:
            payload["state"] = "no_session"
            payload["reason"] = "当前没有这个会话的消息图；先在回放页选中一个会话。"
            return payload
        existing = await self.host.topic_annotations.read(session_key)
        annotated = {row.get("msg_id"): row for row in existing.get("records") or []}
        stored = await self.host.topic_annotations.read_drafts(session_key)
        draft_epoch = stored.get("draft_epoch", 0)
        if not regenerate_dismissed:
            annotated.update({mid: True for mid in stored.get("dismissed_ids", [])})
        if automatic:
            annotated.update(existing.get("drafts") or {})
        runtime = (getattr(self.host, "_sessions", None) or {}).get(session_key)
        epoch = getattr(runtime, "epoch", None)
        bot_id = str(getattr(runtime, "bot_id", "") or "")
        batches, stats = build_batches(dag.get_recent_nodes(_ANNOTATION_DRAFT_WINDOW), annotated,
            limit=int(self.host.annotation_draft_limit or 20), bot_id=bot_id,
            max_prompt_chars=_MAX_TURN_CHARS)
        payload["stats"] = stats
        asked_nodes = {row["msg_id"]: dag.nodes.get(row["msg_id"])
                       for batch in batches for row in batch if row["draft_this"]}

        def is_current() -> bool:
            return bool(not getattr(self.host, "_shutting_down", False)
                        and (is_valid is None or is_valid())
                        and self.host.annotation_draft_enabled
                        and (not automatic or getattr(self.host, "annotation_draft_auto_enabled", False))
                        and self.host.dags.get(session_key) is dag
                        and getattr(runtime, "epoch", None) == epoch
                        and all(dag.nodes.get(mid) is node for mid, node in asked_nodes.items()))

        if not is_current():
            return {**payload, "state": "cancelled", "reason": "会话或设置已变更，本次生成已取消"}
        if not stats["asked"]:
            payload["state"] = "nothing_to_draft"
            payload["reason"] = "窗口里没有可起草的新消息：已有标注或草稿、已忽略，或没有正文。"
            return payload
        provider_id = self.host.llm.configured_provider(purpose="draft")
        payload["provider_id"] = provider_id
        parsed = {"drafts": {}, "drafted": 0, "invented": [], "undecided": [],
                  "missing": [], "duplicate": [], "out_of_window": [], "invalid_confidence": []}
        deadline = asyncio.get_running_loop().time() + float(self.host.annotation_draft_timeout or 60.0)
        try:
            for batch in batches:
                if not is_current():
                    return {**payload, "state": "cancelled", "reason": "会话或设置已变更，本次生成已取消"}
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise LLMUnavailable("draft window deadline exceeded")
                system_prompt, prompt = build_draft_prompt(batch)
                reply = await self.host.llm.generate(
                    prompt=prompt, umo=session_key, system_prompt=system_prompt,
                    purpose="auto_draft" if automatic else "draft", timeout=remaining)
                if not is_current():
                    return {**payload, "state": "cancelled", "reason": "会话或设置已变更，本次生成已取消"}
                part = parse_drafts(reply, batch, diagnostics=True)
                if part is None:
                    parsed["missing"].extend(row["msg_id"] for row in batch if row["draft_this"])
                    continue
                parsed["drafts"].update(part["drafts"])
                for field in ("invented", "undecided", "missing", "duplicate", "out_of_window", "invalid_confidence"):
                    parsed[field].extend(part.get(field, []))
                for field in ("draft_schema_version", "prompt_version"):
                    parsed[field] = part[field]
        except asyncio.CancelledError:
            raise
        except LLMUnavailable as exc:
            self.host._metric("annotation_draft_unavailable")
            payload["reason"] = f"模型不可用（{type(exc).__name__}）。"
            return payload
        except Exception as exc:
            self.host._metric("annotation_draft_failed")
            payload["state"] = "failed"
            payload["reason"] = f"调用模型失败（{type(exc).__name__}）。"
            return payload
        parsed["drafted"] = len(parsed["drafts"])
        payload["diagnostics"] = {key: parsed[key] for key in
            ("missing", "duplicate", "out_of_window", "invalid_confidence", "undecided")}
        if not parsed["drafts"]:
            self.host._metric("annotation_draft_failed")
            payload["state"] = "failed"
            payload["reason"] = "模型返回的不是可解析的 JSON 对象。"
            return payload
        if not is_current():
            return {**payload, "state": "cancelled", "reason": "会话或设置已变更，本次生成已取消"}
        saved = await self.host.topic_annotations.save_drafts(
            session_key, {**parsed, "provider_id": provider_id, "model": ""},
            merge=True, is_current=is_current, expected_epoch=draft_epoch,
            regenerate_dismissed=regenerate_dismissed)
        if saved is None:
            return {**payload, "state": "cancelled", "reason": "会话或设置已变更，本次生成已取消"}
        self.host._metric("annotation_draft_succeeded", amount=int(parsed.get("drafted") or 0))
        payload.update(state="fresh", generated_at=saved.get("generated_at"),
                       drafted=len(set(parsed.get("drafts", {})) & set(saved.get("drafts", {}))),
                       drafts=saved.get("drafts") or {},
                       invented=parsed.get("invented") or [],
                       undecided=parsed.get("undecided") or [])
        return payload


    async def annotation_drafts_payload(self, session_key: str = "") -> dict[str, Any]:
        """Pending AI drafts across sessions (or one), for the approval page.

        Sessions come from the live registry *and* from the store's own draft
        index: a restart empties the in-memory graph, and a session with no new
        traffic would otherwise hide its drafts where nobody could even dismiss
        them. Those items carry `stale_reason: session_gone` and cannot be
        accepted - a label needs the message the graph no longer has. Message
        text follows the same console_show_message_content switch as replay.
        """
        show_content = bool(getattr(self.host, "console_show_message_content", False))
        # DAG nodes are stamped with the monotonic clock; the page renders these as
        # calendar dates, so the conversion happens here (the same presentation-
        # boundary conversion the replay blocks do).
        wall_offset = float(self.host.time_service.wall_time()) - float(self.host.time_service.time())
        if session_key:
            keys = [session_key]
        else:
            keys = sorted(set(self.host.dags) | set(await self.host.topic_annotations.known_sessions()))
        sessions: list[dict[str, Any]] = []
        total = 0
        for key in keys:
            stored = await self.host.topic_annotations.read_drafts(key)
            drafts = stored.get("drafts") or {}
            if not drafts:
                continue
            dag = self.host.dags.get(key)
            existing = await self.host.topic_annotations.read(key)
            annotated = {row.get("msg_id"): row for row in existing.get("records") or []}
            items: list[dict[str, Any]] = []
            for mid, draft in drafts.items():
                if not isinstance(draft, dict):
                    continue
                node = dag.nodes.get(mid) if dag is not None else None
                metadata = getattr(node, "metadata", None) if node is not None else None
                routing = metadata.get("routing", {}) if isinstance(metadata, dict) else {}
                items.append({
                    "msg_id": mid,
                    "expected_reply": draft.get("expected_reply"),
                    "bot_targeted": draft.get("bot_targeted"),
                    "confidence": draft.get("confidence"),
                    "reason": str(draft.get("reason") or ""),
                    "annotated": mid in annotated,
                    "annotation": {field: annotated[mid].get(field) for field in
                        ("expected_reply", "bot_targeted", "expected_topic", "error_type")} if mid in annotated else None,
                    "annotation_revision": self.host.topic_annotations.revision(annotated.get(mid)),
                    "draft_revision": self.host.topic_annotations.revision(draft),
                    "saveable": node is not None,
                    "stale_reason": "" if node is not None else ("session_gone" if dag is None else "evicted"),
                    "text": str(getattr(node, "text", "") or "")[:240] if show_content and node is not None else "",
                    "topic_id": str(routing.get("topic_id") or ""),
                    "ts": (float(getattr(node, "timestamp", 0.0) or 0.0) + wall_offset)
                    if node is not None else 0.0,
                })
            if not items:
                continue
            # Reviewable messages first (chronological), evicted ones last.
            items.sort(key=lambda item: (not item["saveable"], item["ts"]))
            total += len(items)
            sessions.append({
                "session_key": key,
                "generated_at": stored.get("generated_at"),
                "provider_id": str(stored.get("provider_id") or ""),
                "items": items,
            })
        return {"sessions": sessions, "total_drafts": total, "content_hidden": not show_content,
                "reconciliation": getattr(self.host, '_review_reconciliation', {'state': 'not_started'})}


    async def annotation_drafts_apply(self, body: dict[str, Any]) -> dict[str, Any]:
        """Batch review actions over stored drafts: accept, dismiss, or clear.

        Accepting writes a real annotation through the same save path the replay
        page uses, so provenance (`accepted_from: ai`) is decided there by
        comparing values with the stored draft. Accepted and dismissed drafts are
        then removed so the pending list only holds what still needs a human.

        A draft whose message left the graph is *skipped*, not failed: it is a
        normal consequence of restarting the plugin, and the honest answer is
        "this one can only be dismissed" rather than a bare error string.
        """
        if not isinstance(body, dict):
            raise ValueError("invalid body")
        action = str(body.get("action") or "")
        session = str(body.get("session_key") or "").strip()
        if not session or len(session) > 256:
            raise ValueError("session_key is required")
        request_id = body.get('request_id')
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128):
            raise ValueError('invalid request_id')
        store = self.host.topic_annotations
        if request_id:
            # Persist input identity before any per-item effects. A retry after
            # a partial commit must not reuse this ID with a different batch.
            await store.record_request(session, request_id + ':input', body, {'accepted': True})
            async with store.lock:
                completed = await store.request_result(session, request_id, fingerprint=store.revision(body))
            if completed is not None:
                return completed
        async def complete(result):
            if request_id:
                return await store.record_request(session, request_id, body, result)
            return result
        if action == "clear_session":
            await store.clear_drafts(session, request_id=request_id + ':clear' if request_id else None)
            return await complete({"cleared": True})
        if not isinstance(body.get("msg_ids"), list) or len(body["msg_ids"]) > 200:
            raise ValueError("msg_ids must contain at most 200 entries")
        msg_ids = list(dict.fromkeys(str(mid)[:128] for mid in body["msg_ids"] if str(mid).strip()))
        if not msg_ids:
            raise ValueError("msg_ids is required")
        if action == "dismiss":
            removed = await store.remove_drafts(session, msg_ids, request_id=request_id + ':dismiss' if request_id else None)
            return await complete({"removed": removed})
        if action != "accept":
            raise ValueError("unknown action")
        topic = str(body.get("expected_topic") or "KEEP")
        if topic not in _DRAFT_ACCEPT_TOPICS:
            raise ValueError("invalid expected_topic")
        stored = await self.host.topic_annotations.read_drafts(session)
        drafts = stored.get("drafts") or {}
        dag = self.host.dags.get(session)
        saved: list[str] = []
        cleanup_revisions: dict[str, str] = {}
        skipped: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for mid in msg_ids:
            item_request = (request_id + ':' + hashlib.sha256(mid.encode()).hexdigest()) if request_id else None
            if item_request:
                previous_result = await store.read_request(session, item_request)
                if previous_result is not None:
                    saved.append(mid)
                    if isinstance(previous_result.get('accepted_draft_revision'), str):
                        cleanup_revisions[mid] = previous_result['accepted_draft_revision']
                    continue
            draft = drafts.get(mid)
            if not isinstance(draft, dict):
                failed.append({"msg_id": mid, "error": "草稿不存在或已处理"})
                continue
            # Drafts are persisted while the message graph is in memory, so a
            # restart (or the retention window) leaves drafts that nobody can
            # ever accept. Report those as skipped with the reason, instead of
            # letting save() fail them one obscure message at a time.
            if dag is None:
                skipped.append({"msg_id": mid,
                                "error": "会话的消息图已不在内存里（插件重启过），这条草稿只能忽略"})
                continue
            if mid not in dag.nodes:
                skipped.append({"msg_id": mid, "error": "消息已超出保留窗口，这条草稿只能忽略"})
                continue
            record: dict[str, Any] = {
                "session_key": session,
                "msg_id": mid,
                "expected_topic": topic,
                "error_type": _DRAFT_ACCEPT_TOPICS[topic],
            }
            for field in ("expected_reply", "bot_targeted"):
                if isinstance(draft.get(field), bool):
                    record[field] = draft[field]
            revisions = body.get("revisions", {})
            baseline = revisions.get(mid, {}) if isinstance(revisions, dict) else {}
            if "annotation_revision" in baseline:
                record["expected_revision"] = baseline["annotation_revision"]
            try:
                result = await self.host.topic_annotations.save(record, partial=True,
                    draft_revision=baseline.get("draft_revision", store.revision(draft)), request_id=item_request)
            except ValueError as exc:
                if "no longer available" in str(exc):
                    skipped.append({"msg_id": mid, "error": "消息已超出保留窗口，这条草稿只能忽略"})
                else:
                    failed.append({"msg_id": mid, "error": str(exc)})
                continue
            saved.append(mid)
            cleanup_revisions[mid] = result.get('accepted_draft_revision', store.revision(draft))
        if saved:
            await self.host.topic_annotations.remove_drafts(session, saved,
                revisions=cleanup_revisions)
        return await complete({"saved": len(saved), "saved_ids": saved, "skipped": skipped, "failed": failed})
