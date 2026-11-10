"""Recipient rules + independent discussion routing; people may span topics."""
from __future__ import annotations

from dataclasses import asdict
import math

from .rule_recipients import resolve_recipients
from .thread_router import ThreadRouter, RoutingInference, _state_guard
from .session_runtime import PendingAssignment
from .topic_reranker import RerankCandidate
from .topic_identity import node_topic_id
from .message_features import analyze_text
from .evidence import routing_ledger
from .discussion_fragments import low_information, fragment_groups


def substantive(node):
    facts = analyze_text(node.text)
    return bool(facts.can_start_topic and not facts.is_filler and not facts.is_elliptical)


class RuleTopicRouter(ThreadRouter):
    def __init__(self, *args, rule_enabled=True, recipient_gap=45.0, recipient_lifetime=120.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.rule_enabled = rule_enabled
        self.recipient_gap, self.recipient_lifetime = recipient_gap, recipient_lifetime

    @staticmethod
    def _past(runtime, node):
        rows = []
        for item in runtime.dag.get_recent_nodes(0):
            if item.msg_id == node.msg_id:
                break
            if item.timestamp <= node.timestamp:
                rows.append(item)
        return rows

    def _candidates(self, runtime, node, exclude_ids=()):
        past = self._past(runtime, node)
        visible = {n.msg_id for n in past if n.msg_id not in exclude_ids
                   and 0 <= node.timestamp - n.timestamp <= self.topic_resolver.window_seconds}
        candidates = []
        for topic in runtime.routing_state.topics.values():
            if topic.created_at > node.timestamp:
                continue
            rows = [runtime.dag.nodes[mid] for mid in topic.message_ids if mid in visible]
            if not rows:
                continue
            representative = self._representative(rows, node.timestamp)
            recent = sorted(rows, key=lambda n: n.timestamp)[-3:]
            candidates.append((max(n.timestamp for n in rows), RerankCandidate(
                topic.topic_id, representative.text[:400] if representative else '',
                tuple(n.text[:240] for n in recent))))
        offered = {candidate.topic_id for _, candidate in candidates}
        # A low score makes a tentative seed, not an established discussion.
        for seed in past:
            if (seed.msg_id in visible and seed.msg_id not in offered
                    and seed.metadata.get('discussion_seed') and substantive(seed)):
                candidates.append((seed.timestamp, RerankCandidate(seed.msg_id, seed.text[:400], (seed.text[:240],))))
        candidates.sort(key=lambda c: c[0], reverse=True)
        return [c for _, c in candidates[:8]]

    @staticmethod
    def _representative(nodes, now):
        content = [n for n in nodes if substantive(n)]
        if not content:
            return None
        def weight(candidate):
            supporters = {}
            for other in nodes:
                links = other.metadata.get('discussion_support_ids', ())
                if candidate.msg_id in links or other.reply_to_id == candidate.msg_id:
                    supporters[other.user_id] = max(supporters.get(other.user_id, 0),
                        math.exp(-max(0, now - other.timestamp) / 120))
            return (sum(supporters.values()), candidate.timestamp)
        return max(content, key=weight)

    def _commit(self, runtime, node, topic_id, reason, confidence):
        state, dag = runtime.routing_state, runtime.dag
        prior = state.topics.get(node_topic_id(node))
        self._remember(state, node, topic_id, dag=dag)
        if prior is not None and prior.topic_id != topic_id and not prior.message_ids:
            state.topics.pop(prior.topic_id, None)
        topic = state.topics[topic_id]
        visible = {n.msg_id for n in self._past(runtime, node)} | {node.msg_id}
        rows = [dag.nodes[mid] for mid in topic.message_ids if mid in visible]
        representative = self._representative(rows, node.timestamp)
        if representative:
            topic.label = topic.generated_title or representative.text[:48]
            topic.summary_excerpts = [representative.text[:240]]
            topic.exemplar_messages = [(representative.msg_id, 1.0)]
            node.metadata['discussion_representative_id'] = representative.msg_id
        topic.label_requested = len(topic.message_ids) >= 2 and bool(representative)
        if node.user_id != getattr(runtime, 'bot_id', ''):
            topic.human_updated_at = max(topic.human_updated_at, node.timestamp)
        routing = node.metadata['routing']
        routing.update(topic_id=topic_id, topic_confidence=confidence, topic_status='committed', topic_ambiguous=False,
                       ambiguous=routing.get('addressee_ambiguous', True))
        routing['evidence'] = [e for e in routing['evidence'] if e != 'topic_not_formed'] + [reason]
        routing['ledger'] = routing_ledger(routing)
        node.metadata['topic_id'] = topic_id
        node.metadata.pop('discussion_seed', None)
        state.pending_assignments.pop(node.msg_id, None)
        state.last_topic_id = topic_id

    def _seed(self, runtime, node):
        """Retain a possible new discussion without displaying a singleton topic."""
        if substantive(node):
            node.metadata['discussion_seed'] = True
        runtime.routing_state.pending_assignments[node.msg_id] = PendingAssignment(node.msg_id, created_at=node.timestamp)

    def _promote_seed(self, runtime, seed, node, reason, confidence):
        if not substantive(seed) or seed is node:
            return
        # Backfill records when the second supporting message arrives; this is
        # not a claim that the seed's topic was known at its original timestamp.
        seed.metadata['discussion_confirmed_at'] = node.timestamp
        seed.metadata['discussion_confirmed_by'] = node.msg_id
        self._commit(runtime, seed, seed.msg_id, 'topic_seed_confirmed', confidence)
        runtime.routing_state.topics[seed.msg_id].created_at = node.timestamp
        self._commit(runtime, node, seed.msg_id, reason, confidence)

    def refresh_topics(self, runtime):
        """Reopen demonstrably untouched rc.42 singletons; preserve established history."""
        changed = False
        for tid, topic in list(runtime.routing_state.topics.items()):
            if len(topic.message_ids) == 1 and not topic.generated_title:
                seed = runtime.dag.get_node(topic.message_ids[0])
                routing = seed.metadata.get('routing', {}) if seed else {}
                evidence = set(routing.get('evidence', ()))
                if (seed and tid == seed.msg_id and routing.get('routing_mode') == 'rules'
                        and topic.created_at == topic.updated_at == seed.timestamp
                        and evidence & {'topic_rule_seed', 'topic_native_new', 'topic_rule_quote_seed'}):
                    runtime.routing_state.topics.pop(tid)
                    routing.update(topic_id='', topic_status='pending', topic_ambiguous=True, topic_confidence=0.0, ambiguous=True)
                    routing['evidence'] = [e for e in routing.get('evidence', ()) if not e.startswith('topic_')] + ['topic_not_formed']
                    routing['ledger'] = routing_ledger(routing)
                    seed.metadata.pop('topic_id', None)
                    seed.metadata.pop('topic_title', None)
                    self._seed(runtime, seed)
                    changed = True
                    if runtime.routing_state.last_topic_id == tid:
                        runtime.routing_state.last_topic_id = None
                    if runtime.routing_state.last_bot_topic_id == tid:
                        runtime.routing_state.last_bot_topic_id = None
                    continue
            if len(topic.message_ids) >= 2 and not topic.generated_title:
                changed = changed or not topic.label_requested
                topic.label_requested = True
        return changed

    def route(self, runtime, node, bot_names=(), timeout=.5):
        if not self.rule_enabled:
            return super().route(runtime, node, bot_names=bot_names, timeout=timeout)
        dag, state = runtime.dag, runtime.routing_state
        self.refresh_topics(runtime)
        state.prune(dag, max(node.timestamp, getattr(runtime, 'last_activity', 0)),
                    window_seconds=self.topic_resolver.window_seconds)
        old = node.metadata.get('routing', {})
        if old.get('routing_mode') == 'rules' and old.get('topic_status') == 'committed':
            return RoutingInference(**{k: v for k, v in old.items() if k in RoutingInference.__dataclass_fields__})
        node.metadata.setdefault('dialogue_stop_revision', getattr(runtime, 'topic_stop_revisions', {}).get(node.user_id, 0))
        recipient = resolve_recipients(runtime, node, bot_names, gap=self.recipient_gap, lifetime=self.recipient_lifetime)
        # Remove guessed message edges, including the legacy nearest @ target.
        dag.unlink_inferred_reply(node.msg_id)
        for parent_id, kind in list(node.edge_kinds.items()):
            if kind == 'mention':
                node.parent_ids.discard(parent_id)
                node.edge_kinds.pop(parent_id, None)
                if parent_id in dag.nodes:
                    dag.nodes[parent_id].child_ids.discard(node.msg_id)
        bot = getattr(runtime, 'bot_id', '')
        inferred = RoutingInference(topic_ambiguous=True, topic_status='pending',
            addressee_ids=recipient['ids'], addressee_confidence=recipient['confidence'],
            bot_is_addressee=bool(bot and bot in recipient['ids']),
            bot_addressee_confidence=recipient['confidence'] if bot in recipient['ids'] else 0.0,
            explicit_reply=bool(node.reply_to_id), explicit_mention=bool(node.mentioned_users),
            parent_message_id=node.reply_to_id or '', parent_confidence=1.0 if node.reply_to_id else 0.0,
            parent_ambiguous=not bool(node.reply_to_id), addressee_ambiguous=not bool(recipient['ids']),
            subject_is_bot=recipient['subject_is_bot'], evidence=[recipient['reason']])
        snapshot = asdict(inferred)
        snapshot.update(routing_mode='rules', recipient_anchor_id=recipient['anchor_id'])
        node.metadata['routing'] = snapshot
        node.metadata.pop('topic_id', None)
        visible = {n.msg_id: n for n in self._past(runtime, node)}
        quoted = visible.get(node.reply_to_id)
        if quoted:
            tid = node_topic_id(quoted)
            if tid in state.topics:
                node.metadata['discussion_support_ids'] = [quoted.msg_id]
                self._commit(runtime, node, tid, 'topic_rule_quote', 1.0)
            elif substantive(quoted):
                node.metadata['discussion_support_ids'] = [quoted.msg_id]
                self._promote_seed(runtime, quoted, node, 'topic_rule_quote', .9)
        if snapshot['topic_status'] != 'committed':
            anchor = visible.get(recipient['anchor_id'])
            tid = node_topic_id(anchor) if anchor else ''
            candidates = self._candidates(runtime, node)
            snapshot['topic_candidate_ids'] = [c.topic_id for c in candidates]
            # Recipient inheritance may supply context but cannot silently change topic.
            if anchor and tid in state.topics and recipient['reason'].startswith('rule_recipient_') and not substantive(node):
                node.metadata['discussion_support_ids'] = [anchor.msg_id]
                self._commit(runtime, node, tid, 'topic_rule_short_followup', .8)
            elif not candidates and substantive(node):
                self._seed(runtime, node)
                snapshot['evidence'].append('topic_not_formed')
            else:
                state.pending_assignments[node.msg_id] = PendingAssignment(node.msg_id, created_at=node.timestamp)
                snapshot['evidence'].append('topic_not_formed')
        snapshot['ledger'] = routing_ledger(snapshot)
        return RoutingInference(**{k: v for k, v in snapshot.items() if k in RoutingInference.__dataclass_fields__})

    async def rerank_pending(self, runtime, node, reranker, *, is_current=None):
        if not self.rule_enabled:
            return await super().rerank_pending(runtime, node, reranker, is_current=is_current)
        return await self.rerank_group(runtime, [node], reranker, is_current=is_current)

    async def rerank_group(self, runtime, nodes, reranker, *, is_current=None):
        """One utterance query, with independent node identities and guarded commits."""
        if not nodes:
            return
        count = getattr(reranker, 'count', lambda *_: None)
        split = False
        def identity(node):
            r = node.metadata.get('routing', {})
            return (node.user_id, node.timestamp, node.reply_to_id, tuple(node.mentioned_users),
                    tuple(node.metadata.get('actual_mentions', ())), node.metadata.get('turn_id'),
                    tuple(r.get('addressee_ids', ())), r.get('recipient_anchor_id'))
        async with _state_guard(runtime):
            dag, state, epoch = runtime.dag, runtime.routing_state, getattr(runtime, 'epoch', None)
            snapshots = [node.metadata.get('routing', {}) for node in nodes]
            if (any(dag.get_node(n.msg_id) is not n for n in nodes) or (is_current and not is_current())):
                return
            if len(nodes) > 1 and any(s.get('topic_status') == 'committed' or s.get('discussion_rank_attempted') for s in snapshots):
                split = True
            elif len(nodes) == 1 and (snapshots[0].get('topic_status') == 'committed' or snapshots[0].get('discussion_rank_attempted')):
                return
            if len(nodes) == 1 and low_information(nodes[0].text):
                snapshots[0]['discussion_rank_reason'] = 'low_information'
                snapshots[0]['discussion_rank_attempted'] = True
                count('low_information_skips')
                return
            excluded = {n.msg_id for n in nodes} if len(nodes) > 1 else set()
            candidates = self._candidates(runtime, nodes[0], excluded)
            if len(nodes) > 1 and (not candidates or any(self._candidates(runtime, n, excluded) != candidates for n in nodes[1:])):
                split = True
            if not split and not candidates:
                return
            revisions = dict(getattr(runtime, 'topic_stop_revisions', {}))
            if len(nodes) > 1 and len(fragment_groups(runtime, [(n, revisions.get(n.user_id, 0)) for n in nodes])) != 1:
                split = True
            candidate_objects = {c.topic_id: state.topics[c.topic_id] for c in candidates if c.topic_id in state.topics}
            seed_objects = {c.topic_id: dag.get_node(c.topic_id) for c in candidates if c.topic_id not in state.topics}
            query = '\n'.join(n.text for n in nodes)
            texts = tuple(n.text for n in nodes)
            identities = tuple(identity(n) for n in nodes)
            if not split:
                for snapshot in snapshots:
                    snapshot['discussion_rank_attempted'] = True
        if split:
            for node in nodes:
                await self.rerank_pending(runtime, node, reranker, is_current=is_current)
            return
        if len(nodes) > 1:
            count('fragment_query_reductions', len(nodes) - 1)
            count('fragment_groups')
        scoped = getattr(reranker, 'rank_scoped', None)
        ranking = (await scoped(query, candidates, (runtime.session_key, epoch, sorted(revisions.items())))
                   if callable(scoped) else await reranker.rank(query, candidates))
        async with _state_guard(runtime):
            if (runtime.dag is not dag or runtime.routing_state is not state or getattr(runtime, 'epoch', None) != epoch
                    or any(dag.get_node(n.msg_id) is not n or n.metadata.get('routing') is not s for n, s in zip(nodes, snapshots))
                    or tuple(n.text for n in nodes) != texts
                    or tuple(identity(n) for n in nodes) != identities
                    or dict(getattr(runtime, 'topic_stop_revisions', {})) != revisions
                    or any(state.topics.get(tid) is not obj for tid, obj in candidate_objects.items())
                    or any(dag.get_node(mid) is not seed or not seed.metadata.get('discussion_seed')
                           for mid, seed in seed_objects.items())
                    or any(self._candidates(runtime, n, excluded) != candidates for n in nodes)
                    or (is_current and not is_current())):
                count('stale_results_dropped')
                return
            for snapshot in snapshots:
                snapshot['discussion_rank_reason'] = ranking.reason
                snapshot['topic_candidates'] = list(ranking.scores)
                if len(nodes) > 1:
                    snapshot['discussion_fragment_ids'] = [n.msg_id for n in nodes]
            if ranking.reason != 'ranked' or not ranking.scores:
                return
            best, tid = ranking.scores[0]
            second = ranking.scores[1][0] if len(ranking.scores) > 1 else 0.0
            for node, snapshot in zip(nodes, snapshots):
                snapshot['discussion_candidate_ids'] = [t for score, t in ranking.scores if score >= reranker.threshold]
                if best >= reranker.threshold and best - second >= reranker.margin:
                    node.metadata['discussion_support_topic_id'] = tid
                    if tid in seed_objects and tid not in state.topics:
                        self._promote_seed(runtime, seed_objects[tid], node, 'topic_native_rerank', best)
                        state.topics[tid].created_at = nodes[-1].timestamp
                        seed_objects[tid].metadata['discussion_confirmed_at'] = nodes[-1].timestamp
                        seed_objects[tid].metadata['discussion_confirmed_by'] = nodes[-1].msg_id
                    else:
                        self._commit(runtime, node, tid, 'topic_native_rerank', best)
                    if len(nodes) > 1:
                        node.metadata['discussion_confirmed_at'] = nodes[-1].timestamp
                        node.metadata['discussion_confirmed_by'] = nodes[-1].msg_id
                elif best < reranker.threshold - reranker.margin and substantive(node):
                    self._seed(runtime, node)
            # Close scores retain multiple candidates without merging discussions.
