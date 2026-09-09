"""L1 media air-reading gate (image/voice) + optional L2 understand-reply flag.

Heuristic-first: works offline without vision/STT. Failures quiet-degrade.
Never writes media details into memory payloads returned here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger("astrbot_plugin_chat_dynamics.media_gate")

# --- component / outline helpers -------------------------------------------------

_IMAGE_TYPES = frozenset({
    "image", "face", "sticker", "emoji", "animatedemoji", "wechatemoji", "outline",
})
_VOICE_TYPES = frozenset({"record", "audio", "voice"})
_OTHER_MEDIA = frozenset({"video", "file", "forward", "node", "json"})
_POKE_TYPES = frozenset({"poke"})

_OUTLINE_IMAGE = re.compile(
    r"\[(图片|表情|贴纸|动画表情|sticker|image|face|emoji)\]", re.I
)
_OUTLINE_VOICE = re.compile(r"\[(语音|record|audio|voice)\]", re.I)

_MEME_CAPTION = (
    "哈哈", "笑死", "草", "绷不住", "233", "hhh", "lol", "表情包", "整活",
    "绝了", "好家伙", "太真实了", "蚌埠住",
)
_HELP_CAPTION = (
    "帮我看", "帮我看看", "怎么弄", "怎么办", "求助", "报错", "看下这个",
    "看一下", "啥意思", "什么意思", "怎么解决", "为什么失败", "请教",
    "这是啥", "这啥", "识别一下", "翻译一下",
)
_LOOK_LISTEN = (
    "看这个", "听这个", "看看这个", "听听这个", "看图", "听语音",
    "帮我听", "帮我看图", "这张图", "这段语音",
)
_PRIVATE_MARKERS = (
    "身份证", "证件", "护照", "银行卡", "验证码", "密码", "住址", "手机号",
    "身份证号", "私密", "别外传", "别发群", "仅你看", "私聊", "转账截图",
    "账单", "工资条", "病历",
)
_SHORT_ACK = (
    "嗯", "哦", "噢", "喔", "好", "好的", "行", "收到", "ok", "OK", "嗯嗯",
    "嗷", "啊", "额", "呃", "6", "66", "哈哈", "hmmm", "m",
)
_VENT_MARKERS = (
    "难受", "烦死", "累死", "崩溃", "委屈", "想哭", "郁闷", "压力", "吐槽",
    "气死", "无语", "心累", "破防",
)
_HELP_TONE = (
    "怎么办", "帮我", "求助", "怎么弄", "听不清", "你听", "帮忙听",
)

_REASON = {
    "media_others_field": "媒体·别人的场",
    "voice_low_info": "语音·信息量低",
    "image_privacy_skip": "图片·隐私跳过",
    "media_listen": "媒体·不确定旁听",
    "media_meme_listen": "媒体·整活旁听",
    "media_ok": "媒体门闩通过",
    "media_off": "媒体门闩关闭",
    "no_media": "无媒体",
}


@dataclass(frozen=True)
class MediaGateVerdict:
    allow_speak: bool
    force_scale: float
    reason_code: str
    reason_zh: str
    labels: tuple[str, ...] = ()
    request_understand: bool = False
    skip_memory: bool = False
    privacy_hit: bool = False
    has_image: bool = False
    has_voice: bool = False
    multimodal_degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow_speak": self.allow_speak,
            "force_scale": self.force_scale,
            "reason_code": self.reason_code,
            "reason_zh": self.reason_zh,
            "labels": list(self.labels),
            "request_understand": self.request_understand,
            "skip_memory": self.skip_memory,
            "privacy_hit": self.privacy_hit,
            "has_image": self.has_image,
            "has_voice": self.has_voice,
            "multimodal_degraded": self.multimodal_degraded,
        }


_PASS = MediaGateVerdict(
    True, 1.0, "no_media", _REASON["no_media"], labels=(),
)


def _norm_types(types: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for item in types or ():
        name = str(item or "").strip()
        if name:
            out.append(name)
    return tuple(out)


def _lower_types(types: Sequence[str]) -> set[str]:
    return {t.lower() for t in types}


def detect_media_kinds(
    *,
    has_media: bool,
    media_component_types: Sequence[str] = (),
    outline: str = "",
    text: str = "",
) -> tuple[bool, bool]:
    """Return (has_image, has_voice) from component types / outline / placeholders."""
    lowered = _lower_types(_norm_types(media_component_types))
    outline_s = outline or ""
    text_s = text or ""
    has_image = bool(lowered & _IMAGE_TYPES) or bool(_OUTLINE_IMAGE.search(outline_s))
    has_voice = bool(lowered & _VOICE_TYPES) or bool(_OUTLINE_VOICE.search(outline_s))
    poke_only = bool(lowered) and lowered <= _POKE_TYPES
    if poke_only and not has_image and not has_voice:
        return False, False
    if not has_image and not has_voice and has_media:
        # Unknown media / video / file → treat conservatively as unclear image-like.
        leftover = lowered - _POKE_TYPES
        if leftover & _OTHER_MEDIA or "outline" in leftover or leftover or (has_media and not poke_only):
            if _OUTLINE_VOICE.search(outline_s) or "语音" in text_s:
                has_voice = True
            else:
                has_image = True
    if not has_image and ("[图片]" in text_s or "[表情]" in text_s or "[贴纸]" in text_s):
        has_image = True
    if not has_voice and ("[语音]" in text_s or "[record]" in text_s.lower()):
        has_voice = True
    return has_image, has_voice


def classify_image_label(
    *,
    text: str,
    outline: str = "",
    privacy_strict: bool = True,
) -> str:
    blob = f"{text or ''} {outline or ''}"
    if privacy_strict and any(m in blob for m in _PRIVATE_MARKERS):
        return "private_or_id_sensitive"
    if any(m in blob for m in _HELP_CAPTION) or any(m in blob for m in _LOOK_LISTEN):
        return "help_screenshot"
    # Short meme-ish captions or pure placeholder / sticker names.
    clean = (text or "").strip()
    if any(m in blob for m in _MEME_CAPTION):
        return "meme_banter"
    if not clean or re.fullmatch(
        r"(?:\[(?:图片|表情|贴纸|动画表情|sticker|image|face|emoji)\])+",
        clean,
        re.I,
    ):
        return "meme_banter"
    if len(clean) >= 24 or "截图" in blob or "文字" in blob:
        return "text_screenshot_maybe_addressed"
    return "unclear"


def classify_voice_label(*, text: str, outline: str = "") -> str:
    blob = f"{text or ''} {outline or ''}"
    clean = (text or "").strip()
    # Placeholder-only voice → low info unless caption says otherwise.
    placeholder = bool(
        re.fullmatch(r"(?:\[(?:语音|record|audio|voice)\])+", clean, re.I)
    ) or not clean
    if any(m in blob for m in _HELP_TONE) or any(m in blob for m in _LOOK_LISTEN):
        return "help_tone"
    if any(m in blob for m in _VENT_MARKERS) or len(clean) >= 40:
        return "long_vent"
    if placeholder or clean in _SHORT_ACK or len(clean) <= 4:
        # Very short ack / 嗯
        if clean in _SHORT_ACK or placeholder or len(clean) <= 2:
            return "short_ack"
    if "嘈杂" in blob or "听不清" in blob or "噪音" in blob:
        return "noisy_unclear"
    if len(clean) <= 8:
        return "short_ack"
    return "unclear"


def _strong_look_listen(text: str) -> bool:
    t = text or ""
    return any(k in t for k in _LOOK_LISTEN)


class MediaAirGate:
    """L1 speak/force gate for image & voice; L2 understand flag when enabled."""

    def __init__(self) -> None:
        self._multimodal_available: Optional[bool] = None

    def set_multimodal_available(self, available: Optional[bool]) -> None:
        self._multimodal_available = available

    def multimodal_available(self) -> bool:
        return bool(self._multimodal_available)

    def evaluate(
        self,
        *,
        text: str = "",
        has_media: bool = False,
        media_component_types: Sequence[str] = (),
        outline: str = "",
        explicit: bool = False,
        quoted_bot: bool = False,
        private_field_hint: bool = False,
        media_image_gate_enabled: bool = True,
        media_voice_gate_enabled: bool = True,
        media_understand_reply_enabled: bool = False,
        media_privacy_strict: bool = True,
        multimodal_available: Optional[bool] = None,
    ) -> MediaGateVerdict:
        try:
            return self._evaluate_inner(
                text=text,
                has_media=has_media,
                media_component_types=media_component_types,
                outline=outline,
                explicit=explicit,
                quoted_bot=quoted_bot,
                private_field_hint=private_field_hint,
                media_image_gate_enabled=media_image_gate_enabled,
                media_voice_gate_enabled=media_voice_gate_enabled,
                media_understand_reply_enabled=media_understand_reply_enabled,
                media_privacy_strict=media_privacy_strict,
                multimodal_available=multimodal_available,
            )
        except Exception as exc:  # quiet degrade — never break reply pipeline
            logger.info("[ChatDynamics] media_gate degrade: %s", type(exc).__name__)
            return MediaGateVerdict(
                True,
                1.0,
                "media_degrade",
                "媒体门闩降级放行",
                labels=("degrade",),
                multimodal_degraded=True,
            )

    def _evaluate_inner(
        self,
        *,
        text: str,
        has_media: bool,
        media_component_types: Sequence[str],
        outline: str,
        explicit: bool,
        quoted_bot: bool,
        private_field_hint: bool,
        media_image_gate_enabled: bool,
        media_voice_gate_enabled: bool,
        media_understand_reply_enabled: bool,
        media_privacy_strict: bool,
        multimodal_available: Optional[bool],
    ) -> MediaGateVerdict:
        types = _norm_types(media_component_types)
        has_image, has_voice = detect_media_kinds(
            has_media=has_media,
            media_component_types=types,
            outline=outline,
            text=text,
        )
        if not has_image and not has_voice and not has_media:
            return _PASS

        mm_flag = self._multimodal_available if multimodal_available is None else multimodal_available
        multimodal_degraded = mm_flag is False

        # Gates disabled → pass-through (L1 off means do not silence for media reasons).
        if has_image and not media_image_gate_enabled and has_voice and not media_voice_gate_enabled:
            return MediaGateVerdict(
                True, 1.0, "media_off", _REASON["media_off"],
                labels=("gate_off",), has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )
        if has_image and not media_image_gate_enabled and not has_voice:
            return MediaGateVerdict(
                True, 1.0, "media_off", _REASON["media_off"],
                labels=("image_gate_off",), has_image=True,
                multimodal_degraded=multimodal_degraded,
            )
        if has_voice and not media_voice_gate_enabled and not has_image:
            return MediaGateVerdict(
                True, 1.0, "media_off", _REASON["media_off"],
                labels=("voice_gate_off",), has_voice=True,
                multimodal_degraded=multimodal_degraded,
            )

        labels: list[str] = []
        image_label = ""
        voice_label = ""
        if has_image and media_image_gate_enabled:
            image_label = classify_image_label(
                text=text, outline=outline, privacy_strict=media_privacy_strict
            )
            labels.append(f"img:{image_label}")
        if has_voice and media_voice_gate_enabled:
            voice_label = classify_voice_label(text=text, outline=outline)
            labels.append(f"voice:{voice_label}")

        addressed = bool(explicit or quoted_bot)
        privacy_hit = image_label == "private_or_id_sensitive"
        skip_memory = bool(privacy_hit and media_privacy_strict)

        # Privacy strict: no detail description path; prefer silence unless strongly addressed.
        if privacy_hit and media_privacy_strict and not addressed:
            return MediaGateVerdict(
                False, 0.0, "image_privacy_skip", _REASON["image_privacy_skip"],
                labels=tuple(labels), request_understand=False, skip_memory=True,
                privacy_hit=True, has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )
        if privacy_hit and media_privacy_strict and addressed:
            # May acknowledge briefly; never request full multimodal describe.
            return MediaGateVerdict(
                True, 0.35, "image_privacy_skip", _REASON["image_privacy_skip"],
                labels=tuple(labels) + ("privacy_ack_only",),
                request_understand=False, skip_memory=True, privacy_hit=True,
                has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )

        # Private field (manners cue): media in someone else's space → listen.
        if private_field_hint and not addressed:
            return MediaGateVerdict(
                False, 0.15, "media_others_field", _REASON["media_others_field"],
                labels=tuple(labels) + ("others_field",),
                has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )

        # Voice short ack → silence unless addressed.
        if voice_label == "short_ack" and not addressed:
            return MediaGateVerdict(
                False, 0.1, "voice_low_info", _REASON["voice_low_info"],
                labels=tuple(labels), has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )

        # Meme mutual banter images → less speak (listen) unless @/quote.
        if image_label == "meme_banter" and not addressed:
            return MediaGateVerdict(
                False, 0.2, "media_meme_listen", _REASON["media_meme_listen"],
                labels=tuple(labels), has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )

        # Unclear / noisy → listen.
        if (image_label == "unclear" or voice_label in {"unclear", "noisy_unclear"}) and not addressed:
            return MediaGateVerdict(
                False, 0.25, "media_listen", _REASON["media_listen"],
                labels=tuple(labels), has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )

        # Help / vent / text screenshot facing bot → allow with scaled force.
        force = 1.0
        reason_code = "media_ok"
        reason_zh = _REASON["media_ok"]
        allow = True

        if image_label == "help_screenshot":
            force = 1.0 if addressed else 0.7
        elif image_label == "text_screenshot_maybe_addressed":
            force = 0.85 if addressed else 0.45
            if not addressed:
                allow = False
                reason_code = "media_listen"
                reason_zh = _REASON["media_listen"]
        elif voice_label == "help_tone":
            force = 1.0 if addressed else 0.65
        elif voice_label == "long_vent":
            force = 0.55 if addressed else 0.3
            if not addressed:
                allow = False
                reason_code = "media_listen"
                reason_zh = _REASON["media_listen"]
        elif addressed:
            force = 0.8
        elif has_image or has_voice:
            # Remaining media without clear allow → listen.
            allow = False
            force = 0.25
            reason_code = "media_listen"
            reason_zh = _REASON["media_listen"]

        # L2: only when enabled AND strong relevance.
        strong_l2 = bool(
            addressed
            or _strong_look_listen(text)
            or (image_label == "help_screenshot" and addressed)
            or (voice_label == "help_tone" and addressed)
        )
        # @/引用 bot 时必须把图/语音交给模型，否则“帮我看这张图”只能看到占位符。
        # L2 开关继续控制未点名的“看/听这个”环境主动理解。
        request_understand = bool(
            allow
            and strong_l2
            and not privacy_hit
            and (addressed or media_understand_reply_enabled)
        )
        # Without multimodal, never claim understand path; degrade quietly.
        if request_understand and mm_flag is False:
            request_understand = False
            multimodal_degraded = True
            labels.append("l2_degraded")

        if not allow:
            return MediaGateVerdict(
                False, force, reason_code, reason_zh,
                labels=tuple(labels), request_understand=False,
                skip_memory=skip_memory, privacy_hit=privacy_hit,
                has_image=has_image, has_voice=has_voice,
                multimodal_degraded=multimodal_degraded,
            )

        return MediaGateVerdict(
            True, force, reason_code, reason_zh,
            labels=tuple(labels), request_understand=request_understand,
            skip_memory=skip_memory, privacy_hit=privacy_hit,
            has_image=has_image, has_voice=has_voice,
            multimodal_degraded=multimodal_degraded,
        )
