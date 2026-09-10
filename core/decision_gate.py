"""Compose occasion skin + social manners + media air gate + useful proactive into a speak/silence gate."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .media_gate import MediaAirGate, MediaGateVerdict
from .occasion_skin import OccasionClassifier, OccasionSkin, apply_occasion_to_willingness, reason_to_zh
from .social_manners import MannersVerdict, SocialMannersGate
from .daily_rhythm import DailyRhythmGate, DailyRhythmVerdict, is_goodnight_text
from .useful_proactive import UsefulProactiveGate, UsefulProactiveVerdict

logger = logging.getLogger("astrbot_plugin_chat_dynamics.decision_gate")


@dataclass(frozen=True)
class GateResult:
    should_speak: bool
    reason_code: str
    reason_zh: str
    skin: OccasionSkin
    manners: MannersVerdict
    media: Optional[MediaGateVerdict] = None
    force_scale: float = 1.0
    request_understand: bool = False
    proactive: Optional[UsefulProactiveVerdict] = None
    rhythm: Optional[DailyRhythmVerdict] = None
    length_hint: str = "normal"
    delay_scale: float = 1.0


class DynamicsDecisionGate:
    def __init__(self) -> None:
        self.occasion = OccasionClassifier()
        self.manners = SocialMannersGate()
        self.media = MediaAirGate()
        self.useful = UsefulProactiveGate()
        self.rhythm = DailyRhythmGate()

    def evaluate(
        self,
        *,
        session_id: str,
        user_id: str,
        text: str,
        vibe_mode: Any = None,
        telemetrics: Any = None,
        recent_nodes: Sequence[Any] = (),
        bot_id: str = "",
        explicit: bool = False,
        willingness: float = 1.0,
        cfg: Any = None,
        now: Optional[float] = None,
        has_media: bool = False,
        media_component_types: Sequence[Any] = (),
        outline: str = "",
        quoted_bot: bool = False,
        private_field_hint: Optional[bool] = None,
        multimodal_available: Optional[bool] = None,
        group_memory: Any = None,
        public_memory_snippet: str = "",
        committed_reply: bool = False,
    ) -> GateResult:
        presence = str(getattr(cfg, "presence_knob", "sensible") or "sensible")
        scene_tags = tuple(getattr(telemetrics, "scene_tags", ()) or ())
        emotion_tags = tuple(getattr(telemetrics, "emotion_tags", ()) or ())
        deciding_on = bool(getattr(cfg, "deciding_detect_enabled", True))
        skin = self.occasion.classify(
            session_id=session_id,
            text=text,
            vibe_mode=vibe_mode,
            scene_tags=scene_tags,
            emotion_tags=emotion_tags,
            telemetrics=telemetrics,
            presence_knob=presence,
            now=now,
            deciding_detect_enabled=deciding_on,
        )
        manners = self.manners.evaluate(
            session_id=session_id,
            user_id=user_id,
            text=text,
            recent_nodes=recent_nodes,
            bot_id=bot_id,
            explicit=explicit,
            occasion_kind=skin.kind.value,
            presence_knob=presence,
            social_manners_enabled=bool(getattr(cfg, "social_manners_enabled", True)),
            relay_baton_enabled=bool(getattr(cfg, "relay_baton_enabled", True)),
            private_field_enabled=bool(getattr(cfg, "private_field_enabled", True)),
            hyped_quota_enabled=bool(getattr(cfg, "hyped_quota_enabled", True)),
            now=now,
        )
        if not manners.allow:
            return GateResult(
                False, manners.reason_code, manners.reason_zh, skin, manners,
                media=None, force_scale=0.0, request_understand=False,
                length_hint=skin.length_hint,
            )

        if skin.cool_command and not explicit:
            self.manners.note_quiet(
                session_id, reason_code="cool_command", reason_zh="收到降温指令，先低调", now=now
            )
            return GateResult(
                False, "cool_command", "收到降温指令，先低调", skin, manners,
                length_hint=skin.length_hint,
            )

        if skin.kind.value == "conflict" and not explicit:
            self.manners.note_quiet(
                session_id, reason_code="conflict_silence", reason_zh=skin.reason_zh, now=now
            )
            return GateResult(
                False, "conflict_silence", skin.reason_zh, skin, manners,
                length_hint=skin.length_hint,
            )

        # Infer private-field hint for media when manners did not already deny (explicit path).
        pf_hint = bool(private_field_hint) if private_field_hint is not None else False
        if private_field_hint is None and bool(getattr(cfg, "private_field_enabled", True)):
            try:
                pf_hint = self.manners._private_field_active(
                    recent_nodes,
                    bot_id=bot_id,
                    user_id=user_id,
                    text=text,
                    occasion_kind=skin.kind.value,
                )
            except Exception:
                pf_hint = False

        media_verdict: Optional[MediaGateVerdict] = None
        try:
            media_verdict = self.media.evaluate(
                text=text,
                has_media=bool(has_media),
                media_component_types=media_component_types,
                outline=outline,
                explicit=explicit,
                quoted_bot=bool(quoted_bot),
                private_field_hint=pf_hint,
                media_image_gate_enabled=bool(getattr(cfg, "media_image_gate_enabled", True)),
                media_voice_gate_enabled=bool(getattr(cfg, "media_voice_gate_enabled", True)),
                media_understand_reply_enabled=bool(
                    getattr(cfg, "media_understand_reply_enabled", False)
                ),
                media_privacy_strict=bool(getattr(cfg, "media_privacy_strict", True)),
                multimodal_available=multimodal_available,
            )
        except Exception:
            media_verdict = None

        if media_verdict is not None and (has_media or media_verdict.has_image or media_verdict.has_voice):
            if not media_verdict.allow_speak:
                # 晚安图/表情包仍须进入作息；证件等隐私媒体继续静默。
                privacy_block = bool(getattr(media_verdict, "privacy_hit", False))
                if privacy_block or not is_goodnight_text(text):
                    self.manners.note_quiet(
                        session_id,
                        reason_code=media_verdict.reason_code,
                        reason_zh=media_verdict.reason_zh,
                        now=now,
                    )
                    return GateResult(
                        False,
                        media_verdict.reason_code,
                        media_verdict.reason_zh,
                        skin,
                        manners,
                        media=media_verdict,
                        force_scale=float(media_verdict.force_scale),
                        request_understand=False,
                        length_hint=skin.length_hint,
                    )

        scaled = apply_occasion_to_willingness(willingness, skin, explicit=explicit)
        media_scale = float(getattr(media_verdict, "force_scale", 1.0) or 1.0) if media_verdict else 1.0
        combined_scale = max(0.0, min(1.5, float(skin.force_scale) * media_scale))
        scaled *= media_scale

        # Ghost already handled in manners; sensible/lively use scaled WTS for ambient only.
        if not explicit and scaled < 0.35 and presence == "ghost":
            self.manners.note_quiet(
                session_id, reason_code="presence_ghost", reason_zh="分寸旋钮在隐身档，少开口", now=now
            )
            return GateResult(
                False, "presence_ghost", "分寸旋钮在隐身档，少开口", skin, manners,
                media=media_verdict, force_scale=combined_scale,
                length_hint=skin.length_hint,
            )

        if not explicit and skin.silence_bias >= 0.85:
            self.manners.note_quiet(
                session_id, reason_code="occasion_silence", reason_zh=skin.reason_zh, now=now
            )
            return GateResult(
                False, "occasion_silence", skin.reason_zh, skin, manners,
                media=media_verdict, force_scale=combined_scale,
                length_hint=skin.length_hint,
            )

        # Soft media force: ambient with very low media force → listen.
        if (
            media_verdict is not None
            and (has_media or media_verdict.has_image or media_verdict.has_voice)
            and not explicit
            and media_scale < 0.35
            and not is_goodnight_text(text)
        ):
            self.manners.note_quiet(
                session_id,
                reason_code=media_verdict.reason_code or "media_listen",
                reason_zh=media_verdict.reason_zh or "媒体·不确定旁听",
                now=now,
            )
            return GateResult(
                False,
                media_verdict.reason_code or "media_listen",
                media_verdict.reason_zh or "媒体·不确定旁听",
                skin,
                manners,
                media=media_verdict,
                force_scale=combined_scale,
                length_hint=skin.length_hint,
            )

        # daily_rhythm — after manners/media/occasion/deciding skin; before useful_proactive/quota
        rhythm: Optional[DailyRhythmVerdict] = None
        memory_on = True if cfg is None else bool(getattr(cfg, "group_memory_enabled", True))
        gm = group_memory if memory_on else None
        snippet = str(public_memory_snippet or "").strip() if memory_on else ""
        if not snippet and gm is not None:
            try:
                stamp = float(now) if now is not None else time.time()
                snippet = str(self.useful._public_memory_line(gm, session_id, stamp=stamp) or "")
            except Exception:
                snippet = ""
        try:
            rhythm = self.rhythm.evaluate(
                session_id=session_id,
                user_id=user_id,
                text=text,
                explicit=explicit,
                occasion_kind=skin.kind.value,
                presence_knob=presence,
                recent_nodes=recent_nodes,
                bot_id=bot_id,
                telemetrics=telemetrics,
                cfg=cfg,
                now=now,
                private_field=pf_hint,
                quoted_bot=bool(quoted_bot),
                has_media=bool(has_media),
                media_toward_bot=bool(
                    explicit
                    or quoted_bot
                    or (media_verdict is not None and getattr(media_verdict, "request_understand", False))
                ),
                command_prefix=str(getattr(cfg, "command_prefix", "/") or "/") if cfg is not None else "/",
                public_memory_snippet=snippet,
            )
        except Exception:
            rhythm = DailyRhythmVerdict(True, "ok", "作息检查通过")

        # Honor rhythm.allow even for explicit @ — evaluate() already encodes
        # wake-whitelist exceptions. ``rhythm_allow_wake=False`` must not be
        # bypassed just because the turn was addressed.
        if rhythm is not None and not rhythm.allow:
            self.manners.note_quiet(
                session_id,
                reason_code=rhythm.reason_code,
                reason_zh=rhythm.reason_zh,
                now=now,
            )
            return GateResult(
                False,
                rhythm.reason_code,
                rhythm.reason_zh,
                skin,
                manners,
                media=media_verdict,
                force_scale=max(0.0, min(1.5, combined_scale * float(rhythm.force_scale or 1.0))),
                length_hint=rhythm.length_hint or skin.length_hint,
                rhythm=rhythm,
            )

        # useful_proactive / newcomer / quota — after manners/media/occasion (+ rhythm)
        media_privacy = bool(getattr(media_verdict, "privacy_hit", False)) if media_verdict else False
        try:
            # Rhythm-authorized short acts (晚安首波/短醒/早安) must not die on no_gap.
            # A persona/model turn that already chose to reply is a request, not
            # useful-proactive ambient — newcomer_caution / no_gap must not veto it.
            useful_explicit = bool(explicit) or bool(committed_reply) or bool(
                rhythm is not None
                and rhythm.allow
                and rhythm.action in {
                    "goodnight_reply",
                    "wake_reply",
                    "morning_hi",
                    "day_share",
                    "insomnia_line",
                }
            )
            proactive = self.useful.evaluate(
                session_id=session_id,
                user_id=user_id,
                text=text,
                explicit=useful_explicit,
                occasion_kind=skin.kind.value,
                presence_knob=presence,
                recent_nodes=recent_nodes,
                bot_id=bot_id,
                telemetrics=telemetrics,
                group_memory=gm,
                public_memory_snippet=snippet,
                cfg=cfg,
                now=now,
                private_field=pf_hint,
                media_privacy=media_privacy,
            )
        except Exception:
            proactive = UsefulProactiveVerdict(True, "ok", "主动检查通过")

        # Rhythm may block ambient proactive (asleep / wind-down) even if useful allowed.
        if (
            rhythm is not None
            and not explicit
            and rhythm.proactive_blocked
            and proactive.proactive
        ):
            proactive = UsefulProactiveVerdict(
                False,
                rhythm.reason_code or "asleep_ambient",
                rhythm.reason_zh or "已睡·环境主动关闭",
                force_scale=0.0,
                length_hint="brief",
                delay_scale=float(proactive.delay_scale or 1.0),
                quota_used=proactive.quota_used,
                quota_cap=proactive.quota_cap,
            )

        if rhythm is not None:
            combined_scale = max(0.0, min(1.5, combined_scale * float(rhythm.force_scale or 1.0)))

        length_hint = proactive.length_hint or skin.length_hint
        if rhythm is not None and rhythm.length_hint == "brief":
            length_hint = "brief"
        delay_scale = float(proactive.delay_scale or 1.0)
        combined_scale = max(0.0, min(1.5, combined_scale * float(proactive.force_scale or 1.0)))

        if not explicit and not proactive.allow:
            self.manners.note_quiet(
                session_id,
                reason_code=proactive.reason_code,
                reason_zh=proactive.reason_zh,
                now=now,
            )
            return GateResult(
                False,
                proactive.reason_code,
                proactive.reason_zh,
                skin,
                manners,
                media=media_verdict,
                force_scale=combined_scale,
                proactive=proactive,
                rhythm=rhythm,
                length_hint=length_hint,
                delay_scale=delay_scale,
            )

        # Deciding ambient without clarification gap already denied above; if deciding
        # and still ambient with high silence, prefer deciding reason.
        rhythm_act = bool(rhythm is not None and rhythm.allow and rhythm.action in {
            "goodnight_reply", "wake_reply"
        })
        # Morning hi must not fire while deciding (rhythm itself skips); goodnight/wake may.
        if (
            not explicit
            and not rhythm_act
            and skin.kind.value == "deciding"
            and skin.silence_bias >= 0.7
            and not (
                proactive.proactive and proactive.gap_kind in {"hanging_question", "appointment_gap"}
            )
        ):
            # If useful allowed lively non-gap, still hush banter-style ambient in deciding.
            if not proactive.proactive:
                self.manners.note_quiet(
                    session_id,
                    reason_code="deciding_no_banter",
                    reason_zh="决策中不插科",
                    now=now,
                )
                return GateResult(
                    False,
                    "deciding_no_banter",
                    "决策中不插科",
                    skin,
                    manners,
                    media=media_verdict,
                    force_scale=combined_scale,
                    proactive=proactive,
                    rhythm=rhythm,
                    length_hint="brief",
                    delay_scale=delay_scale,
                )

        request_u = bool(getattr(media_verdict, "request_understand", False)) if media_verdict else False
        # Prefer rhythm action reason when morning/goodnight/wake suggested.
        speak_code = "ok" if not proactive.proactive else proactive.reason_code
        speak_zh = "可以开口" if not proactive.proactive else proactive.reason_zh
        if rhythm is not None and rhythm.action and rhythm.allow:
            speak_code = rhythm.reason_code or speak_code
            speak_zh = rhythm.reason_zh or speak_zh
        return GateResult(
            True,
            speak_code,
            speak_zh,
            skin,
            manners,
            media=media_verdict,
            force_scale=combined_scale,
            request_understand=request_u,
            proactive=proactive,
            rhythm=rhythm,
            length_hint=length_hint,
            delay_scale=delay_scale,
        )

    def reset_session(self, session_id: str) -> None:
        sid = str(session_id or "")
        if not sid:
            return
        self.rhythm.reset_session(sid)
        self.useful.reset_session(sid)
        self.manners.reset_session(sid)
        try:
            self.occasion.reset_session(sid)
        except Exception as exc:
            logger.warning("occasion.reset_session failed: %s", type(exc).__name__)

    def note_spoke(
        self,
        session_id: str,
        *,
        skin: OccasionSkin,
        now: Optional[float] = None,
        proactive: Optional[UsefulProactiveVerdict] = None,
        rhythm: Optional[DailyRhythmVerdict] = None,
    ) -> None:
        reason_code = "spoke"
        reason_zh = "接了一句"
        if proactive is not None and getattr(proactive, "proactive", False):
            reason_code = str(proactive.reason_code or "gap_fill_ok")
            reason_zh = str(proactive.reason_zh or "缺口补全，轻接了一句")
        elif rhythm is not None and str(getattr(rhythm, "action", "") or ""):
            reason_code = str(rhythm.reason_code or rhythm.action or "spoke")
            reason_zh = str(rhythm.reason_zh or "作息里开口")
        elif str(getattr(skin.kind, "value", "") or "") not in {"", "neutral"}:
            reason_code = f"spoke_{skin.kind.value}"
            reason_zh = str(skin.reason_zh or "这个场合接了一句")
        self.manners.note_intervene(
            session_id,
            occasion_kind=skin.kind.value,
            hyped=skin.kind.value == "banter",
            now=now,
            reason_code=reason_code,
            reason_zh=reason_zh,
        )
        if proactive is not None and proactive.proactive:
            self.useful.note_proactive(
                session_id,
                gap_fingerprint=proactive.gap_fingerprint,
                now=now,
            )
        if rhythm is not None:
            try:
                self.rhythm.note_spoke(session_id, verdict=rhythm, now=now)
            except Exception as exc:
                logger.warning("rhythm.note_spoke failed: %s", type(exc).__name__)

    def note_arbiter_silence(self, session_id: str, reason: str, *, now: Optional[float] = None) -> None:
        zh = reason_to_zh(reason)
        code = "arbiter_silence"
        lowered = (reason or "").lower()
        for key in (
            "deep_cooling",
            "safe_hover",
            "energy_asymmetry",
            "private_topic",
            "filter_gate",
            "wts_low",
            "media_others_field",
            "voice_low_info",
            "image_privacy_skip",
            "media_listen",
            "media_meme_listen",
            "deciding_no_banter",
            "proactive_quota",
            "newcomer_caution",
            "gap_wait",
            "wind_later_silence",
        ):
            if key.replace("_", " ") in lowered or key in lowered.replace("-", "_"):
                code = key
                break
        if "wts" in lowered:
            code = "wts_low"
        # Prefer Chinese media reasons already embedded.
        if any(
            token in (reason or "")
            for token in ("媒体·", "语音·", "图片·", "决策中", "主动配额", "新人·", "等待缺口", "收束中", "已睡", "短醒", "热聊中", "多数人已歇")
        ):
            zh = reason.split(":", 1)[-1].strip() if ":" in reason else reason
        self.manners.note_quiet(session_id, reason_code=code, reason_zh=zh, now=now)
