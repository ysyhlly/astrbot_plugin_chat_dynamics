"""Bounded, session-local conversation routing; similarity is evidence, not addressivity."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from .graph import ConversationDAG, ConversationNode
from .session_runtime import RoutingState, TopicState

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


@dataclass
class RoutingInference:
    """Comprehensive outcome of conversation topology and addressee inference."""

    topic_id: str = ""
    topic_confidence: float = 0.0
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


def build_contextual_query(node: ConversationNode, dag: Any, max_len: int = 12) -> str:
    """Short elliptical turns borrow only the author's recent substantive context."""
    text = node.text.strip()
    if len(text) > 16 or dag is None:
        return text
    is_elliptical = len(text) <= max_len or bool(_ELLIPTICAL_RE.search(text))
    if not is_elliptical:
        return text
    for previous in reversed(dag.get_recent_nodes(WINDOW_NODES)):
        if previous.msg_id == node.msg_id or not 0 < node.timestamp - previous.timestamp <= 90:
            continue
        if previous.user_id == node.user_id and len(previous.text.strip()) >= 8:
            return previous.text.strip() + "\n" + text
    return text


def _lexical_similarity(text1: str, text2: str) -> float:
    """Compute lexical token overlap supporting both English words and Chinese characters."""
    tokens1 = set(re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text1.lower()))
    tokens2 = set(re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text2.lower()))
    if not tokens1 or not tokens2:
        return 0.0
    return len(tokens1 & tokens2) / max(1, len(tokens1))


class TopicResolver:
    """Resolves topic membership using 5-factor composite scoring and TTL expiration."""

    def __init__(
        self,
        join_threshold: float = TOPIC_JOIN_THRESHOLD,
        ambiguity_threshold: float = TOPIC_AMBIGUITY_THRESHOLD,
        margin_threshold: float = TOPIC_MARGIN_THRESHOLD,
        window_seconds: float = WINDOW_SECONDS,
        max_exemplars: int = 8,
    ):
        self.join_threshold = join_threshold
        self.ambiguity_threshold = ambiguity_threshold
        self.margin_threshold = margin_threshold
        self.window_seconds = window_seconds
        self.max_exemplars = max_exemplars

    @staticmethod
    def remember(state: RoutingState, node: ConversationNode, topic_id: str) -> None:
        """Assign turn to topic, removing prior assignment to prevent double-counting."""
        for topic in state.topics.values():
            if node.msg_id in topic.message_ids:
                topic.message_ids.remove(node.msg_id)
        topic = state.topics.setdefault(topic_id, TopicState(topic_id))
        topic.message_ids.append(node.msg_id)
        topic.participants.add(node.user_id)
        topic.updated_at = max(topic.updated_at, node.timestamp)

    def score_topic(
        self,
        node: ConversationNode,
        dag: ConversationDAG,
        topic: TopicState,
        matches: dict[str, float],
    ) -> float:
        """Evaluate topic affinity with 5-factor composite equation."""
        exemplars = [mid for mid in topic.message_ids[-self.max_exemplars:] if mid in matches]
        if not exemplars:
            return 0.0
        past_nodes = [dag.nodes[mid] for mid in exemplars if mid in dag.nodes]
        if not past_nodes:
            return 0.0

        # Factor 1: Semantic similarity (0.55)
        sim = max(matches[mid] for mid in exemplars)

        # Factor 2: Recency (0.15)
        updated_at = max(n.timestamp for n in past_nodes)
        recency = max(0.0, 1.0 - (node.timestamp - updated_at) / self.window_seconds)

        # Factor 3: Participant continuity (0.15)
        participants = {n.user_id for n in past_nodes}
        participant = 1.0 if node.user_id in participants else 0.0

        # Factor 4: Lexical overlap (0.10)
        lexical = max(_lexical_similarity(node.text, n.text) for n in past_nodes)

        # Factor 5: Conversational lineage (0.05)
        lineage = 0.0
        if node.reply_to_id and node.reply_to_id in topic.message_ids:
            lineage = 1.0
        elif any(u in participants for u in node.mentioned_users):
            lineage = 0.5
        elif any(n.thread_id == node.thread_id for n in past_nodes if node.thread_id):
            lineage = 0.5

        score = (
            sim * 0.55
            + recency * 0.15
            + participant * 0.15
            + lexical * 0.10
            + lineage * 0.05
        )
        return round(score, 4)

    def resolve(
        self,
        node: ConversationNode,
        dag: ConversationDAG,
        state: RoutingState,
        matches: dict[str, float],
        explicit_parent: Optional[ConversationNode] = None,
    ) -> Tuple[str, float, bool, List[str], Optional[str], List[Tuple[float, str]]]:
        """Resolves topic. Returns (topic_id, conf, is_ambiguous, evidence, second_topic, ranked_topics)."""
        if explicit_parent is not None:
            topic_id = (
                explicit_parent.metadata.get("routing", {}).get("topic_id")
                or explicit_parent.thread_id
            )
            return topic_id, 1.0, False, [], None, []

        ranked: List[Tuple[float, str]] = []
        for topic in state.topics.values():
            score = self.score_topic(node, dag, topic, matches)
            if score > 0.0:
                ranked.append((score, topic.topic_id))

        ranked.sort(reverse=True)
        if not ranked:
            return node.msg_id, 1.0, False, [], None, []

        best_score, best_topic = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        second_topic = ranked[1][1] if len(ranked) > 1 else None
        margin = best_score - second_score

        if best_score >= self.join_threshold and margin >= self.margin_threshold:
            return best_topic, best_score, False, ["topic_similarity"], second_topic, ranked
        elif best_score >= self.ambiguity_threshold:
            return best_topic, best_score, True, ["topic_ambiguous"], second_topic, ranked
        else:
            return node.msg_id, 1.0, False, [], None, ranked


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
        f_part = 0.5

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
            cand_topic = (
                candidate.metadata.get("routing", {}).get("topic_id")
                or candidate.thread_id
            )
            if cand_topic not in target_topics:
                continue

            sim = matches.get(candidate.msg_id, 0.0)
            score = self.score_candidate(
                node=node,
                candidate=candidate,
                sim=sim,
                is_primary_topic=(cand_topic == primary_topic),
                turn_distance=turn_distance,
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
    """Precedence-driven addressee resolver for group chat conversation routing.

    Implements a strict 6-tier precedence hierarchy:
    1. Tier 1: Explicit Mention (@mention)
    2. Tier 2: Explicit Platform Reply / Quote
       (Special sub-case: Quoted Subject in Active Bot Dialogue)
    3. Tier 3: Inferred Parent (Topological edge from ParentRetriever)
    4. Tier 4: Active Interlocutor (Dialogue continuation fallback)
    5. Tier 5: Turn-taking / Vocative Address (Direct name call cues)
    6. Tier 6: Unknown / Ambient Broadcast
    """

    VOCATIVE_CUES: Tuple[str, ...] = (
        "你", "帮", "看看", "看下", "回答", "为什么", "怎么看", "怎么做",
        "请", "能", "能不能", "能否", "在吗", "在不在", "出来", "查", "算",
    )

    @classmethod
    def is_vocative_call(cls, text: str, bot_names: Sequence[str]) -> bool:
        """Detect direct vocative address targeting bot while filtering 3rd-person subject remarks."""
        if not text or not bot_names:
            return False
        cleaned = text.strip()
        for name in bot_names:
            if not name:
                continue
            name_str = str(name).strip()
            if not name_str:
                continue
            # Third-person demonstrative prefix filter (e.g. "这个bot怎么老不回")
            demonstrative = rf"(?:这(?:个)?|那(?:个)?|某个|哪(?:个)?|现在的|本群的)\s*{re.escape(name_str)}"
            if re.search(demonstrative, cleaned, re.IGNORECASE):
                continue
            # Vocative address pattern: bot name at front, followed by vocative cues or standalone punctuation
            cues_pat = "|".join(re.escape(cue) for cue in cls.VOCATIVE_CUES)
            vocative_pattern = rf"^\s*(?:[喂嗨hihelloHIHELLO]+\s*[，,\s]*)?{re.escape(name_str)}[，,\s:：!！？?]*(?:{cues_pat}|$)"
            if re.search(vocative_pattern, cleaned, re.IGNORECASE):
                return True
        return False

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

        # Tier 2: Explicit Platform Reply / Quote
        elif quoted_node is not None:
            # Sub-case 2A: Quoted Subject in Active Bot Dialogue
            if last_bot is not None and quoted_node.user_id != bot_id:
                last_bot_reply_to = getattr(last_bot, "reply_to_id", None)
                trigger = dag.get_node(last_bot_reply_to) if (last_bot_reply_to and dag) else None
                interlocutor = trigger.user_id if trigger else getattr(runtime, "last_interlocutor", "")
                last_bot_meta = getattr(last_bot, "metadata", {}) or {}
                bot_topic = (
                    last_bot_meta.get("routing", {}).get("topic_id")
                    if isinstance(last_bot_meta, dict) else None
                ) or getattr(last_bot, "thread_id", "")
                last_bot_ts = getattr(last_bot, "timestamp", 0.0)
                elliptical = bool(re.fullmatch(r"\s*(?:那|这个|这样|这个呢|那这个呢|那怎么办|这个怎么办)[呢？?。!！]*\s*", node.text))
                competing = any(
                    n.user_id not in {node.user_id, bot_id}
                    and n.timestamp > last_bot_ts
                    and (
                        (n.metadata.get("routing", {}).get("topic_id") if isinstance(n.metadata, dict) else None)
                        or getattr(n, "thread_id", "")
                    ) == bot_topic
                    for n in recent_nodes
                )
                if (
                    elliptical
                    and interlocutor == node.user_id
                    and topic_id == bot_topic
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

        # Tier 5 Check: Direct vocative call takes precedence if no explicit platform pointers
        elif vocative_target and bot_id:
            addressee_ids = [bot_id]
            addressee_confidence = 0.95
            evidence.append("direct_name_call")

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
            last_bot_meta = getattr(last_bot, "metadata", {}) or {}
            bot_topic = (
                last_bot_meta.get("routing", {}).get("topic_id")
                if isinstance(last_bot_meta, dict) else None
            ) or getattr(last_bot, "thread_id", "")
            last_bot_ts = getattr(last_bot, "timestamp", 0.0)
            competing = any(
                n.user_id not in {node.user_id, bot_id}
                and n.timestamp > last_bot_ts
                and (
                    (n.metadata.get("routing", {}).get("topic_id") if isinstance(n.metadata, dict) else None)
                    or getattr(n, "thread_id", "")
                ) == bot_topic
                for n in recent_nodes
            )
            followup = bool(re.search(r"然后|继续|那|这个|这样|怎么办|呢[？?]?$", node.text))
            if (
                interlocutor == node.user_id
                and 0 < node.timestamp - last_bot_ts <= 60
                and followup
                and not competing
                and (topic_id == bot_topic or not ranked_topics or ranked_topics[0][0] < 0.35)
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
    ):
        if topic_resolver is None:
            self.topic_resolver = TopicResolver(
                window_seconds=topic_window_seconds,
                ambiguity_threshold=topic_join_threshold,
                join_threshold=max(0.58, topic_join_threshold + 0.10) if topic_join_threshold < 0.58 else topic_join_threshold,
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

        query = build_contextual_query(node, dag)
        matches: dict[str, float] = {}
        for candidate in recent:
            match = dag.semantic_match_fn(query, candidate.text)
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
        result.topic_id = topic_id
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
            result.topic_id = ptopic
            result.topic_confidence = 0.78
            result.parent_message_id = pmid
            result.parent_confidence = pconf
            if hasattr(dag, "link_inferred_reply"):
                dag.link_inferred_reply(node.msg_id, pmid, confidence=pconf, reason="active_interlocutor_followup")
            else:
                dag._link_parent(node.msg_id, pmid, kind="inferred_reply")

        result.ambiguous = result.addressee_confidence < 0.72
        node.metadata["topic_id"] = result.topic_id
        node.metadata["routing"] = asdict(result)
        self._remember(state, node, result.topic_id)
        state.last_topic_id = result.topic_id
        if node.user_id == getattr(runtime, "bot_id", ""):
            state.last_bot_topic_id = result.topic_id
            if hasattr(runtime, "last_bot_node"):
                runtime.last_bot_node = node
        state.prune(dag, now, window_seconds=self.topic_resolver.window_seconds)

        # 5. Structured [Router] Diagnostic Logging
        preview = (node.text or "").strip().replace("\n", " ")
        if len(preview) > 30:
            preview = preview[:27] + "..."
        logger.info(
            "[Router] msg_id=%s sender=%s text=%r | "
            "topic=%s (conf=%.2f, ranked=%s) | "
            "parent=%s (conf=%.2f, possible=%s, margin=%.2f, candidates=%s) | "
            "addressee=%s (conf=%.2f, bot_is_addressee=%s, bot_conf=%.2f) | "
            "subject=%s (subject_is_bot=%s) | "
            "decision: ambiguous=%s evidence=%s",
            node.msg_id,
            node.user_id,
            preview,
            result.topic_id,
            result.topic_confidence,
            [(tid, round(s, 2)) for s, tid in ranked_topics[:3]],
            result.parent_message_id or "none",
            result.parent_confidence,
            result.possible_parent or "none",
            parent_margin,
            [(mid, round(s, 2)) for s, mid in candidates[:3]],
            result.addressee_ids,
            result.addressee_confidence,
            result.bot_is_addressee,
            result.bot_addressee_confidence,
            result.subject_user_ids,
            result.subject_is_bot,
            result.ambiguous,
            result.evidence,
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
