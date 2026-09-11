"""Bounded, session-local conversation routing; similarity is evidence, not addressivity."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from .topic_identity import node_topic_id
from .graph import ConversationDAG, ConversationNode
from .session_runtime import RoutingState as RoutingState, TopicState as TopicState
from .topic_resolution import TopicResolver, build_contextual_query, can_start_topic, is_elliptical
from .pending_topics import defer, reconcile
from .topic_formation import discussion_burst, topic_text

WINDOW_SECONDS = 300.0
WINDOW_NODES = 80
PARENT_WINDOW_SECONDS = 180.0
MAX_PARENT_CANDIDATES = 80
TOPIC_JOIN_THRESHOLD = 0.58
TOPIC_AMBIGUITY_THRESHOLD = 0.48
TOPIC_MARGIN_THRESHOLD = 0.06
PARENT_ACCEPT_THRESHOLD = 0.72
PARENT_MARGIN_THRESHOLD = 0.08

logger = logging.getLogger(__name__)

_ELLIPTICAL_RE = re.compile(
    r"^(?:那|这个|这样|然后|继续|怎么办|为什么|怎么设|默认|我默认|好的|行|对|确实|好使|不行|成)[呢？?。!！\s]*$"
)
_FILLER_RE = re.compile(
    r"^(?:哈+|[?？!！]+|嗯+|哦+|好的|好|ok|[hH]+|233+)$", re.IGNORECASE
)
_QUESTION_RE = re.compile(r"[?？]|怎么|如何|多少|几点|吗|能不能|可以吗|为啥|为什么")


@asynccontextmanager
async def _state_guard(runtime: Any):
    """Serialize snapshots and commits, including lightweight runtime doubles."""
    lock = getattr(runtime, "state_lock", None)
    if lock is None:
        yield
    else:
        async with lock:
            yield


@dataclass
class RoutingInference:
    """Comprehensive outcome of conversation topology and addressee inference."""

    topic_id: str = ""
    topic_confidence: float = 0.0
    topic_ambiguous: bool = False
    topic_status: str = "committed"
    topic_candidates: list[tuple[float, str]] = field(default_factory=list)
    parent_message_id: str = ""
    parent_confidence: float = 0.0
    addressee_ids: list[str] = field(default_factory=list)
    addressee_confidence: float = 0.0
    subject_user_ids: list[str] = field(default_factory=list)
    subject_is_bot: bool = False
    bot_is_addressee: bool = False
    bot_addressee_confidence: float = 0.0
    explicit_reply: bool = False
    explicit_mention: bool = False
    ambiguous: bool = True
    evidence: list[str] = field(default_factory=list)
    possible_parent: str = ""


class ParentRetriever:
    """Retrieves and reranks parent candidates using 6-factor composite scoring."""

    def __init__(
        self,
        accept_threshold: float = PARENT_ACCEPT_THRESHOLD,
        margin_threshold: float = PARENT_MARGIN_THRESHOLD,
        window_seconds: float = PARENT_WINDOW_SECONDS,
        max_candidates: int = MAX_PARENT_CANDIDATES,
    ):
        self.accept_threshold = accept_threshold
        self.margin_threshold = margin_threshold
        self.window_seconds = window_seconds
        self.max_candidates = max_candidates

    def score_candidate(
        self,
        node: ConversationNode,
        candidate: ConversationNode,
        sim: float,
        is_primary_topic: bool,
        turn_distance: int,
        participant_affinity: float = 0.5,
    ) -> float:
        """6-factor reranking: semantic, topic, qa_fit, temporal, turn, participant."""
        # Factor 1: Semantic fit (0.38)
        f_sem = max(0.0, min(1.0, sim))

        # Factor 2: Topic affinity (0.20)
        f_top = 1.0 if is_primary_topic else 0.5

        # Factor 3: Question-Answer fit (0.17)
        cand_q = bool(_QUESTION_RE.search(candidate.text))
        curr_q = bool(_QUESTION_RE.search(node.text))
        if cand_q and not curr_q:
            f_qa = 1.0
        elif not cand_q and curr_q:
            f_qa = 0.5
        elif cand_q and curr_q:
            f_qa = 0.2
        else:
            f_qa = 0.3

        # Factor 4: Temporal decay (0.10)
        delta_t = max(0.0, node.timestamp - candidate.timestamp)
        f_temp = max(0.0, 1.0 - delta_t / self.window_seconds)

        # Factor 5: Turn proximity (0.10)
        f_turn = max(0.0, 1.0 - 0.15 * turn_distance)

        # Factor 6: Participant continuity (0.05)
        f_part = max(0.0, min(1.0, participant_affinity))

        score = (
            f_sem * 0.38
            + f_top * 0.20
            + f_qa * 0.17
            + f_temp * 0.10
            + f_turn * 0.10
            + f_part * 0.05
        )
        return round(score, 4)

    def retrieve(
        self,
        node: ConversationNode,
        dag: ConversationDAG,
        matches: dict[str, float],
        primary_topic: str,
        second_topic: Optional[str] = None,
        is_ambiguous: bool = False,
        recent_nodes: Optional[List[ConversationNode]] = None,
    ) -> Tuple[Optional[str], float, float, List[str], str, List[Tuple[float, str]]]:
        """Evaluates candidates. Returns (parent_id, conf, margin, evidence, possible_parent, candidates)."""
        if bool(_FILLER_RE.fullmatch(node.text.strip())):
            return None, 0.0, 0.0, [], "", []

        target_topics = {primary_topic}
        if is_ambiguous and second_topic:
            target_topics.add(second_topic)

        recent = recent_nodes if recent_nodes is not None else dag.get_recent_nodes(self.max_candidates)
        candidates: List[Tuple[float, str]] = []

        # Count only observed platform interactions; inferred edges must not
        # reinforce their own future confidence.
        interactions: dict[tuple[str, str], float] = {}
        for previous in recent:
            if not 0 < node.timestamp - previous.timestamp <= self.window_seconds:
                continue
            targets = set(previous.mentioned_users)
            quoted = dag.get_node(previous.reply_to_id) if previous.reply_to_id else None
            if quoted is not None:
                targets.add(quoted.user_id)
            for target in targets:
                if target and target != previous.user_id:
                    pair = tuple(sorted((previous.user_id, target)))
                    interactions[pair] = interactions.get(pair, 0.0) + 1.0

        turn_distance = 0
        for candidate in reversed(recent):
            if candidate.msg_id == node.msg_id:
                continue
            delta_t = node.timestamp - candidate.timestamp
            if not 0 < delta_t <= self.window_seconds:
                continue
            turn_distance += 1
            if candidate.user_id == node.user_id:
                continue
            candidate_routing = candidate.metadata.get("routing", {})
            if candidate_routing.get("topic_status") == "unknown":
                continue
            cand_topic = node_topic_id(candidate)
            if candidate_routing.get("topic_status") == "pending":
                eligible = {tid for _, tid in candidate_routing.get("topic_candidates", [])}
                # The current topic must already be independently resolved.
                if is_ambiguous or primary_topic not in eligible:
                    continue
                cand_topic = primary_topic
            if not cand_topic or cand_topic not in target_topics:
                continue

            sim = matches.get(candidate.msg_id, 0.0)
            score = self.score_candidate(
                node=node,
                candidate=candidate,
                sim=sim,
                is_primary_topic=(cand_topic == primary_topic),
                turn_distance=turn_distance,
                participant_affinity=min(1.0, interactions.get(
                    tuple(sorted((node.user_id, candidate.user_id))), 0.0) / 2.0),
            )
            candidates.append((score, candidate.msg_id))

        if not candidates:
            return None, 0.0, 0.0, [], "", []

        candidates.sort(reverse=True)
        best_score, best_mid = candidates[0]
        second_score = candidates[1][0] if len(candidates) > 1 else 0.0
        margin = round(best_score - second_score, 4)

        if best_score >= self.accept_threshold and margin >= self.margin_threshold:
            return best_mid, best_score, margin, ["inferred_reply"], best_mid, candidates
        return None, best_score, margin, [], best_mid, candidates


class AddresseeResolver:
    """Resolve mentions and direct name calls before quotes and inferred parents.

    A quoted message can be the subject of a direct request to the bot. Without
    an explicit recipient, bounded active-dialogue evidence provides a fallback.
    """

    @classmethod
    def is_vocative_call(cls, text: str, bot_names: Sequence[str]) -> bool:
        """Detect direct vocative address targeting bot while filtering 3rd-person subject remarks."""
        from .addressivity import AddressivityRouter

        return any(AddressivityRouter._name_mentioned_in_text(str(name), text)
                   for name in bot_names if name)

    @classmethod
    def is_subject_reference(cls, text: str, bot_names: Sequence[str]) -> bool:
        """Check if message discusses or refers to the bot in the third person."""
        if not text or not bot_names:
            return False
        lowered = text.lower()
        for name in bot_names:
            if not name:
                continue
            name_str = str(name).strip().lower()
            if not name_str:
                continue
            if name_str.isascii() and re.match(r"^[a-zA-Z0-9_-]+$", name_str):
                pattern = rf"(?<![a-zA-Z0-9]){re.escape(name_str)}(?![a-zA-Z0-9])"
                if re.search(pattern, lowered):
                    return True
            else:
                if name_str in lowered:
                    return True
        return False

    def resolve(
        self,
        node: ConversationNode,
        dag: Any,
        runtime: Any,
        topic_id: str,
        quoted_node: Optional[ConversationNode] = None,
        inferred_parent: Optional[ConversationNode] = None,
        inferred_confidence: float = 0.0,
        ranked_topics: Sequence[Tuple[float, str]] = (),
        recent_nodes: Sequence[ConversationNode] = (),
        bot_names: Sequence[str] = (),
    ) -> Tuple[List[str], float, bool, float, List[str], bool, List[str], Optional[Tuple[str, float, str]]]:
        """Resolves addressees and subject references based on the 6-tier precedence hierarchy."""
        bot_id = getattr(runtime, "bot_id", "")
        last_bot = getattr(runtime, "last_bot_node", None)
        names = [str(n) for n in bot_names if str(n)]
        addressee_ids: List[str] = []
        addressee_confidence: float = 0.0
        evidence: List[str] = []
        parent_override: Optional[Tuple[str, float, str]] = None

        # Subject reference analysis
        subject_is_bot = self.is_subject_reference(node.text, names)
        subject_user_ids = [bot_id] if (subject_is_bot and bot_id) else []
        vocative_target = self.is_vocative_call(node.text, names)

        # Tier 1: Explicit Mention (@mention) or Platform Wake
        is_wake = bool(getattr(node, "metadata", {}) and node.metadata.get("is_wake"))
        if (is_wake or (node.mentioned_users and bot_id in node.mentioned_users)) and bot_id:
            addressee_ids = [bot_id]
            addressee_confidence = 1.0
            evidence.append("explicit_mention" if node.mentioned_users else "platform_wake")
        elif node.mentioned_users:
            addressee_ids = list(dict.fromkeys(node.mentioned_users))
            addressee_confidence = 1.0
            evidence.append("explicit_mention")

        # A direct name call can ask the bot to explain a quoted human message.
        elif vocative_target and bot_id:
            addressee_ids = [bot_id]
            addressee_confidence = 0.95
            evidence.append("direct_name_call")

        # Tier 2: Explicit Platform Reply / Quote
        elif quoted_node is not None:
            # Sub-case 2A: Quoted Subject in Active Bot Dialogue
            if last_bot is not None and quoted_node.user_id != bot_id:
                last_bot_reply_to = getattr(last_bot, "reply_to_id", None)
                trigger = dag.get_node(last_bot_reply_to) if (last_bot_reply_to and dag) else None
                interlocutor = trigger.user_id if trigger else getattr(runtime, "last_interlocutor", "")
                bot_topic = node_topic_id(last_bot)
                last_bot_ts = getattr(last_bot, "timestamp", 0.0)
                elliptical = bool(re.fullmatch(r"\s*(?:那|这个|这样|这个呢|那这个呢|那怎么办|这个怎么办)[呢？?。!！]*\s*", node.text))
                competing = any(
                    n.user_id not in {node.user_id, bot_id}
                    and n.timestamp > last_bot_ts
                    and (not bot_topic or node_topic_id(n) == bot_topic)
                    for n in recent_nodes
                )
                if (
                    elliptical
                    and interlocutor == node.user_id
                    and (bool(bot_topic and topic_id == bot_topic)
                         or (not bot_topic and trigger is not None
                             and trigger.reply_to_id == quoted_node.msg_id))
                    and 0 < node.timestamp - last_bot_ts <= 60
                    and not competing
                ):
                    addressee_ids = [bot_id]
                    addressee_confidence = 0.78
                    evidence.append("quoted_subject_active_interlocutor")

            # Sub-case 2B: Standard Platform Reply
            if not addressee_ids:
                addressee_ids = [quoted_node.user_id]
                addressee_confidence = 1.0

        # Tier 3: Inferred Parent (from ParentRetriever)
        elif inferred_parent is not None and inferred_confidence >= 0.72:
            addressee_ids = [inferred_parent.user_id]
            addressee_confidence = inferred_confidence
            evidence.append("inferred_reply")

        # Tier 4: Active Interlocutor Dialogue Continuation Fallback
        elif not node.reply_to_id and last_bot is not None and bot_id:
            last_bot_reply_to = getattr(last_bot, "reply_to_id", None)
            prior = dag.get_node(last_bot_reply_to) if (last_bot_reply_to and dag) else None
            interlocutor = prior.user_id if prior else getattr(runtime, "last_interlocutor", "")
            bot_topic = node_topic_id(last_bot)
            last_bot_ts = getattr(last_bot, "timestamp", 0.0)
            competing = any(
                n.user_id not in {node.user_id, bot_id}
                and n.timestamp > last_bot_ts
                and (not bot_topic or node_topic_id(n) == bot_topic)
                for n in recent_nodes
            )
            followup = bool(re.search(r"然后|继续|那|这个|这样|怎么办|呢[？?]?$", node.text))
            if not bot_topic:
                # With no semantic topic, only a short continuation can borrow
                # the active interlocutor; a new sentence containing "那" cannot.
                followup = is_elliptical(node.text) or bool(re.fullmatch(
                    r"\s*那(?:这个|怎么办|怎么做)[呢？?。!！\s]*", node.text))
            if (
                interlocutor == node.user_id
                and 0 < node.timestamp - last_bot_ts <= 60
                and followup
                and not competing
                and (bool(topic_id and bot_topic and topic_id == bot_topic)
                     or not ranked_topics or ranked_topics[0][0] < 0.35)
            ):
                addressee_ids = [bot_id]
                addressee_confidence = 0.76
                evidence.append("active_interlocutor_followup")
                parent_override = (getattr(last_bot, "msg_id", ""), 0.76, bot_topic)

        # Final synthesis
        bot_is_addressee = bool(bot_id and bot_id in addressee_ids)
        bot_addressee_confidence = addressee_confidence if bot_is_addressee else 0.0

        return (
            addressee_ids,
            addressee_confidence,
            bot_is_addressee,
            bot_addressee_confidence,
            subject_user_ids,
            subject_is_bot,
            evidence,
            parent_override,
        )


class ThreadRouter:
    """High-level conversation topology router coordinating topic, parent, and addressee."""

    build_contextual_query = staticmethod(build_contextual_query)
    _remember = staticmethod(TopicResolver.remember)
    remember = staticmethod(TopicResolver.remember)

    def __init__(
        self,
        topic_resolver: Optional[TopicResolver] = None,
        parent_retriever: Optional[ParentRetriever] = None,
        addressee_resolver: Optional[AddresseeResolver] = None,
        topic_window_seconds: float = WINDOW_SECONDS,
        topic_join_threshold: float = TOPIC_AMBIGUITY_THRESHOLD,
        parent_window_seconds: float = PARENT_WINDOW_SECONDS,
        parent_accept_threshold: float = PARENT_ACCEPT_THRESHOLD,
        require_intense_dialogue: bool = True,
        topic_commit_threshold: float = 0.0,
        topic_ambiguity_threshold: float = 0.0,
        topic_margin_threshold: float = TOPIC_MARGIN_THRESHOLD,
    ):
        self.require_intense_dialogue = require_intense_dialogue
        if topic_resolver is None:
            self.topic_resolver = TopicResolver()
            self.configure_topics(
                window_seconds=topic_window_seconds,
                legacy_threshold=topic_join_threshold,
                commit_threshold=topic_commit_threshold,
                ambiguity_threshold=topic_ambiguity_threshold,
                margin_threshold=topic_margin_threshold,
            )
        else:
            self.topic_resolver = topic_resolver

        if parent_retriever is None:
            self.parent_retriever = ParentRetriever(
                window_seconds=parent_window_seconds,
                accept_threshold=parent_accept_threshold,
            )
        else:
            self.parent_retriever = parent_retriever

        self.addressee_resolver = addressee_resolver or AddresseeResolver()

    def configure_topics(
        self,
        *,
        window_seconds: float,
        legacy_threshold: float,
        commit_threshold: float = 0.0,
        ambiguity_threshold: float = 0.0,
        margin_threshold: float = TOPIC_MARGIN_THRESHOLD,
    ) -> None:
        """Apply the same legacy threshold mapping at startup and hot reload."""
        self.topic_resolver.window_seconds = window_seconds
        self.topic_resolver.ambiguity_threshold = ambiguity_threshold or legacy_threshold
        self.topic_resolver.join_threshold = commit_threshold or (
            max(TOPIC_JOIN_THRESHOLD, legacy_threshold + 0.10)
            if legacy_threshold < TOPIC_JOIN_THRESHOLD else legacy_threshold
        )
        self.topic_resolver.margin_threshold = margin_threshold

    def route(
        self,
        runtime: Any,
        node: ConversationNode,
        bot_names: Sequence[str] = (),
        timeout: float = 0.5,
    ) -> RoutingInference:
        """Route message turn synchronously, inferring topic, parent, and addressee."""
        dag = runtime.dag
        result = RoutingInference(topic_id=node.msg_id)
        if dag is None:
            node.metadata["routing"] = asdict(result)
            return result

        state = runtime.routing_state
        previous_routing = node.metadata.get("routing", {})
        if (any(ev in previous_routing.get("evidence", []) for ev in ("topic_llm_rerank", "topic_burst_confirmed"))
                and previous_routing.get("topic_id") in state.topics):
            return RoutingInference(**{key: value for key, value in previous_routing.items()
                                       if key in RoutingInference.__dataclass_fields__})
        now = max(
            getattr(runtime, "last_activity", 0.0) or 0.0,
            dag.last_timestamp() if hasattr(dag, "last_timestamp") else 0.0,
            getattr(node, "timestamp", 0.0) or 0.0,
        )
        state.prune(dag, now, window_seconds=self.topic_resolver.window_seconds)

        # Only inferred edges are replaceable. Platform and fragment edges survive.
        if hasattr(dag, "unlink_inferred_reply"):
            dag.unlink_inferred_reply(node.msg_id)
        else:
            for parent_id, kind in list(node.edge_kinds.items()):
                if kind in {"inferred_reply", "semantic"}:
                    node.parent_ids.discard(parent_id)
                    node.edge_kinds.pop(parent_id, None)
                    if parent_id in dag.nodes:
                        dag.nodes[parent_id].child_ids.discard(node.msg_id)

        recent = [
            n
            for n in dag.get_recent_nodes(WINDOW_NODES)
            if n.msg_id != node.msg_id and 0 < node.timestamp - n.timestamp <= self.topic_resolver.window_seconds
        ]
        quoted = dag.get_node(node.reply_to_id) if node.reply_to_id else None
        parent = quoted

        if node.reply_to_id:
            result.explicit_reply = True
            result.parent_message_id = node.reply_to_id
            result.parent_confidence = 1.0
            result.evidence.append("explicit_reply")

        if bool((getattr(node, "metadata", {}) or {}).get("is_wake")) or node.mentioned_users:
            result.explicit_mention = True

        if parent is None and node.mentioned_users and not node.reply_to_id:
            parent = next((n for n in reversed(recent) if n.user_id in node.mentioned_users), None)

        burst = discussion_burst(dag, node, getattr(runtime, "bot_id", "")) if self.require_intense_dialogue else []
        formation_allowed = not self.require_intense_dialogue or bool(burst)
        formed_topic = ""
        if burst:
            existing = [node_topic_id(n) for n in burst]
            formed_topic = next((tid for tid in existing if tid in state.topics), burst[0].msg_id)
            for previous in burst:
                if previous.msg_id == node.msg_id or node_topic_id(previous):
                    continue
                self._remember(state, previous, formed_topic, dag=dag)
                state.pending_assignments.pop(previous.msg_id, None)
                prior = dict(previous.metadata.get("routing", {}))
                prior.update(topic_id=formed_topic, topic_status="committed", topic_ambiguous=False,
                             topic_confidence=0.75)
                prior["ambiguous"] = float(prior.get("addressee_confidence", 0.0) or 0.0) < 0.72
                prior["evidence"] = list(prior.get("evidence", [])) + ["topic_burst_confirmed"]
                previous.metadata["routing"] = prior
                previous.metadata["topic_id"] = formed_topic
        query = topic_text(node)
        matches: dict[str, float] = {}
        for candidate in recent:
            if not can_start_topic(topic_text(candidate)):
                continue
            match = dag.semantic_match_fn(query, topic_text(candidate))
            sim = max(float(getattr(match, "score", 0.0) or 0.0), float(getattr(match, "embedding_cosine", 0.0) or 0.0))
            matches[candidate.msg_id] = max(0.0, min(1.0, sim))

        # 1. Topic Resolution
        topic_id, topic_conf, is_ambiguous, topic_evidence, second_topic, ranked_topics = (
            self.topic_resolver.resolve(
                node=node,
                dag=dag,
                state=state,
                matches=matches,
                explicit_parent=parent,
            )
        )
        if formed_topic and topic_id == node.msg_id and "topic_boundary" not in topic_evidence:
            topic_id, topic_conf, is_ambiguous = formed_topic, 0.75, False
            topic_evidence.append("topic_burst_confirmed")
        if topic_id == node.msg_id and not can_start_topic(topic_text(node)):
            is_ambiguous = True
            topic_conf = 0.0
            topic_evidence.append("topic_not_formed")
        result.topic_id = topic_id
        result.topic_ambiguous = is_ambiguous
        result.topic_candidates = list(ranked_topics[:3])
        if (formation_allowed and topic_id == node.msg_id and not is_ambiguous and "topic_boundary" not in topic_evidence
                and (not ranked_topics or ranked_topics[0][0] < self.topic_resolver.ambiguity_threshold)):
            archived = state.archive.retrieve(node, dag)
            if archived is not None:
                result.topic_id = archived.topic_id
                topic_conf = archived.score
                topic_evidence.append("topic_reopen")
                if archived.topic.title:
                    state.topics[archived.topic_id] = TopicState(
                        archived.topic_id, generated_title=archived.topic.title,
                        label=archived.topic.title, title_attempted=True)
                state.archive.pop(archived.topic_id)
        result.topic_confidence = topic_conf
        for ev in topic_evidence:
            if ev not in result.evidence:
                result.evidence.append(ev)

        # 2. Parent Candidate Search & Reranking
        inferred_candidate_parent = None
        inferred_candidate_score = 0.0
        candidates: List[Tuple[float, str]] = []
        parent_margin = 0.0

        if parent is None and not node.reply_to_id and not node.mentioned_users:
            inferred_mid, p_conf, p_margin, p_evidence, possible_mid, candidates = (
                self.parent_retriever.retrieve(
                    node=node,
                    dag=dag,
                    matches=matches,
                    primary_topic=result.topic_id,
                    second_topic=second_topic,
                    is_ambiguous=is_ambiguous,
                    recent_nodes=recent,
                )
            )
            result.possible_parent = possible_mid
            parent_margin = p_margin
            if inferred_mid is not None and inferred_mid in dag.nodes:
                inferred_candidate_parent = dag.nodes[inferred_mid]
                inferred_candidate_score = p_conf

        # 3. Addressee Resolution
        (
            addressee_ids,
            addressee_conf,
            bot_is_addressee,
            bot_addressee_conf,
            subject_user_ids,
            subject_is_bot,
            addr_evidence,
            parent_override,
        ) = self.addressee_resolver.resolve(
            node=node,
            dag=dag,
            runtime=runtime,
            topic_id=result.topic_id,
            quoted_node=quoted,
            inferred_parent=inferred_candidate_parent,
            inferred_confidence=inferred_candidate_score,
            ranked_topics=ranked_topics,
            recent_nodes=recent,
            bot_names=bot_names,
        )

        result.addressee_ids = addressee_ids
        result.addressee_confidence = addressee_conf
        result.bot_is_addressee = bot_is_addressee
        result.bot_addressee_confidence = bot_addressee_conf
        result.subject_user_ids = subject_user_ids
        result.subject_is_bot = subject_is_bot
        for ev in addr_evidence:
            if ev not in result.evidence:
                result.evidence.append(ev)

        # 4. Apply Inferred / Overridden DAG edges
        if "inferred_reply" in addr_evidence and inferred_candidate_parent is not None:
            result.parent_message_id = inferred_candidate_parent.msg_id
            result.parent_confidence = inferred_candidate_score
            if hasattr(dag, "link_inferred_reply"):
                dag.link_inferred_reply(
                    node.msg_id,
                    inferred_candidate_parent.msg_id,
                    confidence=inferred_candidate_score,
                    reason="inferred_reply",
                )
            else:
                dag._link_parent(node.msg_id, inferred_candidate_parent.msg_id, kind="inferred_reply")
        elif parent_override is not None:
            pmid, pconf, ptopic = parent_override
            if ptopic:
                result.topic_id = ptopic
                result.topic_confidence = 0.78
                result.topic_ambiguous = False
            result.parent_message_id = pmid
            result.parent_confidence = pconf
            if hasattr(dag, "link_inferred_reply"):
                dag.link_inferred_reply(node.msg_id, pmid, confidence=pconf, reason="active_interlocutor_followup")
            else:
                dag._link_parent(node.msg_id, pmid, kind="inferred_reply")

        if not formation_allowed:
            result.topic_id = ""
            result.topic_confidence = 0.0
            result.topic_ambiguous = True
            result.topic_status = "unformed"
            result.evidence.append("topic_not_formed")
            state.pending_assignments.pop(node.msg_id, None)
            node.metadata.pop("topic_title", None)
        elif result.topic_ambiguous:
            defer(state, node, result, ranked_topics)
        else:
            state.pending_assignments.pop(node.msg_id, None)
            self._remember(state, node, result.topic_id, dag=dag)
            state.last_topic_id = result.topic_id
        result.ambiguous = result.topic_ambiguous or result.addressee_confidence < 0.72
        node.metadata["topic_id"] = result.topic_id
        node.metadata["routing"] = asdict(result)
        if formation_allowed:
            reconcile(state, dag, node, result, self._remember)
        if node.user_id == getattr(runtime, "bot_id", ""):
            state.last_bot_topic_id = result.topic_id
            if hasattr(runtime, "last_bot_node"):
                runtime.last_bot_node = node
        state.prune(dag, now, window_seconds=self.topic_resolver.window_seconds)

        # Keep diagnostics useful without recording text or participant identifiers.
        logger.debug(
            "[Router] topic_conf=%.2f parent_conf=%.2f parent_margin=%.2f "
            "addressee_conf=%.2f bot_is_addressee=%s ambiguous=%s",
            result.topic_confidence, result.parent_confidence, parent_margin,
            result.addressee_confidence, result.bot_is_addressee, result.ambiguous,
        )
        return result

    async def route_async(
        self,
        runtime: Any,
        node: ConversationNode,
        bot_names: Sequence[str] = (),
        timeout: float = 0.5,
        embedding_adapter: Any = None,
    ) -> RoutingInference:
        """Escalate ambiguous queries; DAG matcher must use this adapter's cache."""
        sync_result = self.route(runtime, node, bot_names=bot_names, timeout=timeout)
        if not sync_result.ambiguous or embedding_adapter is None or not getattr(embedding_adapter, "enabled", False):
            return sync_result

        dag = runtime.dag
        state = runtime.routing_state
        revision = getattr(runtime, "revision", None)
        epoch = getattr(runtime, "epoch", None)
        routing_snapshot = node.metadata.get("routing")
        try:
            query = build_contextual_query(node, getattr(runtime, "dag", None))
            await asyncio.wait_for(embedding_adapter.embed(query), timeout=max(0.0, float(timeout)))
            if (
                runtime.dag is not dag
                or runtime.routing_state is not state
                or getattr(runtime, "revision", None) != revision
                or getattr(runtime, "epoch", None) != epoch
                or dag.get_node(node.msg_id) is not node
                or node.metadata.get("routing") is not routing_snapshot
            ):
                return sync_result
            return self.route(runtime, node, bot_names=bot_names, timeout=timeout)
        except Exception as exc:
            logger.debug("[Router] Neural escalation timed out or failed (%s); using hashed fallback", type(exc).__name__)
            return sync_result

    async def rerank_pending(self, runtime: Any, node: ConversationNode, reranker: Any) -> None:
        from .topic_reranker import RerankCandidate

        async with _state_guard(runtime):
            dag, state = runtime.dag, runtime.routing_state
            if dag is None or dag.get_node(node.msg_id) is not node:
                return
            snapshot = node.metadata.get("routing", {})
            if snapshot.get("topic_status") == "unformed":
                return
            source_id = snapshot.get("topic_id", "")
            new_topic = source_id == node.msg_id
            if (not snapshot.get("topic_ambiguous") and not new_topic) or snapshot.get("rerank_attempted"):
                return
            revision, epoch = getattr(runtime, "revision", None), getattr(runtime, "epoch", None)
            topic_ids = [tid for _, tid in snapshot.get("topic_candidates", []) if tid != source_id]
            topic_ids.extend(t.topic_id for t in sorted(state.topics.values(), key=lambda t: t.updated_at, reverse=True)
                             if t.topic_id != source_id)
            candidates = []
            for tid in dict.fromkeys(topic_ids):
                topic = state.topics.get(tid)
                if topic is None:
                    continue
                recent = [dag.nodes[mid] for mid in topic.message_ids if mid in dag.nodes
                          and 0 < node.timestamp - dag.nodes[mid].timestamp <= self.topic_resolver.window_seconds]
                if not recent:
                    continue
                candidates.append(RerankCandidate(tid, topic.label, tuple(
                    f"{n.user_id}: {n.text}" for n in recent[-3:])))
                if len(candidates) == 3:
                    break
            if not candidates:
                return
            snapshot["rerank_attempted"] = True
        decision = await reranker.rerank(umo=runtime.umo, text=node.text,
                                         candidates=candidates, ambiguous=True)
        async with _state_guard(runtime):
            if (runtime.dag is not dag or runtime.routing_state is not state
                    or getattr(runtime, "revision", None) != revision or getattr(runtime, "epoch", None) != epoch
                    or dag.get_node(node.msg_id) is not node
                    or node.metadata.get("routing") is not snapshot
                    or (not new_topic and node.msg_id not in state.pending_assignments)):
                return
            topic_id = node.msg_id if decision.choice == "NEW" else decision.topic_id
            if decision.choice == "NEW" and not can_start_topic(node.text):
                return
            if not topic_id or (decision.choice != "NEW" and
                                (topic_id not in state.topics or topic_id not in {c.topic_id for c in candidates})):
                return
            self._remember(state, node, topic_id, dag=dag)
            if source_id != topic_id and source_id in state.topics and not state.topics[source_id].message_ids:
                state.topics.pop(source_id)
                state.archive.pop(source_id)
            state.pending_assignments.pop(node.msg_id, None)
            snapshot.update(topic_id=topic_id, topic_ambiguous=False, topic_status="committed",
                            topic_confidence=0.78, ambiguous=snapshot.get("addressee_confidence", 0.0) < 0.72)
            snapshot["evidence"] = list(snapshot.get("evidence", [])) + ["topic_llm_rerank"]
            node.metadata["topic_id"] = topic_id
            state.last_topic_id = topic_id

    async def title_topic(self, runtime: Any, node: ConversationNode, reranker: Any) -> None:
        """Generate display metadata without holding the session lock during I/O."""
        async with _state_guard(runtime):
            state, dag = runtime.routing_state, runtime.dag
            if dag is None or dag.get_node(node.msg_id) is not node:
                return
            snapshot = node.metadata.get("routing", {})
            topic_id = snapshot.get("topic_id")
            topic = state.topics.get(topic_id)
            if (topic is None or topic.generated_title or topic.title_in_flight
                    or time.monotonic() < topic.title_retry_at):
                return
            messages = [dag.nodes[mid].text for mid in topic.message_ids if mid in dag.nodes][-5:]
            if not messages:
                return
            topic.title_attempted = True
            topic.title_in_flight = True
            revision, epoch = getattr(runtime, "revision", None), getattr(runtime, "epoch", None)
        title = ""
        failed = False
        try:
            title = await reranker.title(umo=runtime.umo, messages=messages)
            failed = not bool(title)
        except Exception as exc:
            failed = True
            logger.debug("[Router] Topic title unavailable (%s)", type(exc).__name__)
        finally:
            async with _state_guard(runtime):
                topic.title_in_flight = False
                if failed:
                    topic.title_failures = min(topic.title_failures + 1, 5)
                    topic.title_retry_at = time.monotonic() + min(300.0, 30.0 * 2 ** (topic.title_failures - 1))
                if (title and runtime.routing_state is state and runtime.dag is dag
                        and getattr(runtime, "revision", None) == revision
                        and getattr(runtime, "epoch", None) == epoch
                        and state.topics.get(topic_id) is topic
                        and dag.get_node(node.msg_id) is node
                        and node.metadata.get("routing") is snapshot
                        and snapshot.get("topic_id") == topic_id):
                    topic.generated_title = title
                    topic.label = title
                    topic.title_failures = 0
                    topic.title_retry_at = 0.0
                    for mid in topic.message_ids:
                        if mid in dag.nodes:
                            dag.nodes[mid].metadata["topic_title"] = title

    def observe_bot_message(self, runtime: Any, node: ConversationNode) -> RoutingInference:
        """Call only after successful delivery, when the bot node is in the DAG."""
        result = self.route(runtime, node)
        state = getattr(runtime, "routing_state", None)
        if state is not None:
            state.last_bot_topic_id = result.topic_id
            state.last_topic_id = result.topic_id
        if hasattr(runtime, "last_bot_node"):
            runtime.last_bot_node = node
        dag = getattr(runtime, "dag", None)
        bot_id = getattr(runtime, "bot_id", "")
        reply_to = getattr(node, "reply_to_id", None)
        if dag is not None and reply_to:
            trigger_node = dag.get_node(reply_to)
            if trigger_node and trigger_node.user_id and trigger_node.user_id != bot_id:
                if hasattr(runtime, "last_interlocutor"):
                    runtime.last_interlocutor = trigger_node.user_id
        return result
