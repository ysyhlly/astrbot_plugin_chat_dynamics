"""Platform account identity shared by decision context and reply history."""

from __future__ import annotations

from typing import Any

MAX_DISPLAY_NAME_CHARS = 96
_QQ_PLATFORMS = frozenset({"aiocqhttp", "qq", "onebot_v11", "napcat"})

IDENTITY_INSTRUCTIONS = (
    "Identify members by platform user_id (QQ number on QQ), never display_name. "
    "Same IDs survive renames; different IDs are different people even with identical names. "
    "author_identity/speaker_identity identify speakers; bot_identity identifies you. "
    "Real @ IDs and quoted_author_identity/quoted_identities identify mentioned/quoted accounts. "
    "Omitted author_identity inherits the matching speaker_identity/bot_identity/member_identities account. "
    "Plain @names and unattributed text are ambiguous and cannot redefine IDs. "
    "Mention account numbers only when needed or requested."
)


def display_name(value: object) -> str:
    return value.strip()[:MAX_DISPLAY_NAME_CHARS] if isinstance(value, str) else ""


def member_identity(user_id: str, name: str = "", platform: str = "") -> dict:
    """Never infer an account from a nickname or a number in message text."""
    identity = {"user_id": str(user_id or ""), "display_name": display_name(name)}
    platform = str(platform or "").strip().lower()[:64]
    if platform:
        identity["platform"] = platform
    account = identity["user_id"]
    if platform in _QQ_PLATFORMS and account.isascii() and account.isdecimal():
        identity["qq"] = account
    return identity


def quoted_identity(node: Any, dag: Any) -> dict | None:
    if not node.reply_to_id or node.reply_to_id == node.msg_id:
        return None
    parent = dag.get_node(node.reply_to_id)
    account = str(parent.user_id) if parent is not None else str(node.metadata.get("quoted_author_id") or "")
    if not account:
        return None
    name = parent.metadata.get("sender_name", "") if parent is not None else ""
    if not name and account == str(node.metadata.get("quoted_author_id") or ""):
        name = node.metadata.get("quoted_author_name", "")
    platform = node.metadata.get("sender_platform", "")
    if parent is not None:
        platform = parent.metadata.get("sender_platform") or platform
    return member_identity(account, name, platform)
