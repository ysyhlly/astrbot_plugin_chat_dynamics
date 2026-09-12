"""AstrBot event parsing and delivery helpers.

Keeps plugin code off invented APIs (event.send_message / provider.text_chat)
and extracts At / Reply / self_id from real AstrMessageEvent shapes.
"""

from __future__ import annotations

import logging
import hashlib
import inspect
import re
import copy
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger("astrbot_plugin_chat_dynamics.platform_bridge")


def _session_label(session_id: Any) -> str:
    return hashlib.sha256(str(session_id or "").encode("utf-8", "ignore")).hexdigest()[:12]

try:
    from astrbot.api.event import MessageChain
except ImportError:  # test / standalone fallback
    class MessageChain:  # type: ignore[no-redef]
        def __init__(self, chain: Optional[List[Any]] = None) -> None:
            self.chain: List[Any] = list(chain or [])

        def message(self, text: str) -> "MessageChain":
            self.chain.append(str(text))
            return self

        def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
            parts = []
            for item in self.chain:
                parts.append(item if isinstance(item, str) else getattr(item, "text", str(item)))
            return "".join(parts)


_AT_IN_TEXT_RE = re.compile(r"@([^\s@]+)")
_COMMAND_PREFIXES = ("/", "／")
_MEDIA_TYPES = {
    "face", "image", "sticker", "emoji", "animatedemoji", "record", "video", "file",
    "wechatemoji", "forward", "node", "json",
}
_NON_MEDIA_TYPES = {
    "at", "atall", "reply", "plain", "text", "markdown", "mention", "poke",
}
_POKE_TYPES = frozenset({"poke"})
_MEDIA_PLACEHOLDER_RE = re.compile(
    r"^(?:\[(?:图片|表情|贴纸|动画表情|语音|视频|文件|转发|json|sticker|image|face|emoji|record|video|file|forward)\])+$",
    re.I,
)
_MEDIA_OUTLINE_RE = re.compile(
    r"\[(图片|表情|贴纸|动画表情|语音|视频|文件|转发|json|sticker|image|face|emoji|record|video|file|forward)\]",
    re.I,
)
_POKE_OUTLINE_RE = re.compile(r"\[(?:戳一戳|poke)\]", re.I)
_POKE_TEXT_RE = re.compile(r"^\[(?:戳一戳|poke)\]", re.I)
_UNDERSTANDABLE_MEDIA = frozenset({
    "image", "record", "audio", "voice", "video", "file",
})


@dataclass
class ParsedEvent:
    """Normalized view of an incoming AstrMessageEvent."""

    group_id: str
    sender_id: str
    self_id: str
    message_id: str
    text: str
    mentions: List[str] = field(default_factory=list)
    reply_to_id: Optional[str] = None
    unified_msg_origin: str = ""
    is_at_or_wake: bool = False
    is_command: bool = False
    outline: str = ""
    has_media: bool = False
    media_component_types: List[str] = field(default_factory=list)
    media_only: bool = False
    has_poke: bool = False
    poke_target_id: str = ""
    poke_at_bot: bool = False
    reply_sender_id: str = ""


@dataclass(frozen=True)
class SendResult:
    """Outcome of one platform send attempt."""

    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


def _safe_call(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    attr = getattr(obj, name, None)
    if attr is None:
        return default
    if callable(attr):
        try:
            return attr()
        except TypeError:
            return default
        except Exception:
            return default
    return attr


def _comp_type_name(comp: Any) -> str:
    return type(comp).__name__


def _poke_target_id(comp: Any) -> str:
    """Read the poke target across AstrBot 4.16 (`qq`) and 4.27 (`id` / `target_id()`)."""
    getter = getattr(comp, "target_id", None)
    if callable(getter):
        try:
            value = getter()
            if value not in (None, "", "None", 0, "0"):
                return str(value)
        except Exception:
            pass
    for key in ("qq", "user_id"):
        value = getattr(comp, key, None)
        if value not in (None, "", "None", 0, "0"):
            return str(value)
    value = getattr(comp, "id", None)
    if value not in (None, "", "None", 0, "0"):
        return str(value)
    return ""


def poke_event_text(*, poke_at_bot: bool, poke_target_id: str = "") -> str:
    if poke_at_bot:
        return "[戳一戳] 有人戳了你"
    if poke_target_id:
        return "[戳一戳] 有人戳了别人"
    return "[戳一戳]"


def is_poke_placeholder(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    return bool(_POKE_TEXT_RE.match(stripped)) and len(stripped) <= 40


def _coerce_success(value: Any) -> bool:
    """Interpret structured adapter success flags without treating ``\"false\"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "0", "no", "n", "error", "failed", "failure"}:
            return False
        if normalized in {"true", "1", "yes", "y", "ok", "success", "succeeded"}:
            return True
    return bool(value)


def _extract_components(event: Any) -> List[Any]:
    if event is None:
        return []
    if hasattr(event, "get_messages"):
        try:
            comps = event.get_messages()
            if comps:
                return list(comps)
        except Exception:
            pass
    msg_obj = getattr(event, "message_obj", None)
    if msg_obj is not None:
        comps = getattr(msg_obj, "message", None)
        if comps:
            return list(comps)
    return []


def extract_mentions_and_reply(event: Any, text: str) -> tuple[List[str], Optional[str]]:
    """Reads At / Reply components and @name tokens from plain text."""
    mentions: List[str] = []
    reply_to_id: Optional[str] = None

    for comp in _extract_components(event):
        type_name = _comp_type_name(comp)
        lowered = type_name.lower()
        if lowered == "atall":
            continue
        if lowered == "at":
            uid = getattr(comp, "qq", None)
            if uid is None:
                uid = getattr(comp, "user_id", None)
            if uid is not None and str(uid) not in ("", "None", "0"):
                mentions.append(str(uid))
            continue
        if lowered == "reply":
            rid = getattr(comp, "id", None)
            if rid is None:
                rid = getattr(comp, "message_id", None)
            if rid is not None:
                reply_to_id = str(rid)

    for token in _AT_IN_TEXT_RE.findall(text or ""):
        cleaned = token.strip().strip("，。！？,.!?:：;；")
        if cleaned:
            mentions.append(cleaned)

    # de-dupe, preserve order
    seen = set()
    unique: List[str] = []
    for m in mentions:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique, reply_to_id


_COMMAND_FILTER_NAMES = frozenset({"CommandFilter", "CommandGroupFilter"})
_OWN_MESSAGE_HANDLERS = frozenset({"on_group_message"})


def is_command_like(text: str, extra_prefixes: Optional[List[str]] = None) -> bool:
    """True when the message should be left to command handlers, not chat dynamics."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    prefixes = list(_COMMAND_PREFIXES)
    if extra_prefixes:
        prefixes.extend(extra_prefixes)
    return any(stripped.startswith(p) for p in prefixes if p)


def _original_message_text(event: Any, stripped: str) -> str:
    """Wake-prefix stripping mutates event.message_str; the adapter copy often keeps '/'."""
    msg_obj = getattr(event, "message_obj", None)
    if msg_obj is not None:
        raw = str(getattr(msg_obj, "message_str", "") or "")
        if raw.strip() and raw.strip() != (stripped or "").strip():
            return raw
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            value = str(getter() or "")
            if value.strip():
                return value
        except Exception:
            pass
    return stripped


def host_command_activated(event: Any) -> bool:
    """True when AstrBot already matched another plugin's command/group command.

    WakingCheck strips wake prefixes (often ``/``) before handlers run, so
    ``/签到`` arrives as ``签到``. Slash-prefix detection misses it and this
    plugin would otherwise talk over the real command handler.
    """
    if event is None:
        return False
    get_extra = getattr(event, "get_extra", None)
    extras: Any = None
    handlers: Any = None
    if callable(get_extra):
        try:
            extras = get_extra("handlers_parsed_params")
        except TypeError:
            try:
                extras = get_extra("handlers_parsed_params", None)
            except Exception:
                extras = None
        except Exception:
            extras = None
        try:
            handlers = get_extra("activated_handlers")
        except TypeError:
            try:
                handlers = get_extra("activated_handlers", None)
            except Exception:
                handlers = None
        except Exception:
            handlers = None
    if isinstance(extras, dict) and extras:
        return True
    for handler in handlers or ():
        handler_name = str(getattr(handler, "handler_name", "") or "")
        fn = getattr(handler, "handler", None)
        fn_name = str(getattr(fn, "__name__", "") or "")
        if handler_name in _OWN_MESSAGE_HANDLERS or fn_name in _OWN_MESSAGE_HANDLERS:
            continue
        for filt in getattr(handler, "event_filters", ()) or ():
            name = type(filt).__name__
            if name in _COMMAND_FILTER_NAMES:
                return True
            if getattr(filt, "command_name", None):
                return True
    return False


def parse_group_event(event: Any, command_prefixes: Optional[List[str]] = None) -> ParsedEvent:
    """Normalizes an AstrMessageEvent (or test double) into ParsedEvent."""
    group_id = str(_safe_call(event, "get_group_id", "") or "")
    sender_id = str(_safe_call(event, "get_sender_id", "unknown") or "unknown")
    self_id = str(_safe_call(event, "get_self_id", "") or "")
    text = str(getattr(event, "message_str", "") or "")
    original_text = _original_message_text(event, text)

    message_id = getattr(event, "message_id", None)
    if not message_id:
        msg_obj = getattr(event, "message_obj", None)
        message_id = getattr(msg_obj, "message_id", None) if msg_obj is not None else None
    message_id = str(message_id) if message_id else ""

    mentions, reply_to_id = extract_mentions_and_reply(event, text)
    is_at = bool(getattr(event, "is_at_or_wake_command", False))
    # Only treat a real At component of the bot as a mention. Platform wake
    # words are too generic to inject self_id (that would force STRONG).

    outline = str(_safe_call(event, "get_message_outline", "") or "")
    comps = _extract_components(event)
    reply_sender_id = ""
    for comp in comps:
        if _comp_type_name(comp).lower() != "reply":
            continue
        rid = getattr(comp, "id", None)
        if rid is None:
            rid = getattr(comp, "message_id", None)
        if str(rid) != reply_to_id or reply_to_id == message_id:
            continue
        sender = str(getattr(comp, "sender_id", "") or "")
        reply_sender_id = sender if sender not in {"0", "None"} else ""
    media_component_types: List[str] = []
    poke_target_id = ""
    has_poke = False
    for comp in comps:
        component_name = _comp_type_name(comp)
        lowered = component_name.lower()
        if lowered in _POKE_TYPES:
            has_poke = True
            if not poke_target_id:
                poke_target_id = _poke_target_id(comp)
            continue
        # Unknown components are treated conservatively as media. This keeps
        # a new adapter component from accidentally opening an LLM call while
        # retaining a stable type name for diagnostics.
        if lowered in _MEDIA_TYPES or lowered not in _NON_MEDIA_TYPES:
            media_component_types.append(component_name)
    has_media = bool(media_component_types)
    if not has_media and outline:
        has_media = bool(_MEDIA_OUTLINE_RE.search(outline))
        if has_media:
            media_component_types.append("outline")
    if not has_poke and _POKE_OUTLINE_RE.search(f"{outline or ''} {text or ''}"):
        has_poke = True
    poke_at_bot = bool(poke_target_id and self_id and str(poke_target_id) == str(self_id))
    if has_poke and not text.strip():
        text = poke_event_text(poke_at_bot=poke_at_bot, poke_target_id=poke_target_id)
    media_only = bool(has_media and (not text.strip() or _MEDIA_PLACEHOLDER_RE.fullmatch(text.strip())))

    umo = str(getattr(event, "unified_msg_origin", "") or group_id)

    return ParsedEvent(
        group_id=group_id,
        sender_id=sender_id,
        self_id=self_id,
        message_id=message_id,
        text=text,
        mentions=mentions,
        reply_to_id=reply_to_id,
        reply_sender_id=reply_sender_id,
        unified_msg_origin=umo,
        is_at_or_wake=is_at,
        is_command=is_command_like(text, command_prefixes)
        or is_command_like(original_text, command_prefixes)
        or host_command_activated(event),
        outline=outline,
        has_media=has_media,
        media_component_types=media_component_types,
        media_only=media_only,
        has_poke=has_poke,
        poke_target_id=poke_target_id,
        poke_at_bot=poke_at_bot,
    )


def iter_message_components(event: Any) -> List[Any]:
    """Prefer get_messages(); fall back to message_obj.message."""
    comps = _extract_components(event)
    return list(comps or [])


def has_understandable_media(parsed: ParsedEvent) -> bool:
    """True when the turn carries image/voice/file the host agent can consume."""
    types = {str(item).lower() for item in (parsed.media_component_types or ())}
    if types & _UNDERSTANDABLE_MEDIA:
        return True
    blob = f"{parsed.outline or ''} {parsed.text or ''}"
    return bool(
        re.search(
            r"\[(图片|语音|视频|文件|image|record|audio|voice|video|file)\]",
            blob,
            re.I,
        )
    )


def media_placeholder_text(parsed: ParsedEvent) -> str:
    types = {str(item).lower() for item in (parsed.media_component_types or ())}
    blob = f"{parsed.outline or ''} {parsed.text or ''}"
    if types & {"record", "audio", "voice"} or "语音" in blob:
        return "[语音]"
    if types & {"image"} or "图片" in blob:
        return "[图片]"
    if types & {"video"} or "视频" in blob:
        return "[视频]"
    if types & {"file"} or "文件" in blob:
        return "[文件]"
    if types & {"poke"} or "戳一戳" in blob or "poke" in blob.lower():
        return "[戳一戳]"
    return "[媒体附件]"


async def _component_file_ref(comp: Any) -> str:
    for method in ("convert_to_file_path", "get_file"):
        fn = getattr(comp, method, None)
        if not callable(fn):
            continue
        try:
            value = fn()
            if inspect.isawaitable(value):
                value = await value
            if value:
                return str(value)
        except Exception:
            continue
    for key in ("url", "file", "path"):
        value = getattr(comp, key, None)
        if value not in (None, "", "None"):
            return str(value)
    return ""


def _is_image_component(comp: Any) -> bool:
    return type(comp).__name__.lower() == "image"


def _is_audio_component(comp: Any) -> bool:
    return type(comp).__name__.lower() in {"record", "audio", "voice"}


async def collect_media_urls(event: Any) -> tuple[list[str], list[str]]:
    """Collect local/remote image and audio refs from an AstrMessageEvent."""
    images: list[str] = []
    audios: list[str] = []
    seen: set[str] = set()

    async def _take(comp: Any) -> None:
        path = await _component_file_ref(comp)
        if not path or path in seen:
            return
        if _is_image_component(comp):
            seen.add(path)
            images.append(path)
        elif _is_audio_component(comp):
            seen.add(path)
            audios.append(path)

    for comp in iter_message_components(event):
        await _take(comp)
        chain = getattr(comp, "chain", None) or []
        for inner in chain:
            await _take(inner)
    return images, audios


def _make_reply_component(message_id: str) -> Any:
    try:
        from astrbot.api.message_components import Reply
        return Reply(id=str(message_id))
    except Exception:
        return type("Reply", (), {"id": str(message_id)})()


def build_plain_chain(text: str, reply_to_id: Optional[str] = None) -> MessageChain:
    chain = MessageChain()
    if reply_to_id:
        reply_comp = _make_reply_component(reply_to_id)
        if hasattr(chain, "chain") and isinstance(chain.chain, list):
            chain.chain.append(reply_comp)
    return chain.message(text)


def _make_poke_component(target_id: str) -> Any:
    target = str(target_id or "")
    try:
        from astrbot.api.message_components import Poke
        try:
            poke = Poke(id=target, qq=target)
        except TypeError:
            poke = Poke(type="poke", qq=target, id=target)
        try:
            if not getattr(poke, "qq", None):
                poke.qq = target
            if not getattr(poke, "id", None):
                poke.id = target
        except Exception:
            pass
        return poke
    except Exception:
        return type("Poke", (), {"id": target, "qq": target})()


def build_poke_chain(target_id: str) -> MessageChain:
    chain = MessageChain()
    poke = _make_poke_component(target_id)
    if hasattr(chain, "chain") and isinstance(chain.chain, list):
        chain.chain.append(poke)
    return chain


_STREAMING_TYPE_NAMES = frozenset({"STREAMING_RESULT", "STREAMING_FINISH"})


def is_streaming_host_result(result: Any) -> bool:
    """True when AstrBot already streamed this result to the user.

    STREAMING_RESULT is the live stream; STREAMING_FINISH is the completed
    transcript. Both have already been delivered. Rewriting them with
    ``plain_result()`` drops the marker and the host sends the same text again.
    """
    if result is None:
        return False
    content_type = getattr(result, "result_content_type", None)
    if content_type is None:
        return False
    name = getattr(content_type, "name", None)
    if name:
        return str(name).upper() in _STREAMING_TYPE_NAMES
    text = str(content_type).upper().replace(" ", "")
    return any(token in text for token in _STREAMING_TYPE_NAMES)


_PLAIN_RESULT_TYPES = frozenset({"plain", "text", "at", "atall", "markdown", "mention"})


def result_has_rich_media(result: Any) -> bool:
    """True when a host result still carries image/voice/file components."""
    if result is None:
        return False
    chain = getattr(result, "chain", None)
    if chain is None and hasattr(result, "get_chain"):
        try:
            chain = result.get_chain()
        except Exception:
            chain = None
    items = list(chain or [])
    for item in items:
        nested = getattr(item, "chain", None)
        if nested:
            items.extend(list(nested))
        name = type(item).__name__.lower()
        if name not in _PLAIN_RESULT_TYPES and name not in {"str"}:
            if isinstance(item, str):
                continue
            return True
    return False


def chain_plain_text(chain: Any) -> str:
    if chain is None:
        return ""
    if isinstance(chain, str):
        return chain
    if hasattr(chain, "get_plain_text"):
        try:
            return str(chain.get_plain_text())
        except TypeError:
            return str(chain.get_plain_text(False))
        except Exception:
            pass
    if hasattr(chain, "chain"):
        parts = []
        for item in chain.chain:
            if isinstance(item, str):
                parts.append(item)
            else:
                parts.append(getattr(item, "text", str(item)))
        return "".join(parts)
    return str(chain)


async def send_plain(
    event: Any,
    context: Any,
    session_id: str,
    text: Any,
    reply_to_id: Optional[str] = None,
) -> SendResult:
    """Dispatches a plain-text fragment through official AstrBot send APIs.

    Preference order:
    1. event.send(MessageChain)
    2. context.send_message(unified_msg_origin, MessageChain)
    Returns success separately from the optional platform message id.
    """
    if isinstance(text, str):
        chain = build_plain_chain(text, reply_to_id=reply_to_id)
    else:
        # Main-agent tool results may contain images/files; preserve their MessageChain.
        chain = copy.copy(text)
        chain.chain = list(text.chain)
        if reply_to_id:
            chain.chain.insert(0, _make_reply_component(reply_to_id))
    result = None
    try:
        if event is not None and hasattr(event, "send"):
            result = event.send(chain)
            if inspect.isawaitable(result):
                result = await result
        elif context is not None and hasattr(context, "send_message"):
            umo = getattr(event, "unified_msg_origin", None) if event is not None else None
            target = umo or session_id
            result = context.send_message(target, chain)
            if inspect.isawaitable(result):
                result = await result
        else:
            logger.info(
                "[ChatDynamics] No send API available; dropping fragment for session %s",
                _session_label(session_id),
            )
            return SendResult(False, error="no send API available")
    except Exception as exc:
        # Keep platform exception details out of logs; callers still receive
        # the original error text for programmatic diagnostics and tests.
        logger.error("[ChatDynamics] Failed to send fragment code=CD_SEND_FAILED type=%s", type(exc).__name__)
        return SendResult(False, error=str(exc))

    if result is False:
        return SendResult(False, error="platform rejected the message")
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        return SendResult(bool(result), error=None if result else "platform rejected the message")
    if result is None:
        # AstrMessageEvent.send() commonly returns no value on success.
        return SendResult(True)
    if isinstance(result, SendResult):
        return result
    if isinstance(result, dict) and any(key in result for key in ("success", "ok", "status")):
        raw_success = result.get("success", result.get("ok", result.get("status")))
        if (
            isinstance(raw_success, (int, float))
            and not isinstance(raw_success, bool)
            and "status" in result
            and "success" not in result
            and "ok" not in result
        ):
            # HTTP-like status fields use 2xx for success; a boolean/numeric
            # success field keeps the direct truthiness contract.
            success = 200 <= raw_success < 300
        elif isinstance(raw_success, (int, float)) and not isinstance(raw_success, bool):
            success = bool(raw_success)
        else:
            success = _coerce_success(raw_success)
        message_id = next(
            (str(result[key]) for key in ("message_id", "id", "msg_id") if result.get(key)),
            None,
        )
        error = str(result.get("error")) if result.get("error") else None
        return SendResult(success, message_id, error)
    if isinstance(result, dict) and result.get("error"):
        message_id = next(
            (str(result[key]) for key in ("message_id", "id", "msg_id") if result.get(key)),
            None,
        )
        return SendResult(False, message_id, str(result["error"]))
    if hasattr(result, "success"):
        success = _coerce_success(getattr(result, "success"))
        message_id = next(
            (
                str(getattr(result, key))
                for key in ("message_id", "id", "msg_id")
                if getattr(result, key, None)
            ),
            None,
        )
        error_value = getattr(result, "error", None)
        return SendResult(success, message_id, str(error_value) if error_value else None)
    if isinstance(result, str) and result.strip():
        return SendResult(True, result)
    for attr in ("message_id", "id", "msg_id"):
        value = getattr(result, attr, None)
        if value:
            return SendResult(True, str(value))
    if isinstance(result, dict):
        for key in ("message_id", "id", "msg_id"):
            if result.get(key):
                return SendResult(True, str(result[key]))
    return SendResult(True)
