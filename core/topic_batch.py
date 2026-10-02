"""Session-local timed topic windows; one model call, independent of reply turns."""
from __future__ import annotations

import asyncio

from .decision_routing import message_snapshot

MAX_PENDING = 128
MAX_MESSAGES = 32
MAX_TOPICS = 8


class TopicBatcher:
    def __init__(self, host):
        self.host = host

    def enabled(self, runtime) -> bool:
        h, cfg = self.host, self.host._runtime_config
        return bool(not h._shutting_down and cfg.enabled and cfg.topic_reranker_enabled
                    and cfg.conversation_router_enabled and h._sessions.get(runtime.session_key) is runtime
                    and h.is_group_takeover_enabled(runtime.group_id))

    def enqueue(self, runtime, nodes) -> None:
        """Caller owns state_lock. New messages never postpone a running window."""
        if not self.enabled(runtime):
            return
        if not runtime.topic_batch_pending:
            runtime.topic_batch_due = self.host.time_service.time() + self.host._runtime_config.topic_batch_interval
        for node in nodes:
            if runtime.dag.get_node(node.msg_id) is node:
                runtime.topic_batch_pending[node.msg_id] = (node, runtime.topic_stop_revisions.get(node.user_id, 0))
        while len(runtime.topic_batch_pending) > MAX_PENDING:
            runtime.topic_batch_pending.pop(next(iter(runtime.topic_batch_pending)))
        if runtime.topic_batch_pending and (runtime.topic_batch_task is None or runtime.topic_batch_task.done()):
            runtime.topic_batch_task = self.host._create_background_task(self._run(runtime, runtime.epoch))

    def reconfigure(self) -> None:
        for runtime in self.host._sessions.values():
            if runtime.topic_batch_task is not None:
                runtime.topic_batch_task.cancel()
                runtime.topic_batch_task = None
            if not self.enabled(runtime):
                runtime.topic_batch_pending.clear()
            elif runtime.topic_batch_pending:
                runtime.topic_batch_due = self.host.time_service.time() + self.host._runtime_config.topic_batch_interval
                runtime.topic_batch_task = self.host._create_background_task(self._run(runtime, runtime.epoch))

    async def _run(self, runtime, epoch) -> None:
        h, task = self.host, asyncio.current_task()
        h._track_hook_task(runtime.session_key)

        def current():
            return self.enabled(runtime) and runtime.epoch == epoch and runtime.topic_batch_task is task

        try:
            while current():
                await h.time_service.sleep(max(0.0, runtime.topic_batch_due - h.time_service.time()))
                async with runtime.state_lock:
                    if not current():
                        return
                    entries = list(runtime.topic_batch_pending.values())[-MAX_MESSAGES:]
                    runtime.topic_batch_pending.clear()
                await self.process(runtime, entries, is_current=current)
                async with runtime.state_lock:
                    if not runtime.topic_batch_pending:
                        return
        finally:
            if runtime.topic_batch_task is task:
                runtime.topic_batch_task = None
                runtime.topic_batch_pending.clear()

    async def process(self, runtime, entries, *, is_current) -> None:
        h = self.host
        async with runtime.state_lock:
            if not is_current():
                return
            dag, state, epoch = runtime.dag, runtime.routing_state, runtime.epoch
            config_id = h._turn_config_identity()
            entries = [(n, rev) for n, rev in entries if dag.get_node(n.msg_id) is n
                       and runtime.topic_stop_revisions.get(n.user_id, 0) == rev]
            if not entries:
                return
            now = h.time_service.time()
            state.expire_topics(dag, now)
            topics, title_work, deferred = {}, {}, []
            for topic in sorted(state.topics.values(), key=lambda t: t.updated_at, reverse=True):
                if (not topic.label_requested or topic.generated_title or topic.title_in_flight
                        or topic.title_failures >= 3 or now < topic.title_retry_at):
                    continue
                nodes = [dag.nodes[mid] for mid in topic.message_ids if mid in dag.nodes][-3:]
                if not nodes:
                    continue
                if len(topics) >= MAX_TOPICS:
                    if not topic.title_attempted:
                        deferred.append(nodes[-1])
                    continue
                topics[topic.topic_id] = {"messages": [self._payload(n, dag) for n in nodes], "needs_title": True}
                title_work[topic.topic_id] = (topic, tuple(nodes),
                    {n.user_id: runtime.topic_stop_revisions.get(n.user_id, 0) for n in nodes})
            # Finish already requested new labels over later windows. Failed
            # labels still require a fresh message to retry after their backoff.
            if deferred:
                self.enqueue(runtime, deferred)
            if not title_work:
                return
            reranker = h._topic_reranker(for_display=True)
            if reranker is None:
                return
            for topic, _, _ in title_work.values():
                topic.title_attempted = topic.title_in_flight = True
            payload = {"topics": topics}
        opinion = None
        try:
            opinion = await reranker.enrich_batch(umo=runtime.umo, payload=payload)
        finally:
            async with runtime.state_lock:
                for topic, _, _ in title_work.values():
                    topic.title_in_flight = False
                valid = bool(is_current() and runtime.epoch == epoch and runtime.dag is dag
                             and runtime.routing_state is state and config_id == h._turn_config_identity())
                if valid and opinion is not None:
                    for tid, (topic, nodes, stops) in title_work.items():
                        if (state.topics.get(tid) is not topic or topic.generated_title
                                or any(runtime.topic_stop_revisions.get(uid, 0) != rev for uid, rev in stops.items())
                                or any(dag.get_node(n.msg_id) is not n or n.msg_id not in topic.message_ids for n in nodes)):
                            continue
                        title = opinion.titles.get(tid, "")
                        if title:
                            topic.generated_title = topic.label = title
                            topic.title_failures = 0
                            topic.title_retry_at = 0.0
                            for mid in topic.message_ids:
                                if mid in dag.nodes:
                                    dag.nodes[mid].metadata["topic_title"] = title
                        else:
                            topic.title_failures += 1
                            topic.title_retry_at = h.time_service.time() + min(300.0, 30.0 * 2 ** (topic.title_failures - 1))
                    h._mark_panel_runtime_dirty()

    @staticmethod
    def _payload(node, dag):
        data = message_snapshot(node, dag)
        data["text"] = str(data["text"])[:240]
        return data
