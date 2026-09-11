"""Addressivity Router and Scoring Engine for AstrBot Group Chat Dynamics.

Evaluates whether incoming messages target the bot (Strong Address),
target other participants / ambient chat (Weak Address), or fall into
an ambiguous Safe Hover zone (0.4 - 0.7) where messages are silently buffered.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from enum import Enum
from typing import Any, List, Optional, Set

from .bot_identity import BotIdentityMatcher
from .participation_policy import Evidence, ParticipationPolicy, ParticipationSnapshot, RecipientSnapshot
from .routing_contract import addressee_is_ambiguous
from .topic_identity import node_topic_id
from .graph import ConversationDAG, ConversationNode
from .semantics import lexical_tokens, semantic_match

logger = logging.getLogger("astrbot_plugin_chat_dynamics.addressivity")


class AddressivityLevel(str, Enum):
    """Classification levels for message addressivity."""

    STRONG = "strong"      # Score >= 0.7: Strong indication targeting the bot
    SAFE_HOVER = "hover"   # Score 0.4 - 0.7: Ambiguous; buffered silently into DAG without immediate reply
    WEAK = "weak"          # Score < 0.4: Directed at others or general background noise


class AddressivityScore:
    """Detailed score result of addressivity evaluation."""

    def __init__(
        self,
        score: float,
        level: AddressivityLevel,
        is_bot_targeted: bool,
        target_user_id: Optional[str] = None,
        reasons: Optional[List[str]] = None,
        topic_relevance: float = 0.0,
        *,
        evidence=(),
        family_contributions=(),
        contribution_total=None,
    ):
        self.score: float = round(float(score), 4)
        self.level: AddressivityLevel = level
        self.is_bot_targeted: bool = is_bot_targeted
        self.target_user_id: Optional[str] = target_user_id
        self.reasons: List[str] = list(reasons) if reasons else []
        self.topic_relevance: float = max(0.0, min(1.0, round(float(topic_relevance), 4)))

        self.evidence = tuple(evidence)
        self.family_contributions = dict(family_contributions)
        self.contribution_total = self.score if contribution_total is None else contribution_total

    def __repr__(self) -> str:
        return (
            f"AddressivityScore(score={self.score}, level={self.level.value}, "
            f"is_bot_targeted={self.is_bot_targeted}, target={self.target_user_id}, reasons={self.reasons})"
        )


class AddressivityRouter:
    """Routes and scores addressivity of conversation turns within a DAG."""

    # Common Chinese conversational follow-up triggers and pronouns indicating thread continuation
    CONTINUATION_CUES = {
        "为什么", "怎么", "如何", "然后呢", "然后", "真的吗", "真的假的",
        "你说的是", "确实", "不对吧", "还有呢", "继续", "比如呢", "啥意思",
        "细说", "后来呢", "具体点", "再讲讲", "原来如此", "你觉得呢", "你说呢",
        "你确定", "你刚刚", "你刚才", "赞同", "对啊", "不是吧", "好厉害",
    }

    def __init__(
        self,
        bot_id: str = "bot",
        bot_names: Optional[List[str]] = None,
        strong_threshold: float = 0.70,
        hover_threshold: float = 0.40,
        time_decay_half_life: float = 45.0,
    ):
        self.bot_id: str = bot_id
        self.bot_names: Set[str] = set(bot_names) if bot_names else {bot_id}
        self.strong_threshold: float = strong_threshold
        self.hover_threshold: float = hover_threshold
        self.time_decay_half_life: float = time_decay_half_life

    def compute_addressivity(
        self,
        node: ConversationNode,
        dag: ConversationDAG,
        last_bot_node: Optional[ConversationNode] = None,
        bot_id: Optional[str] = None,
        bot_names: Optional[List[str]] = None,
        prior_hover: Optional[ConversationNode] = None,
        prior_hovers: Optional[List[ConversationNode]] = None,
        semantic_match_fn: Optional[Any] = None,
        runtime: Optional[Any] = None,
    ) -> AddressivityScore:
        """Scores whether a message is directed at the bot.

        Args:
            node: The candidate ConversationNode to score.
            dag: The ConversationDAG containing recent context.
            last_bot_node: The most recent ConversationNode posted by the bot in this session.
            bot_id: Optional override for bot user ID.
            bot_names: Optional override for bot names/nicknames.

        Returns:
            AddressivityScore instance.
        """
        b_id = bot_id if bot_id is not None else self.bot_id
        names = set(bot_names) if bot_names is not None else self.bot_names
        routing = node.metadata.get("routing") or {}
        if hasattr(routing, "__dataclass_fields__"):
            from dataclasses import asdict
            routing = asdict(routing)
        if not isinstance(routing, dict):
            routing = {}
        recipient = RecipientSnapshot(
            ids=tuple(routing.get("addressee_ids") or ()),
            canonical=routing.get("routing_schema_version") == 2,
            ambiguous=addressee_is_ambiguous(routing),
            bot_targeted=bool(routing.get("bot_is_addressee")),
            confidence=float(routing.get("addressee_confidence", 0.0) or 0.0),
            bot_confidence=float(routing.get("bot_addressee_confidence", 0.0) or 0.0),
            topic_confidence=float(routing.get("topic_confidence", 0.0) or 0.0),
            subject_is_bot=bool(routing.get("subject_is_bot")),
        )
        policy = ParticipationPolicy(self.strong_threshold, self.hover_threshold)
        facts, details = [], []

        def observe(code, family, strength, source, reason=None):
            facts.append(Evidence(code, family, strength, source))
            if reason is not None:
                details.append((code, reason))

        # Collect legacy identity facts. The pure policy owns their precedence
        # and always consumes a resolved schema-2 recipient first.
        mention_hits = self._matching_mentions(node.mentioned_users, b_id, names)
        if mention_hits:
            observe("bot_mention", "recipient", 1.0, "message.mentions",
                    f"Explicit @mention of bot ({mention_hits[0]})")
        other_mentions = [u for u in node.mentioned_users if u != b_id]
        if other_mentions and not BotIdentityMatcher.match(node.text, names).vocative:
            observe("other_mention", "recipient", 1.0, "message.mentions", "Explicit @mention of other user(s)")
            details.append(("other_target", other_mentions[0]))
        for name in names:
            if self._name_mentioned_in_text(name, node.text):
                observe("vocative", "recipient", 1.0, "identity_matcher", f"Bot name '{name}' used as a vocative")
                break
        parent_node = dag.get_node(node.reply_to_id) if node.reply_to_id else None
        if parent_node is not None and parent_node.user_id == b_id:
            observe("bot_reply", "recipient", 1.0, "message.reply", f"Explicit reply to bot message {node.reply_to_id}")
        snapshot = ParticipationSnapshot(b_id, recipient, tuple(facts), tuple(details),
                                         parent_node.user_id if parent_node else None)
        direct = policy.explicit(snapshot)
        if direct is not None:
            return self._policy_score(direct)
        if parent_node is not None and parent_node.user_id != b_id:
            observe("human_quote", "recipient", 1.0, "message.reply")
        if not last_bot_node:
            return self._policy_score(policy.evaluate(replace(snapshot, observations=tuple(facts))))
        if node.metadata.get("is_wake"):
            observe("platform_wake", "platform", 1.0, "message.is_wake")
        time_diff = max(0.0, node.timestamp - last_bot_node.timestamp)
        observe("temporal_gap", "temporal", time_diff, "message.timestamp")
        recent = dag.get_recent_nodes(limit=20)
        bot_idx = next((idx for idx, item in enumerate(recent) if item.msg_id == last_bot_node.msg_id), -1)
        if bot_idx != -1:
            count = sum(1 for item in recent[bot_idx + 1:] if item.msg_id != node.msg_id
                        and last_bot_node.timestamp < item.timestamp <= node.timestamp)
            observe("intervening_messages", "dialogue", count, "dag.recent")
        text = node.text.strip()
        for cue in self.CONTINUATION_CUES:
            if time_diff < 120.0 and cue in text:
                observe("continuation_cue", "dialogue", 1.0, "continuation_matcher", f"Contains continuation cue '{cue}'")
                break
        bot_words, user_words = lexical_tokens(last_bot_node.text), lexical_tokens(text)
        match = (semantic_match_fn or semantic_match)(last_bot_node.text, text)
        overlap = bot_words.intersection(user_words)
        if bot_words and user_words:
            if overlap:
                observe("lexical_overlap", "topic", len(overlap) / min(len(bot_words), len(user_words)), "lexical_tokens")
        else:
            observe("embedding_without_tokens", "topic", match.embedding_cosine, "semantic_match")
        interlocutor_id = ""
        if last_bot_node.reply_to_id:
            trigger_node = dag.get_node(last_bot_node.reply_to_id)
            if trigger_node:
                interlocutor_id = trigger_node.user_id
        interlocutor_id = (interlocutor_id or last_bot_node.metadata.get("trigger_user_id", "")
                           or (getattr(runtime, "last_interlocutor", "") if runtime else ""))
        node_topic, bot_topic = node_topic_id(node), node_topic_id(last_bot_node)
        same_topic = bool(node_topic and bot_topic and node_topic == bot_topic)
        parent_continuity = bool((node.reply_to_id and node.reply_to_id == last_bot_node.msg_id)
                                 or routing.get("parent_message_id") == last_bot_node.msg_id
                                 or last_bot_node.msg_id in node.parent_ids)
        if interlocutor_id and node.user_id == interlocutor_id and (same_topic or parent_continuity):
            observe("active_interlocutor", "dialogue", 1.0, "dag.dialogue")
        if last_bot_node.thread_id and node.thread_id == last_bot_node.thread_id:
            observe("explicit_thread", "dialogue", 1.0, "dag.thread")
        hover_nodes = []
        for candidate in list(prior_hovers or []):
            if candidate is not None and candidate not in hover_nodes:
                hover_nodes.append(candidate)
        if prior_hover is not None and prior_hover not in hover_nodes:
            hover_nodes.append(prior_hover)
        for hover in hover_nodes:
            if hover.user_id != node.user_id or not (0.0 <= node.timestamp - hover.timestamp <= 120.0):
                continue
            follows_hover = node.reply_to_id == hover.msg_id or hover.msg_id in node.parent_ids
            if follows_hover or any(cue in text for cue in self.CONTINUATION_CUES) or lexical_tokens(hover.text).intersection(user_words):
                observe("pending_hover", "dialogue", float(follows_hover), "pending_hover")
        snapshot = replace(snapshot, observations=tuple(facts), reason_details=tuple(details),
                           has_prior_bot=True, semantic_score=match.score, lexical_overlap=tuple(overlap))
        return self._policy_score(policy.evaluate(snapshot))

    @staticmethod
    def _policy_score(decision) -> AddressivityScore:
        return AddressivityScore(decision.score, AddressivityLevel(decision.level), decision.is_bot_targeted,
            decision.target_user_id, list(decision.reasons), decision.topic_relevance,
            evidence=decision.evidence, family_contributions=decision.family_contributions,
            contribution_total=decision.contribution_total)

    @staticmethod
    def _matching_mentions(mentioned_users: List[str], bot_id: str, names: Set[str]) -> List[str]:
        hits: List[str] = []
        lowered_names = {n.lower() for n in names if n}
        bot_l = (bot_id or "").lower()
        for raw in mentioned_users:
            token = str(raw).strip()
            if not token:
                continue
            if token == bot_id or token.lower() == bot_l or token.lower() in lowered_names:
                hits.append(token)
        return hits

    @staticmethod
    def _name_mentioned_in_text(name: str, text: str) -> bool:
        """Match a bot nickname without substring traps like bot⊂both / 助手⊂助手席."""
        return BotIdentityMatcher.is_vocative(name, text)

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        """ASCII words plus adjacent Chinese character bigrams for topical overlap."""
        return lexical_tokens(text)
