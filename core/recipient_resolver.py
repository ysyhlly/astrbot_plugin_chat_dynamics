"""Single recipient inference owner; topic scores are context, not identity."""
from dataclasses import dataclass
import re
from typing import Any, List, Optional, Sequence, Tuple
from .bot_identity import BotIdentityMatcher
from .graph import ConversationNode
from .topic_identity import node_topic_id
from .topic_resolution import is_elliptical


@dataclass(frozen=True)
class RecipientInference:
    recipient_ids: tuple[str, ...]
    confidence: float
    bot_targeted: bool
    bot_confidence: float
    subject_user_ids: tuple[str, ...]
    bot_is_subject: bool
    evidence: tuple[str, ...]
    parent_override: Optional[Tuple[str, float, str]] = None

    @property
    def addressee_ambiguous(self) -> bool:
        return self.confidence < 0.72

    @property
    def explicit(self) -> bool:
        return bool(set(self.evidence) & {"explicit_mention", "platform_wake", "direct_name_call", "explicit_reply"})


class RecipientResolver:
    """Resolve mentions and direct name calls before quotes and inferred parents.

    A quoted message can be the subject of a direct request to the bot. Without
    an explicit recipient, bounded active-dialogue evidence provides a fallback.
    """

    @classmethod
    def is_vocative_call(cls, text: str, bot_names: Sequence[str]) -> bool:
        """Detect direct vocative address targeting bot while filtering 3rd-person subject remarks."""
        return BotIdentityMatcher.match(text, bot_names).vocative

    @classmethod
    def is_subject_reference(cls, text: str, bot_names: Sequence[str]) -> bool:
        """Subject and addressee are independent identity evidence."""
        return BotIdentityMatcher.match(text, bot_names).subject

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

        # Preserve all explicit recipients, including a textual bot vocative.
        is_wake = bool(node.metadata.get("is_wake"))
        reference = BotIdentityMatcher.match(node.text, names,
            mentions=node.mentioned_users, bot_id=bot_id)
        aliases = {name.casefold() for name in names}
        explicit_ids = list(dict.fromkeys(
            bot_id if bot_id and str(uid).casefold() in aliases else str(uid)
            for uid in node.mentioned_users if uid))
        if bot_id and (is_wake or reference.mention or vocative_target):
            if bot_id not in explicit_ids:
                explicit_ids.append(bot_id)
        if explicit_ids:
            addressee_ids = explicit_ids
            addressee_confidence = 1.0 if node.mentioned_users or is_wake else 0.95
            if node.mentioned_users:
                evidence.append("explicit_mention")
            if is_wake:
                evidence.append("platform_wake")
            if vocative_target:
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
            short_followup = bool(re.fullmatch(r"(?:然后呢|为什么|那这个呢|真的吗|怎么弄)[？?。!！\s]*", node.text.strip()))
            followup = short_followup or bool(re.search(r"然后|继续|那|这个|这样|怎么办|呢[？?]?$", node.text))
            if not bot_topic:
                # With no semantic topic, only a short continuation can borrow
                # the active interlocutor; a new sentence containing "那" cannot.
                followup = short_followup or is_elliptical(node.text) or bool(re.fullmatch(
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

    def infer(self, **kwargs) -> RecipientInference:
        ids, confidence, targeted, bot_conf, subjects, subject, evidence, parent = self.resolve(**kwargs)
        if kwargs.get("quoted_node") is not None and not evidence:
            evidence = ["explicit_reply"]
        return RecipientInference(tuple(ids), confidence, targeted, bot_conf,
                                  tuple(subjects), subject, tuple(evidence), parent)
