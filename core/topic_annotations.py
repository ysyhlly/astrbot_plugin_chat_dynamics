"""Human labels for offline routing evaluation; never mutate live routing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from copy import deepcopy

from .annotation_draft import CONTEXT_KEY, build_context, public_draft
from .routing_trace import build_routing_trace, trace_with_updates

ERROR_TYPES = {"correct", "topic_merge", "topic_split", "wrong_assignment", "premature_assignment", "reopen_miss", "unknown", "unreviewed"}

RECIPIENT_ERROR_TYPES = {"correct", "missed_bot", "false_bot", "wrong_recipient", "missing_recipient", "subject_confusion", "unknown"}
REQUIRED_FIELDS = {"session_key", "msg_id", "expected_topic", "error_type"}
# Drafts live in their own key: a proposal a human has not accepted must not be
# readable as a label by anything downstream (the learning plugin, the export,
# the metrics below).
# Drafts outlive the in-memory message graph, so the sessions that own them
# are indexed here: without the index a restart hides drafts on sessions that
# have not seen new traffic, and nobody can even dismiss them.
DRAFT_KEY_PREFIX = "annotation_drafts_v1_"
DRAFT_INDEX_KEY = "annotation_drafts_index_v1"
# A draft outlives its message: each one carries a snapshot of what it is about
# (see annotation_draft.build_context), and those snapshots -- not the drafts --
# are what would make the stored document grow without limit. A snapshot that
# does not fit either bound is left out and the draft keeps the old behaviour:
# listed, but only dismissible once the message is gone.
MAX_DRAFT_CONTEXTS = 512
MAX_DRAFT_CONTEXT_BYTES = 32 * 1024
MAX_DRAFT_CONTEXT_TOTAL_BYTES = 2 * 1024 * 1024
MAX_PENDING_DRAFTS = 2000
RECIPIENT_FIELDS = {"recipient_correct", "bot_targeted", "recipient_ids", "subject_ids", "expected_reply", "recipient_error_type"}


class TopicAnnotations:
    @staticmethod
    def revision(record):
        return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()

    @classmethod
    def state_key(cls, session):
        return "annotation_review_state_v2_" + cls.digest(session)

    @classmethod
    def request_archive_key(cls, session, request_id):
        return "annotation_request_v2_" + cls.digest(session) + "_" + cls.digest(request_id)

    async def _request_entry(self, session, request_id, entries):
        entry = entries.get(request_id)
        if entry is None:
            entry = await self.plugin.get_kv_data(self.request_archive_key(session, request_id), None)
        return entry

    async def _archive_requests(self, session, entries):
        # Archive-before-prune: a crash can duplicate a committed result, never
        # forget one. Only call on already committed records, under the lock.
        for request_id in list(entries)[:-256]:
            await self.plugin.put_kv_data(self.request_archive_key(session, request_id), entries[request_id])
            del entries[request_id]

    async def _state(self, session):
        state = await self.plugin.get_kv_data(self.state_key(session), None)
        if isinstance(state, dict):
            return deepcopy(state)
        rows = await self.plugin.get_kv_data(self.key(session), [])
        return {"labels": deepcopy(rows) if isinstance(rows, list) else [], "requests": {}}

    async def request_result(self, session, request_id, *, fingerprint=None):
        state = await self._state(session)
        entry = await self._request_entry(session, request_id, state.get("requests", {}))
        if entry is None:
            return None
        if fingerprint is not None and entry["fingerprint"] != fingerprint:
            raise ValueError("request_id reused with different input")
        # Repair the legacy projection after an interrupted commit.
        await self.plugin.put_kv_data(self.key(session), state["labels"])
        return deepcopy(entry["result"])

    async def read_request(self, session, request_id):
        async with self.lock:
            result = await self.request_result(session, request_id)
            if result is not None:
                return result
            raw = await self.plugin.get_kv_data(self.draft_key(session), {})
            return deepcopy(raw.get("requests", {}).get(request_id, {}).get("result")) if isinstance(raw, dict) else None

    async def record_request(self, session, request_id, body, result):
        async with self.lock:
            state = await self._state(session)
            fingerprint = self.revision(body)
            entry = await self._request_entry(session, request_id, state.setdefault("requests", {}))
            if entry is not None:
                if entry["fingerprint"] != fingerprint:
                    raise ValueError("request_id reused with different input")
                return deepcopy(entry["result"])
            await self._archive_requests(session, state["requests"])
            state["requests"][request_id] = {"fingerprint": fingerprint, "result": deepcopy(result)}
            await self._index_session(session)
            await self.plugin.put_kv_data(self.state_key(session), state)
            return result

    async def reconcile(self):
        async with self.lock:
            sessions = await self.known_sessions()
            repaired = 0
            for session in sessions:
                state = await self.plugin.get_kv_data(self.state_key(session), None)
                if isinstance(state, dict) and state.get("projection_pending"):
                    await self.plugin.put_kv_data(self.key(session), deepcopy(state["labels"]))
                    state = deepcopy(state)
                    state["projection_pending"] = False
                    await self.plugin.put_kv_data(self.state_key(session), state)
                    repaired += 1
                if isinstance(state, dict):
                    state = deepcopy(state)
                    await self._archive_requests(session, state.setdefault("requests", {}))
                    await self.plugin.put_kv_data(self.state_key(session), state)
                draft = await self.plugin.get_kv_data(self.draft_key(session), None)
                if isinstance(draft, dict):
                    draft = deepcopy(draft)
                    await self._archive_requests(session, draft.setdefault("requests", {}))
                    await self.plugin.put_kv_data(self.draft_key(session), draft)
            return {"sessions": len(sessions), "repaired": repaired}

    @staticmethod
    def digest(session):
        """Stable per-session key that does not carry the group id itself.

        The record stores this so an export stays groupable after it leaves the
        plugin: without it a learner cannot split train from validation by
        conversation, and every row collapses into one "unknown" session.
        """
        return hashlib.sha256(session.encode()).hexdigest()

    @classmethod
    def key(cls, session):
        return "topic_annotations_v1_" + cls.digest(session)

    @classmethod
    def draft_key(cls, session):
        return DRAFT_KEY_PREFIX + cls.digest(session)

    async def read_drafts(self, session) -> dict:
        """Model-drafted labels nobody has accepted yet."""
        raw = await self.plugin.get_kv_data(self.draft_key(session), {})
        if not isinstance(raw, dict):
            return {}
        drafts = raw.get("drafts")
        return {
            "draft_epoch": int(raw.get("draft_epoch", 0)),
            "requests": deepcopy(raw.get("requests", {})),
            "draft_schema_version": raw.get("draft_schema_version"),
            "generated_at": raw.get("generated_at"),
            "provider_id": str(raw.get("provider_id") or ""),
            "model": str(raw.get("model") or ""),
            "dismissed_ids": [mid for mid in raw.get("dismissed_ids", []) if isinstance(mid, str)]
            if isinstance(raw.get("dismissed_ids"), list) else [],
            "drafts": {str(mid): draft for mid, draft in drafts.items()
                       if isinstance(draft, dict)} if isinstance(drafts, dict) else {},
        }

    async def _index_session(self, session, *, remove: bool = False) -> None:
        """Keep review state discoverable for crash recovery. Caller holds the lock.

        Never truncate this index: even sessions without drafts can have a pending
        label projection or a committed request that must survive a restart.
        """
        if remove:
            return  # Retain tombstones so interrupted writes remain discoverable.
        key = DRAFT_INDEX_KEY + "_shard_" + self.digest(session)[:2]
        rows = await self.plugin.get_kv_data(key, [])
        rows = [str(row) for row in rows if isinstance(row, str)] if isinstance(rows, list) else []
        rows = [row for row in rows if row != session]
        if not remove:
            rows.insert(0, session)
        await self.plugin.put_kv_data(key, rows)

    async def known_sessions(self) -> list:
        """Sessions with stored drafts, newest first, whether or not they are live."""
        rows = await self.plugin.get_kv_data(DRAFT_INDEX_KEY, [])
        result = [row for row in rows if isinstance(row, str)] if isinstance(rows, list) else []
        for shard in range(256):
            rows = await self.plugin.get_kv_data(DRAFT_INDEX_KEY + "_shard_" + format(shard, "02x"), [])
            if isinstance(rows, list):
                result.extend(row for row in rows if isinstance(row, str))
        return list(dict.fromkeys(result))

    @staticmethod
    def _encoded_size(value) -> int:
        """Encoded size of a snapshot, or a size no bound can admit.

        A value that cannot be encoded is not storable as a snapshot: the host
        serializes the whole document, so one unserializable field would take
        the draft write down with it. Refusing it here costs the snapshot, not
        the draft.
        """
        try:
            return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        except (TypeError, ValueError):
            return MAX_DRAFT_CONTEXT_BYTES + 1

    def _wall_offset(self) -> float:
        """Calendar minus monotonic clock, so a snapshot keeps a readable stamp.

        Nodes are stamped by the session clock, which restarts with the process.
        A stamp that is meant to outlive the process has to be converted while
        both clocks are still readable.
        """
        clock = getattr(self.plugin, "time_service", None)
        if clock is None:
            return 0.0
        try:
            return float(clock.wall_time()) - float(clock.time())
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _with_contexts(self, session, drafts, *, generated_ids):
        """Capture snapshots once, before newly generated drafts are published.

        A persisted draft is frozen even when its snapshot did not fit. Backfilling
        it on a later merge would change the revision an approval page previewed.
        Membership in the incoming generation identifies a new version; carried
        drafts, including legacy records, need no migration or extra hash fields.
        """
        dag = (getattr(self.plugin, "dags", None) or {}).get(session)
        nodes = getattr(dag, "nodes", None)
        if not isinstance(nodes, dict) or not nodes:
            return drafts
        pending = [(mid, draft) for mid, draft in drafts.items()
                   if mid in generated_ids and isinstance(draft, dict)
                   and not isinstance(draft.get(CONTEXT_KEY), dict)]
        if not pending:
            return drafts
        held = [self._encoded_size(draft.get(CONTEXT_KEY)) for draft in drafts.values()
                if isinstance(draft, dict) and isinstance(draft.get(CONTEXT_KEY), dict)]
        budget = MAX_DRAFT_CONTEXT_TOTAL_BYTES - sum(held)
        room = min(MAX_DRAFT_CONTEXTS - len(held), len(pending))
        if budget <= 0 or room <= 0:
            return drafts
        show_content = bool(getattr(self.plugin, "console_show_message_content", False))
        offset = self._wall_offset()
        result = dict(drafts)
        # Oldest draft first: those are the messages nearest the retention window
        # and the ones whose loss a human notices first.
        for mid, draft in pending:
            node = nodes.get(mid)
            if node is None:
                continue
            context = build_context(node, show_content=show_content,
                                    wall_ts=float(getattr(node, "timestamp", 0.0) or 0.0) + offset)
            size = self._encoded_size(context)
            if size > MAX_DRAFT_CONTEXT_BYTES or size > budget:
                continue
            budget -= size
            room -= 1
            result[mid] = {**draft, CONTEXT_KEY: context}
            if room <= 0:
                break
        return result

    async def save_drafts(self, session, payload, *, merge=False, is_current=None,
                          expected_epoch=None, regenerate_dismissed=False) -> dict | None:
        raw = payload.get("drafts")
        record = {
            "draft_schema_version": payload.get("draft_schema_version") or 1,
            "generated_at": time.time(),
            "provider_id": str(payload.get("provider_id") or "")[:64],
            "model": str(payload.get("model") or "")[:64],
            "drafts": {str(mid): draft for mid, draft in (raw or {}).items()
                       if isinstance(mid, str) and isinstance(draft, dict)},
        }
        async with self.lock:
            previous = await self.read_drafts(session)
            epoch = previous.get("draft_epoch", 0)
            if expected_epoch is not None and expected_epoch != epoch:
                return None
            labels = (await self._state(session))["labels"]
            labelled = {row.get("msg_id") for row in labels if isinstance(row, dict)}
            dismissed = set(previous.get("dismissed_ids", []))
            if regenerate_dismissed:
                dismissed.difference_update(record["drafts"])
            record["dismissed_ids"] = sorted(dismissed)
            record["draft_epoch"] = epoch
            record["requests"] = previous.get("requests", {})
            combined = {**(previous.get("drafts", {}) if merge else {}), **record["drafts"]}
            combined = {mid: draft for mid, draft in combined.items()
                        if mid not in labelled and mid not in dismissed}
            # Contexts are captured after the trim: measuring a snapshot for a draft
            # that is about to be dropped would spend the budget on nothing.
            record["drafts"] = self._with_contexts(
                session, dict(list(combined.items())[-MAX_PENDING_DRAFTS:]),
                generated_ids=set(record["drafts"]))
            if is_current is not None and not is_current():
                return None
            await self._index_session(session)
            await self.plugin.put_kv_data(self.draft_key(session), record)
        return record

    async def clear_drafts(self, session, *, request_id=None):
        async with self.lock:
            previous = await self.read_drafts(session)
            requests = previous.get("requests", {})
            entry = await self._request_entry(session, request_id, requests) if request_id is not None else None
            if entry is not None:
                if entry["fingerprint"] != "clear":
                    raise ValueError("request_id reused with different input")
                return deepcopy(entry["result"])
            dismissed = list(dict.fromkeys(previous.get("dismissed_ids", []) + list(previous.get("drafts", {}))))
            result = {"cleared": True, "draft_epoch": previous.get("draft_epoch", 0) + 1}
            if request_id is not None:
                await self._archive_requests(session, requests)
                requests[request_id] = {"fingerprint": "clear", "result": result}
            await self._index_session(session)
            await self.plugin.put_kv_data(self.draft_key(session), {"drafts": {}, "dismissed_ids": dismissed,
                                                                  "requests": requests,
                                                                  "draft_epoch": previous.get("draft_epoch", 0) + 1})
            await self._index_session(session, remove=True)
            return result

    async def remove_drafts(self, session, msg_ids, *, revisions=None, request_id=None) -> int:
        """Drop drafts a reviewer dismissed or accepted; returns the count removed."""
        wanted = {str(mid) for mid in msg_ids if str(mid).strip()}
        if not wanted:
            return 0
        async with self.lock:
            raw = deepcopy(await self.plugin.get_kv_data(self.draft_key(session), {}))
            if not isinstance(raw, dict):
                return 0
            fingerprint = self.revision({"remove": sorted(wanted), "revisions": revisions})
            requests = raw.setdefault("requests", {})
            entry = await self._request_entry(session, request_id, requests) if request_id is not None else None
            if entry is not None:
                if entry["fingerprint"] != fingerprint:
                    raise ValueError("request_id reused with different input")
                return entry["result"]
            drafts = raw.get("drafts")
            if not isinstance(drafts, dict):
                drafts = {}
            removed = 0
            removed_ids = []
            for mid in wanted:
                if revisions is not None and self.revision(drafts.get(mid)) != revisions.get(mid):
                    continue
                if drafts.pop(mid, None) is not None:
                    removed += 1
                    removed_ids.append(mid)
            raw["drafts"] = drafts
            previous = raw.get("dismissed_ids", [])
            previous = previous if isinstance(previous, list) else []
            raw["dismissed_ids"] = list(dict.fromkeys(
                [mid for mid in previous if isinstance(mid, str)] + sorted(removed_ids)))
            if request_id is not None:
                await self._archive_requests(session, requests)
                requests[request_id] = {"fingerprint": fingerprint, "result": removed}
            await self._index_session(session)
            await self.plugin.put_kv_data(self.draft_key(session), raw)
            if not drafts:
                await self._index_session(session, remove=True)
            return removed

    async def read(self, session):
        rows = (await self._state(session))["labels"]
        rows = deepcopy(rows) if isinstance(rows, list) else []
        # A stored row is only trusted while it still carries the fields this
        # schema requires: one legacy or truncated row used to break both the
        # read and every later save (save() reads first), with no log.
        rows = [
            row for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("error_type"), str)
            and isinstance(row.get("predicted_topic"), str)
            and isinstance(row.get("expected_topic"), str)
            and isinstance(row.get("msg_id"), str)
        ]
        topic_rows = [row for row in rows if row.get("topic_reviewed", True)]
        counts = Counter(row["error_type"] for row in topic_rows)
        matrix = Counter((row["predicted_topic"], row["expected_topic"]) for row in topic_rows)
        recipient_rows = [row for row in rows if RECIPIENT_FIELDS.intersection(row)]
        recipient_counts = Counter(row["recipient_error_type"] for row in recipient_rows if "recipient_error_type" in row)
        drafts = await self.read_drafts(session)
        assisted = sum(1 for row in rows if row.get("accepted_from") == "ai")
        # Labels are a selected sample, not an unbiased estimate of accuracy.
        return {"records": rows, "metrics": {"total": len(topic_rows), "unreviewed": len(rows) - len(topic_rows), "ai_assisted": assisted,
                "error_counts": dict(counts),
                "confusion": [{"predicted": a, "expected": b, "count": n} for (a, b), n in sorted(matrix.items())],
                "sample_note": ("仅统计人工标注样本，不代表真实准确率；其中 %d 条采纳了模型草稿" % assisted)},
                # The pages read the proposal. The message snapshot each draft keeps
                # is storage, not display: shipping a frozen trace per row would grow
                # every annotation response by orders of magnitude.
                "drafts": {mid: public_draft(draft) for mid, draft in (drafts.get("drafts") or {}).items()},
                "drafts_generated_at": drafts.get("generated_at"),
                "recipient_metrics": {"total": len(recipient_rows), "error_counts": dict(recipient_counts),
                    "correct": sum(row.get("recipient_correct") is True for row in recipient_rows),
                    "incorrect": sum(row.get("recipient_correct") is False for row in recipient_rows),
                    "expected_reply": sum(row.get("expected_reply") is True for row in recipient_rows),
                    "sample_note": "仅统计人工标注样本，不代表真实准确率"}}

    async def save(self, body, *, partial=False, draft_revision=None, request_id=None,
                   recovered_node=None):
        """Write one human label.

        `recovered_node` is a message rebuilt from the snapshot a draft keeps, and
        it is used only when the session graph no longer holds the message: the
        approval page can still read such a draft, so the honest answer is a label
        written from what was saved, not a refusal. The caller decides this -- a
        live node always wins, and a missing message with no snapshot still fails.
        """
        if not isinstance(body, dict):
            raise ValueError("invalid annotation fields")
        fingerprint = self.revision({"body": body, "partial": partial, "draft_revision": draft_revision})
        if request_id is not None:
            if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
                raise ValueError("invalid request_id")
            async with self.lock:
                result = await self.request_result(body.get("session_key", ""), request_id, fingerprint=fingerprint)
                if result is not None:
                    return result
        if not REQUIRED_FIELDS <= set(body) or set(body) - REQUIRED_FIELDS - RECIPIENT_FIELDS - {"expected_revision"}:
            raise ValueError("invalid annotation fields")
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in (body[key] for key in REQUIRED_FIELDS)):
            raise ValueError("invalid annotation value")
        for key in ("recipient_correct", "bot_targeted", "expected_reply"):
            if key in body and type(body[key]) is not bool:
                raise ValueError("invalid recipient boolean")
        for key in ("recipient_ids", "subject_ids"):
            if key in body and (not isinstance(body[key], list) or len(body[key]) > 64
                    or any(not isinstance(value, str) or not value or len(value) > 256 for value in body[key])
                    or len(set(body[key])) != len(body[key])):
                raise ValueError("invalid recipient identities")
        if "recipient_error_type" in body and (not isinstance(body["recipient_error_type"], str)
                or body["recipient_error_type"] not in RECIPIENT_ERROR_TYPES):
            raise ValueError("invalid recipient error type")
        session, mid = body["session_key"], body["msg_id"]
        error = body["error_type"]
        if error not in ERROR_TYPES:
            raise ValueError("invalid error type")
        dag = getattr(self.plugin, "dags", {}).get(session)
        node = dag.nodes.get(mid) if dag else None
        if node is None:
            node = recovered_node
        if node is None:
            raise ValueError("message no longer available; refresh replay")
        routing = node.metadata.get("routing", {})
        predicted = str(routing.get("topic_id") or "UNKNOWN")
        frozen_trace = node.metadata.get("decision_trace") or node.metadata.get("trace_inputs") or {}
        if isinstance(frozen_trace, dict) and frozen_trace.get("trace_schema_version"):
            predicted = str(frozen_trace.get("routing", {}).get("selected_topic")
                            or frozen_trace.get("topic", {}).get("topic_id") or predicted)
        expected = body["expected_topic"]
        # Without a graph there is nothing to check a topic name against, and that is
        # the honest answer: a recovered draft can still choose NEW/UNKNOWN/CORRECT/
        # KEEP, which are the choices the approval page offers anyway.
        known = ({str(n.metadata.get("routing", {}).get("topic_id")) for n in dag.nodes.values()}
                 if dag is not None else set())
        runtime = getattr(self.plugin, "_sessions", {}).get(session)
        state = getattr(runtime, "routing_state", None)
        known.update(getattr(state, "topics", {}).keys())
        known.update(getattr(state, "archived_topics", {}).keys())
        known.update(getattr(getattr(state, "archive", None), "entries", {}).keys())
        if expected not in known | {"NEW", "UNKNOWN", "CORRECT", "KEEP", "UNREVIEWED"}:
            raise ValueError("unknown target topic")
        if expected == "CORRECT":
            expected, error = predicted, "correct"
        elif expected not in {"KEEP", "UNREVIEWED"} and error == "correct" and expected != predicted:
            raise ValueError("correct label must match prediction")
        record = {"annotation_schema_version": 2, "msg_id": mid, "predicted_topic": predicted, "expected_topic": expected,
                  "error_type": error, "annotated_at": time.time(),
                  "session_hash": self.digest(session),
                  "routing": {key: deepcopy(routing[key]) for key in ("topic_confidence", "ambiguous", "topic_ambiguous", "topic_status", "candidates", "topic_candidates", "topic_candidate_evidence", "boundary_score", "evidence") if key in routing}}
        record.update({key: deepcopy(body[key]) for key in RECIPIENT_FIELDS if key in body})
        # `decision_trace` is the live frozen snapshot; `trace_inputs` is the bounded
        # copy that survives a restart. Both carry the same key names by construction.
        trace = node.metadata.get("decision_trace") or node.metadata.get("trace_inputs") or {}
        trace = trace if isinstance(trace, dict) else {}
        # The outcome is read from the node rather than from the frozen trace:
        # the trace was snapshotted at decision time, and the gate, the generator
        # and the platform adapter all ran after that. `build_routing_trace`
        # re-attaches it so the rebuilt snapshot describes the whole turn.
        record["decision_trace"] = build_routing_trace(
            routing=routing, identity=trace.get("identity"), participation=trace.get("participation"),
            state=trace.get("state"), mode=trace.get("mode", "legacy"),
            weights_version=trace.get("weights_version", "default"),
            outcome=node.metadata.get("outcome") or trace.get("outcome"),
            shadow=node.metadata.get("shadow_decision") or trace.get("shadow"),
        )
        if trace.get("trace_schema_version"):
            record["decision_trace"] = trace_with_updates(
                trace, outcome=node.metadata.get("outcome") or trace.get("outcome"),
                shadow=node.metadata.get("shadow_decision") or trace.get("shadow"))
        # Only submitted labels and bounded diagnostics, never automatic message collection.
        if getattr(self.plugin, "console_show_message_content", False):
            record["text"] = node.text[:2000]
        async with self.lock:
            state = await self._state(session)
            if request_id is not None:
                result = await self.request_result(session, request_id, fingerprint=fingerprint)
                if result is not None:
                    return result
            rows = (await self.read(session))["records"]
            previous = next((row for row in rows if row["msg_id"] == mid), None)
            if partial and previous and "expected_revision" not in body:
                raise ValueError("已有人工标注，请刷新后确认字段变化再采纳")
            if "expected_revision" in body and body["expected_revision"] != self.revision(previous):
                raise ValueError("标注已被其他页面修改，请刷新后重新确认")
            # Provenance: a human pressed save, and if the values they saved are
            # the ones the draft proposed, the record says so instead of leaving
            # the learning layer to guess how much of a label is model output.
            record["label_source"] = "human"
            draft = (await self.read_drafts(session)).get("drafts", {}).get(mid)
            if draft_revision is not None and draft_revision != self.revision(draft):
                raise ValueError("草稿已更新，请刷新后重新确认")
            if partial and previous:
                for key in RECIPIENT_FIELDS:
                    if key not in body and key in previous:
                        record[key] = deepcopy(previous[key])
            if body["expected_topic"] in {"KEEP", "UNREVIEWED"}:
                # Older learning consumers already treat an empty topic as absent
                # supervision; a new sentinel would be mistaken for a real topic.
                record.update(expected_topic="", error_type="unreviewed", topic_reviewed=False)
                if previous and body["expected_topic"] == "KEEP":
                    for key in ("expected_topic", "error_type", "topic_reviewed"):
                        record[key] = previous.get(key, True if key == "topic_reviewed" else record[key])
            else:
                record["topic_reviewed"] = True
            if isinstance(draft, dict):
                shared = [key for key in ("expected_reply", "bot_targeted") if key in draft]
                if shared and all(body.get(key) == draft.get(key) for key in shared):
                    record["accepted_from"] = "ai"
                    record["draft_confidence"] = draft.get("confidence")
            rows = [row for row in rows if row["msg_id"] != mid]
            rows.append(record)
            state["labels"] = rows[-2000:]
            result = {"saved": True, "record": record, "accepted_draft_revision": self.revision(draft)}
            if request_id is not None:
                await self._archive_requests(session, state.setdefault("requests", {}))
                state.setdefault("requests", {})[request_id] = {"fingerprint": fingerprint, "result": result}
            state["projection_pending"] = True
            await self._index_session(session)
            await self.plugin.put_kv_data(self.state_key(session), state)
            await self.plugin.put_kv_data(self.key(session), state["labels"])
        return result
