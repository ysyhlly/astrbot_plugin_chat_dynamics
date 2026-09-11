"""Session-local attribution; explicit evidence stays separate from guesses."""

from dataclasses import dataclass

from .topic_identity import node_topic_id
from .semantics import classify_message, concept_scores


@dataclass(frozen=True)
class MessageSemantics:
    sender_id: str
    sender_is_bot: bool
    recipient_ids: tuple[str, ...]
    basis: str
    certainty: str
    evidence_message_id: str
    mentioned_user_ids: tuple[str, ...]
    scenes: tuple[str, ...]
    emotions: tuple[str, ...]
    intent: str
    quoted_message_id: str = ""
    quoted_author_id: str = ""
    topic_id: str = ""
    topic_confidence: float = 0.0
    parent_message_id: str = ""
    parent_confidence: float = 0.0
    addressee_confidence: float = 0.0
    subject_user_ids: tuple[str, ...] = ()
    subject_is_bot: bool = False
    bot_is_addressee: bool = False
    bot_addressee_confidence: float = 0.0
    routing_ambiguous: bool = False
    routing_evidence: tuple[str, ...] = ()

    @property
    def inferred_parent_id(self) -> str:
        """Return parent_message_id when inferred rather than explicitly quoted."""
        if self.quoted_message_id and self.parent_message_id == self.quoted_message_id:
            return ""
        return self.parent_message_id

    @property
    def bot_is_subject(self) -> bool:
        """Alias for subject_is_bot indicating the bot is discussed in the third person."""
        return bool(self.subject_is_bot)


def describe_message(node, dag, bot_id: str = "") -> MessageSemantics:
    mentions = tuple(dict.fromkeys(str(uid) for uid in
                                  node.metadata.get("actual_mentions", node.mentioned_users) if uid))
    recipients, basis, certainty, evidence = (), "unknown", "unknown", ""
    routing = node.metadata.get("routing") or {}
    if not isinstance(routing, dict):
        if hasattr(routing, "__dataclass_fields__"):
            from dataclasses import asdict
            routing = asdict(routing)
        else:
            routing = {}
    parent = dag.get_node(node.reply_to_id) if node.reply_to_id else None
    quoted_author = str(parent.user_id) if parent is not None and parent.msg_id != node.msg_id else ""
    if mentions:
        recipients, basis, certainty = mentions, "mention", "explicit"
    elif routing.get("addressee_ids"):
        recipients = tuple(str(uid) for uid in routing["addressee_ids"])
        basis = "routing"
        evidence = str(routing.get("parent_message_id") or "")
        conf = float(routing.get("addressee_confidence", 0.0) or 0.0)
        is_ambiguous = bool(routing.get("ambiguous", False))
        if routing.get("explicit_mention"):
            certainty, basis = "explicit", "mention"
        elif routing.get("explicit_reply") and not routing.get("bot_is_addressee"):
            certainty, basis = "explicit", "reply"
        elif conf >= 0.72 and not is_ambiguous:
            certainty = "probable"
        else:
            certainty = "possible"
    elif node.reply_to_id:
        evidence, basis = node.reply_to_id, "reply"
        if quoted_author:
            recipients, certainty = (quoted_author,), "explicit"
    elif any(kind == "inferred_reply" for kind in node.edge_kinds.values()):
        for mid, kind in node.edge_kinds.items():
            if kind == "inferred_reply":
                candidate = dag.get_node(mid)
                if candidate is not None and candidate.user_id != node.user_id:
                    recipients = (str(candidate.user_id),)
                    basis = "inferred_reply"
                    certainty = "possible"
                    evidence = str(candidate.msg_id)
                    break
    scenes, emotions = classify_message(node.text)

    topic_id = node_topic_id(node)
    topic_conf = float(routing.get("topic_confidence", 0.0) or 0.0)
    parent_mid = str(
        routing.get("parent_message_id")
        or node.metadata.get("inferred_parent_id")
        or (evidence if basis in {"reply", "inferred_reply"} else "")
        or (node.reply_to_id or "")
    )
    parent_conf = float(routing.get("parent_confidence", 1.0 if node.reply_to_id else 0.0) or 0.0)
    addr_conf = float(routing.get("addressee_confidence", 1.0 if basis in {"mention", "reply"} else 0.0) or 0.0)
    subj_users = tuple(str(uid) for uid in routing.get("subject_user_ids", ()))
    subj_is_bot = bool(routing.get("subject_is_bot"))
    bot_is_addr = bool(routing.get("bot_is_addressee") if "bot_is_addressee" in routing else (bot_id and bot_id in recipients))
    bot_addr_conf = float(
        routing.get(
            "bot_addressee_confidence",
            1.0 if (bot_id and bot_id in recipients and certainty == "explicit") else (addr_conf if bot_is_addr else 0.0)
        ) or 0.0
    )
    is_ambig = bool(routing.get("ambiguous", certainty not in {"explicit", "probable"}))
    rout_ev = tuple(str(item) for item in routing.get("evidence", ()))

    return MessageSemantics(str(node.user_id), bool(bot_id and node.user_id == bot_id),
                            recipients, basis, certainty, evidence, mentions, scenes, emotions,
                            "question" if concept_scores(node.text).get("question") else "unknown",
                            node.reply_to_id or "", quoted_author,
                            topic_id,
                            topic_conf,
                            parent_mid,
                            parent_conf,
                            addr_conf,
                            subj_users,
                            subj_is_bot,
                            bot_is_addr,
                            bot_addr_conf,
                            is_ambig,
                            rout_ev)
