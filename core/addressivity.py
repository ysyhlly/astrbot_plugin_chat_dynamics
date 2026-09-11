"""Addressivity Router and Scoring Engine for AstrBot Group Chat Dynamics.

Evaluates whether incoming messages target the bot (Strong Address),
target other participants / ambient chat (Weak Address), or fall into
an ambiguous Safe Hover zone (0.4 - 0.7) where messages are silently buffered.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, List, Optional, Set

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
    ):
        self.score: float = round(float(score), 4)
        self.level: AddressivityLevel = level
        self.is_bot_targeted: bool = is_bot_targeted
        self.target_user_id: Optional[str] = target_user_id
        self.reasons: List[str] = list(reasons) if reasons else []
        self.topic_relevance: float = max(0.0, min(1.0, round(float(topic_relevance), 4)))

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
        reasons: List[str] = []

        # 1. Check direct message attributes
        # Rule 1a: Explicit @mention of bot id or any configured nickname
        mention_hits = self._matching_mentions(node.mentioned_users, b_id, names)
        if mention_hits:
            reasons.append(f"Explicit @mention of bot ({mention_hits[0]})")
            return AddressivityScore(
                score=1.0,
                level=AddressivityLevel.STRONG,
                is_bot_targeted=True,
                target_user_id=b_id,
                reasons=reasons,
                topic_relevance=1.0,
            )

        # A human mention is a stronger recipient signal than a nickname or quote.
        other_mentions = [u for u in node.mentioned_users if u != b_id]
        if other_mentions:
            return AddressivityScore(0.15, AddressivityLevel.WEAK, False,
                                     other_mentions[0], ["Explicit @mention of other user(s)"])

        routing = node.metadata.get("routing") or {}
        if not isinstance(routing, dict):
            if hasattr(routing, "__dataclass_fields__"):
                from dataclasses import asdict
                routing = asdict(routing)
            else:
                routing = {}
        for name in names:
            if self._name_mentioned_in_text(name, node.text):
                return AddressivityScore(0.95, AddressivityLevel.STRONG, True, b_id,
                                         [f"Bot name '{name}' used as a vocative"], 1.0)

        parent_node = dag.get_node(node.reply_to_id) if node.reply_to_id else None
        if parent_node is not None and parent_node.user_id == b_id:
            return AddressivityScore(0.98, AddressivityLevel.STRONG, True, b_id,
                                     [f"Explicit reply to bot message {node.reply_to_id}"], 1.0)

        confidence = float(routing.get("bot_addressee_confidence", 0.0) or 0.0)
        if (routing.get("bot_is_addressee") and confidence >= 0.72
                and not routing.get("ambiguous", False)):
            return AddressivityScore(max(self.strong_threshold, min(0.94, confidence)),
                                     AddressivityLevel.STRONG, True, b_id,
                                     ["Conversation routing identifies bot as addressee"],
                                     float(routing.get("topic_confidence", 0.0) or 0.0))
        recipients = routing.get("addressee_ids") or []
        if (recipients and b_id not in recipients
                and float(routing.get("addressee_confidence", 0.0) or 0.0) >= 0.72
                and not routing.get("ambiguous", False)):
            return AddressivityScore(0.15, AddressivityLevel.WEAK, False, recipients[0],
                                     ["Conversation routing identifies another addressee"])
        if routing.get("subject_is_bot") and not routing.get("bot_is_addressee"):
            return AddressivityScore(0.20, AddressivityLevel.WEAK, False, None,
                                     ["Bot is the subject, not the identified addressee"])

        # 2. No explicit pointers; analyze weak addressivity / semantic continuity with last bot message
        is_human_quote = (parent_node is not None and parent_node.user_id != b_id)
        if not last_bot_node:
            reasons.append("No prior bot message in session context")
            base_score = 0.20
            if is_human_quote:
                base_score = max(0.05, round(base_score - 0.15, 4))
                reasons.append("Human quote penalty (-0.15)")
            return AddressivityScore(
                score=base_score,
                level=AddressivityLevel.WEAK,
                is_bot_targeted=False,
                target_user_id=parent_node.user_id if parent_node else None,
                reasons=reasons,
            )

        score = 0.20  # Baseline ambient probability
        topic_relevance = 0.0
        if is_human_quote:
            score -= 0.15
            reasons.append("Human quote penalty (-0.15)")
        if node.metadata.get("is_wake"):
            score += 0.15
            reasons.append("Platform wake flag without explicit @mention")

        # Factor A: Temporal proximity (decay over time)
        time_diff = max(0.0, node.timestamp - last_bot_node.timestamp)
        if not is_human_quote:
            if time_diff < 15.0:
                time_factor = 0.25
                reasons.append(f"Immediate temporal proximity ({time_diff:.1f}s <= 15s)")
            elif time_diff < 45.0:
                time_factor = 0.15
                reasons.append(f"Close temporal proximity ({time_diff:.1f}s <= 45s)")
            elif time_diff < 120.0:
                time_factor = 0.05
                reasons.append(f"Moderate temporal proximity ({time_diff:.1f}s <= 120s)")
            else:
                time_factor = -0.10
                reasons.append(f"Stale conversation gap ({time_diff:.1f}s > 120s)")
            score += time_factor

        # Factor B: Intervening human messages
        # Count how many messages from other users occurred between last_bot_node and current node
        recent = dag.get_recent_nodes(limit=20)
        bot_idx = -1
        for idx, n in enumerate(recent):
            if n.msg_id == last_bot_node.msg_id:
                bot_idx = idx
                break

        if bot_idx != -1:
            # The candidate node is normally already in the DAG.  Count only
            # messages strictly between the last bot output and this node.
            intervening_count = sum(
                1
                for n in recent[bot_idx + 1 :]
                if n.msg_id != node.msg_id
                and last_bot_node.timestamp < n.timestamp <= node.timestamp
            )
            if intervening_count == 0:
                # Direct immediate follow-up to bot
                score += 0.05
                reasons.append("Zero intervening messages since bot output")
            elif intervening_count <= 2:
                reasons.append(f"{intervening_count} intervening messages")
            else:
                score -= 0.15
                reasons.append(f"{intervening_count} intervening messages (thread divergence)")

        # Factor C: Conversational continuation cues
        text = node.text.strip()
        for cue in self.CONTINUATION_CUES:
            if time_diff < 120.0 and cue in text:
                score += 0.15
                reasons.append(f"Contains continuation cue '{cue}'")
                break

        # Factor D: Surface lexical overlap for the score boost; hashed
        # embedding + concept classifier feed topic_relevance / WTS only.
        bot_words = lexical_tokens(last_bot_node.text)
        user_words = lexical_tokens(text)
        match_fn = semantic_match_fn or semantic_match
        match = match_fn(last_bot_node.text, text)
        topic_relevance = max(topic_relevance, match.score)
        if bot_words and user_words:
            overlap = bot_words.intersection(user_words)
            if overlap:
                overlap_ratio = len(overlap) / min(len(bot_words), len(user_words))
                if overlap_ratio >= 0.25 or len(overlap) >= 2:
                    score += 0.15
                    topic_relevance = max(topic_relevance, min(1.0, overlap_ratio))
                    reasons.append(f"Lexical keyword overlap: {list(overlap)[:3]}")
                elif len(overlap) == 1:
                    score += 0.05
                    topic_relevance = max(topic_relevance, min(0.4, overlap_ratio))
                    reasons.append(f"Minor keyword overlap: {list(overlap)}")
        elif match.embedding_cosine >= 0.62:
            score += 0.05
            reasons.append("High embedding cosine without surface overlap")

        # Factor E: Router bot addressee confidence bonus
        bot_addressee_conf = float(routing.get("bot_addressee_confidence", 0.0) or 0.0)
        if bot_addressee_conf > 0.0:
            routing_bonus = round(bot_addressee_conf * 0.20, 4)
            score += routing_bonus
            topic_relevance = max(topic_relevance, float(routing.get("topic_confidence", 0.0) or 0.0))
            reasons.append(f"Router bot addressee confidence bonus (+{routing_bonus:.2f})")

        # Factor F: Active interlocutor bonus gated on topic coherence or parent continuity
        interlocutor_id = ""
        if last_bot_node.reply_to_id:
            trigger_node = dag.get_node(last_bot_node.reply_to_id)
            if trigger_node:
                interlocutor_id = trigger_node.user_id
        interlocutor_id = (
            interlocutor_id
            or getattr(last_bot_node, "metadata", {}).get("trigger_user_id", "")
            or (getattr(runtime, "last_interlocutor", "") if runtime else "")
        )
        node_topic = node_topic_id(node)
        bot_topic = node_topic_id(last_bot_node)
        same_topic = bool(node_topic and bot_topic and node_topic == bot_topic)
        parent_continuity = bool(
            (node.reply_to_id and node.reply_to_id == last_bot_node.msg_id)
            or routing.get("parent_message_id") == last_bot_node.msg_id
            or last_bot_node.msg_id in node.parent_ids
        )
        if interlocutor_id and node.user_id == interlocutor_id and (same_topic or parent_continuity):
            score += 0.12
            topic_relevance = max(topic_relevance, 0.45)
            reasons.append("Active interlocutor dialogue continuation bonus (+0.12)")

        # Same explicit thread as the last bot turn (reply / @ / fragment chain).
        # Semantic-only candidate edges do not merge thread_id, so this does not
        # promote mere lexical clustering into a strong address.
        if last_bot_node.thread_id and node.thread_id == last_bot_node.thread_id:
            score += 0.08
            topic_relevance = max(topic_relevance, 0.35)
            reasons.append("Shares explicit thread with last bot turn")

        # A later message from the same author can resolve earlier hover turns.
        # The first ambiguous fragment stays silent; evidence accumulates.
        hover_nodes: List[ConversationNode] = []
        for candidate in list(prior_hovers or []):
            if candidate is not None and candidate not in hover_nodes:
                hover_nodes.append(candidate)
        if prior_hover is not None and prior_hover not in hover_nodes:
            hover_nodes.append(prior_hover)
        current_tokens = lexical_tokens(text)
        hover_bonus = 0.0
        for hover in hover_nodes:
            if hover.user_id != node.user_id:
                continue
            if not (0.0 <= node.timestamp - hover.timestamp <= 120.0):
                continue
            hover_tokens = lexical_tokens(hover.text)
            hover_overlap = hover_tokens.intersection(current_tokens)
            follows_hover = node.reply_to_id == hover.msg_id or hover.msg_id in node.parent_ids
            has_followup_cue = any(cue in text for cue in self.CONTINUATION_CUES)
            if follows_hover or has_followup_cue or hover_overlap:
                hover_bonus += 0.12 if hover_bonus else 0.20
                topic_relevance = max(topic_relevance, 0.65 if follows_hover else 0.45)
        if hover_bonus:
            score += min(0.28, hover_bonus)
            reasons.append("Follow-up resolves a pending safe-hover turn")

        # Clamp score to [0.0, 1.0]
        score = max(0.0, min(1.0, score))

        # Classify Level
        if score >= self.strong_threshold:
            level = AddressivityLevel.STRONG
            is_targeted = True
        elif score >= self.hover_threshold:
            level = AddressivityLevel.SAFE_HOVER
            is_targeted = False  # Not definitively targeted; safe hover buffers silently
        else:
            level = AddressivityLevel.WEAK
            is_targeted = False

        target_uid = b_id if is_targeted else (parent_node.user_id if parent_node else (recipients[0] if recipients else None))
        return AddressivityScore(
            score=score,
            level=level,
            is_bot_targeted=is_targeted,
            target_user_id=target_uid,
            reasons=reasons,
            topic_relevance=topic_relevance,
        )

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
        if not name or not text:
            return False
        name = name.strip()
        if not name:
            return False

        # @name with a token boundary after it
        at_pat = r"@" + re.escape(name) + r"(?:\b|$|[\s,，。！？!?:：])"
        if re.search(at_pat, text, flags=re.IGNORECASE):
            return True

        if re.search(r"[\u4e00-\u9fff]", name) and len(name) < 2:
            return False
        # Name occurrence alone describes a subject. A vocative requires a
        # standalone call, an imperative/question, or a second-person request.
        boundary = r"(?![A-Za-z0-9_])" if name.isascii() else ""
        match = re.match(r"^\s*(?:(?:喂|嗨|hi|hello)[，,\s]*)?" + re.escape(name) + boundary + r"(.*)$", text, re.IGNORECASE)
        if not match:
            return False
        tail = match.group(1)
        if not tail.strip(" \t,，。!！?？:："):
            return True
        tail = tail.lstrip(" \t,，:：!！")
        return bool(re.match(
            r"(?:第[一二三四五六七八九十0-9]+[问个]|帮|请|能不能|能否|可以|在吗|在不在|出来|回答|查|算|你|怎么看|怎么做|为什么|继续|说说|讲讲|看一下|看图|看看|看下|早上好|你好|晚上好|"
            r"please\b|can\s+you\b|could\s+you\b|help\b|what\s+do\s+you\b)",
            tail, re.IGNORECASE))

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        """ASCII words plus adjacent Chinese character bigrams for topical overlap."""
        return lexical_tokens(text)
