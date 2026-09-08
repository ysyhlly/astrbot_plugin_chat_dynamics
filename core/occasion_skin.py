"""Occasion skin: read-the-room classifier that modulates silence and force."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Sequence

from .vibe_analyzer import GroupChatMode


class OccasionKind(str, Enum):
    SERIOUS_HELP = "serious_help"
    BANTER = "banter"
    VENT = "vent"
    CONFLICT = "conflict"
    DECIDING = "deciding"
    NEUTRAL = "neutral"


_COOL_COMMANDS = (
    "今天别闹",
    "今天别起哄",
    "别闹了",
    "安静点",
    "消停点",
    "别整活",
    "闭嘴",
    "少说两句",
    "今天低调",
)

_CONFLICT_MARKERS = (
    "吵什么",
    "别吵",
    "滚",
    "有病",
    "打架",
    "撕逼",
    "对线",
    "吵架",
    "别骂",
    "你礼貌吗",
    "nmsl",
    "去死",
)

_VENT_MARKERS = (
    "难受",
    "烦死",
    "累死",
    "崩溃",
    "委屈",
    "想哭",
    "郁闷",
    "压力好大",
)

_HELP_MARKERS = (
    "怎么弄",
    "怎么办",
    "帮我看",
    "求助",
    "报错",
    "怎么实现",
    "为什么失败",
    "请教",
)

_BANTER_MARKERS = (
    "哈哈",
    "笑死",
    "草",
    "绷不住",
    "233",
    "梗",
    "绝了",
    "hhh",
    "lol",
)

# Conservative deciding markers: schedule / vote / pick /分工. Uncertain ≠ deciding.
_DECIDING_MARKERS = (
    "约几点",
    "几点见",
    "几点集合",
    "定个时间",
    "定一下时间",
    "投票",
    "投一下",
    "举手表决",
    "选哪个",
    "选一个",
    "选型",
    "谁负责",
    "谁来做",
    "怎么分工",
    "分工一下",
    "拍板",
    "敲定",
    "定下来",
    "还是改",
    "还是定",
    "A还是B",
    "哪个方案",
    "选方案",
)

_DECIDING_SETTLE = (
    "就这样",
    "就定了",
    "就定这个",
    "就选这个",
    "敲定了",
    "定了",
    "好就这个",
    "就按这个",
    "不再改",
    "成交",
)

_DECIDING_TOPIC_JUMP = (
    "换个话题",
    "不说这个了",
    "说点别的",
    "扯远了",
)


@dataclass(frozen=True)
class OccasionSkin:
    kind: OccasionKind
    silence_bias: float  # 0..1 higher => prefer silence
    length_hint: str  # brief|normal|detailed
    force_scale: float  # multiplies WTS / ambient willingness
    reason_zh: str
    cool_command: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "silence_bias": self.silence_bias,
            "length_hint": self.length_hint,
            "force_scale": self.force_scale,
            "reason_zh": self.reason_zh,
            "cool_command": self.cool_command,
        }


# Presence knob scales how aggressively we intervene.
_PRESENCE_FORCE = {
    "ghost": 0.45,
    "sensible": 1.0,
    "lively": 1.25,
}
_PRESENCE_SILENCE = {
    "ghost": 0.25,
    "sensible": 0.0,
    "lively": -0.12,
}


class OccasionClassifier:
    """Local heuristics over vibe/telemetrics + text markers."""

    def __init__(self) -> None:
        # session_id -> cool_until epoch
        self._cool_until: Dict[str, float] = {}
        # session_id -> deciding_until epoch (soft sticky)
        self._deciding_until: Dict[str, float] = {}

    def note_cool_command(self, session_id: str, *, duration: float = 6 * 3600, now: Optional[float] = None) -> None:
        stamp = time.time() if now is None else float(now)
        self._cool_until[str(session_id)] = stamp + float(duration)

    def cool_remaining(self, session_id: str, *, now: Optional[float] = None) -> float:
        stamp = time.time() if now is None else float(now)
        return max(0.0, self._cool_until.get(str(session_id), 0.0) - stamp)

    def note_deciding(self, session_id: str, *, duration: float = 180.0, now: Optional[float] = None) -> None:
        stamp = time.time() if now is None else float(now)
        self._deciding_until[str(session_id)] = stamp + float(duration)

    def clear_deciding(self, session_id: str) -> None:
        self._deciding_until.pop(str(session_id), None)

    def deciding_remaining(self, session_id: str, *, now: Optional[float] = None) -> float:
        stamp = time.time() if now is None else float(now)
        return max(0.0, self._deciding_until.get(str(session_id), 0.0) - stamp)

    def reset_session(self, session_id: str) -> None:
        sid = str(session_id or "")
        self._cool_until.pop(sid, None)
        self._deciding_until.pop(sid, None)

    @staticmethod
    def is_cool_command(text: str) -> bool:
        clean = (text or "").strip()
        if not clean:
            return False
        return any(marker in clean for marker in _COOL_COMMANDS)

    @staticmethod
    def is_deciding_marker(text: str) -> bool:
        clean = (text or "").strip()
        if not clean:
            return False
        # Require clear markers; short ambiguous chat is NOT deciding.
        if any(m in clean for m in _DECIDING_MARKERS):
            return True
        # Short Q/A vote shape: "A还是B？" / "选1还是2"
        if ("还是" in clean or "or" in clean.lower()) and ("?" in clean or "？" in clean):
            if len(clean) <= 40:
                return True
        return False

    @staticmethod
    def is_deciding_settle(text: str) -> bool:
        clean = (text or "").strip()
        return bool(clean) and any(m in clean for m in _DECIDING_SETTLE)

    @staticmethod
    def is_topic_jump(text: str) -> bool:
        clean = (text or "").strip()
        return bool(clean) and any(m in clean for m in _DECIDING_TOPIC_JUMP)

    def classify(
        self,
        *,
        session_id: str = "",
        text: str = "",
        vibe_mode: Any = None,
        scene_tags: Sequence[str] = (),
        emotion_tags: Sequence[str] = (),
        telemetrics: Any = None,
        presence_knob: str = "sensible",
        now: Optional[float] = None,
        deciding_detect_enabled: bool = True,
    ) -> OccasionSkin:
        clean = (text or "").strip()
        tags = {str(t).lower() for t in (scene_tags or ())}
        emotions = {str(t).lower() for t in (emotion_tags or ())}
        mode_value = getattr(vibe_mode, "value", vibe_mode)
        mode_str = str(mode_value or "")

        cool_hit = self.is_cool_command(clean)
        if cool_hit and session_id:
            self.note_cool_command(session_id, now=now)

        cool_active = self.cool_remaining(session_id, now=now) > 0.0 if session_id else False

        kind = OccasionKind.NEUTRAL
        silence_bias = 0.15
        length_hint = "normal"
        force_scale = 1.0
        reason_zh = "普通闲聊，按默认分寸"

        conflict = (
            any(m in clean for m in _CONFLICT_MARKERS)
            or "tense" in tags
            or "tense" in emotions
            or "conflict" in tags
        )
        vent = any(m in clean for m in _VENT_MARKERS) or "support" in tags or "negative" in emotions
        help_like = (
            any(m in clean for m in _HELP_MARKERS)
            or "technical_help" in tags
            or mode_str == GroupChatMode.SERIOUS_INQUIRY.value
        )
        banter = (
            any(m in clean for m in _BANTER_MARKERS)
            or "banter" in tags
            or mode_str == GroupChatMode.FAST_BANTER.value
        )

        deciding_hit = False
        if bool(deciding_detect_enabled):
            if session_id and (self.is_deciding_settle(clean) or self.is_topic_jump(clean)):
                self.clear_deciding(session_id)
            elif self.is_deciding_marker(clean):
                deciding_hit = True
                if session_id:
                    self.note_deciding(session_id, now=now)
            elif session_id and self.deciding_remaining(session_id, now=now) > 0.0:
                # Sticky deciding until settle / topic jump / timeout; chill fade clears.
                if mode_str == GroupChatMode.CHILL_FADE.value:
                    self.clear_deciding(session_id)
                else:
                    deciding_hit = True

        # Priority: conflict/cool > deciding > serious_help > vent > banter > neutral
        if conflict or cool_active:
            kind = OccasionKind.CONFLICT if conflict else OccasionKind.NEUTRAL
            silence_bias = 0.95 if conflict else 0.85
            length_hint = "brief"
            force_scale = 0.15
            reason_zh = "冲突/降温场合，宁可安静" if conflict else "收到降温指令，压低存在感"
        elif deciding_hit:
            kind = OccasionKind.DECIDING
            silence_bias = 0.72
            length_hint = "brief"
            force_scale = 0.45
            reason_zh = "决策中，不插科只可短澄清"
        elif help_like:
            kind = OccasionKind.SERIOUS_HELP
            silence_bias = 0.1
            length_hint = "brief"
            force_scale = 0.9
            reason_zh = "认真求助，短而稳"
        elif vent:
            kind = OccasionKind.VENT
            silence_bias = 0.55
            length_hint = "brief"
            force_scale = 0.55
            reason_zh = "倾诉场合，少话多接"
        elif banter:
            kind = OccasionKind.BANTER
            silence_bias = 0.2
            length_hint = "brief"
            force_scale = 1.1
            reason_zh = "整活场合，可轻起哄"
        elif mode_str == GroupChatMode.CHILL_FADE.value:
            kind = OccasionKind.NEUTRAL
            silence_bias = 0.7
            length_hint = "brief"
            force_scale = 0.5
            reason_zh = "冷场衰退，少打扰"

        knob = str(presence_knob or "sensible").lower()
        force_scale *= _PRESENCE_FORCE.get(knob, 1.0)
        silence_bias = min(1.0, max(0.0, silence_bias + _PRESENCE_SILENCE.get(knob, 0.0)))
        if cool_hit:
            reason_zh = "收到「今天别闹」类指令，立刻压低存在感"

        # Slight speaker-density nudge: crowded rooms bias quieter unless banter.
        speakers = int(getattr(telemetrics, "unique_speakers", 0) or 0)
        if speakers >= 4 and kind != OccasionKind.BANTER:
            silence_bias = min(1.0, silence_bias + 0.08)

        return OccasionSkin(
            kind=kind,
            silence_bias=round(silence_bias, 3),
            length_hint=length_hint,
            force_scale=round(force_scale, 3),
            reason_zh=reason_zh,
            cool_command=cool_hit or cool_active,
        )


def apply_occasion_to_willingness(
    willingness: float,
    skin: OccasionSkin,
    *,
    explicit: bool = False,
) -> float:
    """Scale WTS by occasion; explicit @ bypasses silence bias."""
    if explicit:
        return willingness
    scaled = willingness * skin.force_scale
    scaled -= skin.silence_bias * 0.5
    return max(0.0, min(1.0, round(scaled, 3)))


REASON_ZH = {
    "conflict_silence": "冲突场合，先安静不插话",
    "cool_command": "群友说了今天别闹，先低调",
    "relay_baton": "两人正在互回，先靠边不插中间",
    "private_field": "像是两人私场，不硬插",
    "hyped_quota": "这段整活已经起哄过一次，先旁听",
    "presence_ghost": "分寸旋钮在隐身档，少开口",
    "filter_gate": "未点名且未开环境插话",
    "deep_cooling": "深度冷却中",
    "safe_hover": "安全悬停，先记着不抢答",
    "energy_asymmetry": "对方敷衍回复，先离场",
    "private_topic": "触及私密边界，未点名不说",
    "wts_low": "发言欲望不够，先旁听",
    "media_others_field": "媒体·别人的场",
    "voice_low_info": "语音·信息量低",
    "image_privacy_skip": "图片·隐私跳过",
    "media_listen": "媒体·不确定旁听",
    "media_meme_listen": "媒体·整活旁听",
    "deciding_no_banter": "决策中不插科",
    "proactive_quota": "主动配额用尽",
    "newcomer_caution": "新人·更收",
    "gap_wait": "等待缺口闭合",
    "gap_fill_ok": "缺口补全主动",
}


def reason_to_zh(reason: str, *, fallback: str = "") -> str:
    text = (reason or "").strip()
    if not text:
        return fallback or "先安静旁听"
    lowered = text.lower()
    for code, zh in REASON_ZH.items():
        if code in lowered or code.replace("_", " ") in lowered:
            return zh
    if "deep cooling" in lowered or "cooling" in lowered:
        return REASON_ZH["deep_cooling"]
    if "safe hover" in lowered:
        return REASON_ZH["safe_hover"]
    if "energy asymmetry" in lowered:
        return REASON_ZH["energy_asymmetry"]
    if "private-topic" in lowered or "private topic" in lowered:
        return REASON_ZH["private_topic"]
    if "filter gate" in lowered or "ambient" in lowered:
        return REASON_ZH["filter_gate"]
    if "wts" in lowered:
        return REASON_ZH["wts_low"]
    if "conflict" in lowered:
        return REASON_ZH["conflict_silence"]
    if "deciding" in lowered:
        return REASON_ZH["deciding_no_banter"]
    if "proactive_quota" in lowered or "quota" in lowered:
        return REASON_ZH["proactive_quota"]
    if "newcomer" in lowered:
        return REASON_ZH["newcomer_caution"]
    if "gap_wait" in lowered or "gap closed" in lowered:
        return REASON_ZH["gap_wait"]
    return fallback or text[:80]
