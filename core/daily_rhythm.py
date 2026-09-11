"""Daily rhythm (今日作息): atmosphere prop — wind-down ≠ asleep.

Wired into DynamicsDecisionGate after manners/media/occasion (incl. deciding),
before useful_proactive / quotas. Never replaces manners/media/deciding.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("astrbot_plugin_chat_dynamics.daily_rhythm")

# --- states -------------------------------------------------------------------

STATE_AWAKE = "awake"
STATE_WINDING_DOWN = "winding_down"
STATE_ASLEEP_AFTER_WIND = "asleep_after_wind"
STATE_ASLEEP_SELF = "asleep_self"
STATE_BRIEF_WAKE = "brief_wake"
STATE_INSOMNIAC = "insomniac"

ASLEEP_STATES = frozenset({STATE_ASLEEP_AFTER_WIND, STATE_ASLEEP_SELF})
SLEEPY_STATES = frozenset({STATE_WINDING_DOWN, *ASLEEP_STATES, STATE_BRIEF_WAKE, STATE_INSOMNIAC})

STATE_LABEL_ZH = {
    STATE_AWAKE: "还醒着",
    STATE_WINDING_DOWN: "收束中",
    STATE_ASLEEP_AFTER_WIND: "已睡·收束后",
    STATE_ASLEEP_SELF: "已睡·自己睡",
    STATE_BRIEF_WAKE: "短醒·被吵醒",
    STATE_INSOMNIAC: "失眠中",
}

REASON = {
    "ok": "作息检查通过",
    "disabled": "今日作息关闭",
    "force_sleep": "强制入睡·环境主动关闭",
    "wind_goodnight_ok": "收束中·晚安首波可回",
    "wind_goodnight_done": "收束中·晚安已回过",
    "wind_later_silence": "收束中·后续静默",
    "wind_hot_delay": "热聊中·推迟睡点",
    "asleep_ambient": "已睡·环境主动关闭",
    "asleep_plain_gn": "已睡·普通晚安不吵醒",
    "asleep_wake_ok": "短醒·白名单可短回",
    "brief_wake_cooldown": "短醒冷却中",
    "morning_hi_ok": "醒来窗·可选早安",
    "morning_hi_skip": "决策中/冲突/私场/隐身不早安",
    "morning_hi_tonight_off": "今晚别早安",
    "day_share_ok": "日间分享槽可用",
    "day_share_done": "日间分享槽已用完",
    "insomnia_ok": "失眠中·至多一句气氛",
    "insomnia_off": "睡不着默认关",
    "insomnia_cap": "失眠周期硬顶",
    "majority_asleep": "多数人已歇·入睡",
}

_GOODNIGHT_RE = re.compile(
    r"(晚安|晚安啦|晚安哦|睡了|去睡|睡啦|睡觉了|好梦|拜拜.*睡|睡前|"
    r"good\s*night|gn\b|nighty)",
    re.IGNORECASE,
)
_MORNING_RE = re.compile(
    r"(早啊|早上好|早安|早呀|起来了|起床了|good\s*morning)",
    re.IGNORECASE,
)
_HELP_MARKERS = ("怎么办", "怎么弄", "帮我", "求助", "报错", "请教", "救命", "急")
_COMMAND_RE = re.compile(r"^[/!！．.]?\w+")


@dataclass(frozen=True)
class DailyRhythmVerdict:
    allow: bool
    reason_code: str
    reason_zh: str
    state: str = STATE_AWAKE
    action: str = ""  # goodnight_reply|wake_reply|morning_hi|day_share|insomnia_line|""
    force_scale: float = 1.0
    length_hint: str = "normal"  # brief|normal
    consume_goodnight_quota: bool = False
    consume_day_share: bool = False
    proactive_blocked: bool = False  # ambient useful_proactive must stay 0
    morning_hi: bool = False
    day_share: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allow": self.allow,
            "reason_code": self.reason_code,
            "reason_zh": self.reason_zh,
            "state": self.state,
            "state_zh": STATE_LABEL_ZH.get(self.state, self.state),
            "action": self.action,
            "force_scale": self.force_scale,
            "length_hint": self.length_hint,
            "consume_goodnight_quota": self.consume_goodnight_quota,
            "consume_day_share": self.consume_day_share,
            "proactive_blocked": self.proactive_blocked,
            "morning_hi": self.morning_hi,
            "day_share": self.day_share,
        }


_PASS = DailyRhythmVerdict(True, "ok", REASON["ok"])


@dataclass
class _SessionRhythm:
    sid: str = ""
    state: str = STATE_AWAKE
    day_key: str = ""
    timezone: str = ""
    force_sleep: bool = False
    last_wake_at: float = 0.0
    wind_started_at: float = 0.0
    wind_sleep_deadline: float = 0.0  # conservative 20–40min target
    goodnight_text_used: int = 0
    goodnight_replied: bool = False
    asleep_since: float = 0.0
    asleep_kind: str = ""  # after_wind|self
    brief_wake_until: float = 0.0
    wake_replies_today: int = 0
    morning_hi_done: bool = False
    day_share_used: int = 0
    insomnia_lines_today: int = 0
    insomnia_last_at: float = 0.0
    last_reason_code: str = ""
    last_reason_zh: str = ""
    hot_delay_until: float = 0.0
    pre_sleep_bot_msg_hint: str = ""


def _node_text(node: Any) -> str:
    return str(getattr(node, "text", "") or getattr(node, "content", "") or "")


def _node_user(node: Any) -> str:
    return str(getattr(node, "user_id", "") or "")


def _node_ts(node: Any) -> float:
    for key in ("timestamp", "ts", "created_at", "time"):
        val = getattr(node, key, None)
        if val is None and isinstance(node, dict):
            val = node.get(key)
        try:
            if val is not None:
                return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def _day_key(stamp: float, timezone: str = "") -> str:
    if timezone:
        return datetime.fromtimestamp(stamp, ZoneInfo(timezone)).strftime("%Y%m%d")
    return time.strftime("%Y%m%d", time.localtime(stamp))


def _local_hour(stamp: float, timezone: str = "") -> int:
    if timezone:
        return datetime.fromtimestamp(stamp, ZoneInfo(timezone)).hour
    return int(time.localtime(stamp).tm_hour)


def is_goodnight_text(text: str) -> bool:
    clean = (text or "").strip()
    if not clean:
        return False
    return bool(_GOODNIGHT_RE.search(clean))


def is_morning_text(text: str) -> bool:
    clean = (text or "").strip()
    if not clean:
        return False
    return bool(_MORNING_RE.search(clean))


def _mentions_bot(text: str, recent_nodes: Sequence[Any], *, bot_id: str, user_id: str) -> bool:
    if not bot_id:
        return False
    bid = str(bot_id)
    for node in recent_nodes[-8:]:
        mentions = getattr(node, "mentioned_users", None) or getattr(node, "mentions", None) or []
        if isinstance(node, dict):
            mentions = node.get("mentioned_users") or node.get("mentions") or mentions
        try:
            if bid in {str(m) for m in mentions} and _node_user(node) == str(user_id):
                return True
        except Exception:
            pass
    # crude @bot in text
    if f"@{bid}" in (text or ""):
        return True
    return False


def _quoted_bot(recent_nodes: Sequence[Any], *, bot_id: str, quoted_bot: bool) -> bool:
    if quoted_bot:
        return True
    if not bot_id or not recent_nodes:
        return False
    tip = recent_nodes[-1]
    reply_to = getattr(tip, "reply_to_id", "") or (tip.get("reply_to_id") if isinstance(tip, dict) else "")
    if not reply_to:
        return False
    for node in recent_nodes:
        mid = getattr(node, "msg_id", "") or getattr(node, "id", "") or ""
        if str(mid) == str(reply_to) and _node_user(node) == str(bot_id):
            return True
    return False


def _looks_command(text: str, *, command_prefix: str = "/") -> bool:
    clean = (text or "").strip()
    if not clean:
        return False
    prefix = command_prefix or "/"
    if clean.startswith(prefix):
        return True
    return bool(_COMMAND_RE.match(clean) and clean.startswith(("!", "！", ".", "．")))


def _help_toward_bot(text: str, *, explicit: bool) -> bool:
    clean = (text or "").strip()
    if not any(m in clean for m in _HELP_MARKERS):
        return False
    return explicit or "你" in clean or "@" in clean


def _chat_heat(telemetrics: Any, recent_nodes: Sequence[Any], *, stamp: float) -> str:
    """Return cold|normal|hot for sleep heuristics."""
    mpm = None
    if telemetrics is not None:
        for key in ("mpm", "messages_per_minute", "rate_mpm"):
            val = getattr(telemetrics, key, None)
            if val is None and isinstance(telemetrics, dict):
                val = telemetrics.get(key)
            try:
                if val is not None:
                    mpm = float(val)
                    break
            except (TypeError, ValueError):
                pass
    if mpm is None and recent_nodes:
        window = 180.0
        count = 0
        for node in recent_nodes:
            ts = _node_ts(node)
            if ts and stamp - ts <= window:
                count += 1
        mpm = count / (window / 60.0)
    if mpm is None:
        return "normal"
    if mpm >= 8.0:
        return "hot"
    if mpm <= 2.0:
        return "cold"
    return "normal"


def _unique_speakers(recent_nodes: Sequence[Any], *, since: float, bot_id: str = "") -> int:
    users = set()
    for node in recent_nodes:
        ts = _node_ts(node)
        if ts and ts < since:
            continue
        uid = _node_user(node)
        if not uid or (bot_id and uid == str(bot_id)):
            continue
        users.add(uid)
    return len(users)


class DailyRhythmGate:
    """Per-UMO session atmosphere rhythm state machine."""

    def __init__(self) -> None:
        self._sessions: Dict[str, _SessionRhythm] = {}
        self._why: Dict[str, List[Dict[str, Any]]] = {}

    # --- public ---------------------------------------------------------------

    def evaluate(
        self,
        *,
        session_id: str,
        user_id: str = "",
        text: str = "",
        explicit: bool = False,
        occasion_kind: str = "neutral",
        presence_knob: str = "sensible",
        recent_nodes: Sequence[Any] = (),
        bot_id: str = "",
        telemetrics: Any = None,
        cfg: Any = None,
        now: Optional[float] = None,
        private_field: bool = False,
        quoted_bot: bool = False,
        has_media: bool = False,
        media_toward_bot: bool = False,
        command_prefix: str = "/",
        public_memory_snippet: str = "",
        gap_fill_candidate: bool = False,
    ) -> DailyRhythmVerdict:
        stamp = time.time() if now is None else float(now)
        sid = str(session_id or "")
        enabled = bool(getattr(cfg, "daily_rhythm_enabled", True)) if cfg is not None else True
        if not enabled:
            return DailyRhythmVerdict(True, "disabled", REASON["disabled"], state=STATE_AWAKE)

        sess = self._ensure(sid, stamp, timezone=str(getattr(cfg, "rhythm_timezone", "") or ""))
        presence = str(presence_knob or "sensible").lower()
        kind = str(occasion_kind or "neutral").lower()
        clean = (text or "").strip()

        force_sleep = bool(getattr(cfg, "rhythm_force_sleep", False)) if cfg is not None else False
        allow_wake = bool(getattr(cfg, "rhythm_allow_wake", True)) if cfg is not None else True
        allow_self_sleep = bool(getattr(cfg, "rhythm_allow_self_sleep", True)) if cfg is not None else True
        sleep_after_wind = bool(getattr(cfg, "rhythm_sleep_after_winddown", True)) if cfg is not None else True
        gn_quota = int(getattr(cfg, "rhythm_goodnight_text_quota", 1) or 1) if cfg is not None else 1
        gn_quota = max(1, min(2, gn_quota))
        morning_on = bool(getattr(cfg, "rhythm_morning_hi_enabled", True)) if cfg is not None else True
        skip_morning = bool(getattr(cfg, "rhythm_skip_morning_hi_tonight", False)) if cfg is not None else False
        share_slots = int(getattr(cfg, "rhythm_day_share_slots", 1) or 0) if cfg is not None else 1
        share_slots = max(0, min(2, share_slots))
        insomnia_on = bool(getattr(cfg, "rhythm_insomnia_enabled", False)) if cfg is not None else False

        sess.force_sleep = force_sleep
        # Wake before timers can replace the original overnight sleep timestamp.
        if not force_sleep:
            self._maybe_end_overnight_sleep(sess, stamp)

        # Advance timers / sleep transitions before acting on this turn.
        self._tick_sleep(
            sess,
            stamp=stamp,
            telemetrics=telemetrics,
            recent_nodes=recent_nodes,
            bot_id=bot_id,
            sleep_after_wind=sleep_after_wind,
            allow_self_sleep=allow_self_sleep,
            force_sleep=force_sleep,
        )

        if force_sleep and sess.state not in ASLEEP_STATES and sess.state != STATE_BRIEF_WAKE:
            self._enter_asleep(sess, stamp, kind="self", reason="force_sleep")

        if not force_sleep:
            self._maybe_end_overnight_sleep(sess, stamp)

        heat = _chat_heat(telemetrics, recent_nodes, stamp=stamp)

        # --- asleep / brief_wake ---------------------------------------------
        if sess.state in ASLEEP_STATES or sess.state == STATE_BRIEF_WAKE:
            return self._eval_asleep(
                sess,
                stamp=stamp,
                text=clean,
                explicit=explicit,
                recent_nodes=recent_nodes,
                bot_id=bot_id,
                user_id=user_id,
                quoted_bot=quoted_bot,
                has_media=has_media,
                media_toward_bot=media_toward_bot,
                allow_wake=allow_wake,
                command_prefix=command_prefix,
                force_sleep=force_sleep,
            )

        # --- winding_down ----------------------------------------------------
        if sess.state == STATE_WINDING_DOWN:
            return self._eval_winding(
                sess,
                stamp=stamp,
                text=clean,
                explicit=explicit,
                heat=heat,
                gn_quota=gn_quota,
                sleep_after_wind=sleep_after_wind,
            )

        # --- insomniac (rare) ------------------------------------------------
        if sess.state == STATE_INSOMNIAC:
            if not insomnia_on:
                sess.state = STATE_AWAKE
            else:
                return self._eval_insomnia(sess, stamp=stamp, explicit=explicit)

        # Detect goodnight → enter winding_down (HARD: never jump to asleep).
        # Quota is committed in note_spoke after a successful send.
        if is_goodnight_text(clean) and (_local_hour(stamp, sess.timezone) >= 22 or _local_hour(stamp, sess.timezone) < 6):
            self._enter_winding(sess, stamp, heat=heat)
            if sess.goodnight_text_used < gn_quota and not sess.goodnight_replied:
                sess.last_reason_code = "wind_goodnight_ok"
                sess.last_reason_zh = REASON["wind_goodnight_ok"]
                self._note_why(sid, stamp, "wind_goodnight_ok", REASON["wind_goodnight_ok"])
                return DailyRhythmVerdict(
                    True,
                    "wind_goodnight_ok",
                    REASON["wind_goodnight_ok"],
                    state=STATE_WINDING_DOWN,
                    action="goodnight_reply",
                    force_scale=0.85,
                    length_hint="brief",
                    consume_goodnight_quota=True,
                )
            # Already used quota on enter — stay winding, silence later waves.
            sess.last_reason_code = "wind_later_silence"
            sess.last_reason_zh = REASON["wind_later_silence"]
            self._note_why(sid, stamp, "wind_later_silence", REASON["wind_later_silence"])
            return DailyRhythmVerdict(
                False if not explicit else True,
                "wind_later_silence" if not explicit else "ok",
                REASON["wind_later_silence"] if not explicit else REASON["ok"],
                state=STATE_WINDING_DOWN,
                force_scale=0.3 if not explicit else 1.0,
                length_hint="brief",
                proactive_blocked=True,
            )

        # --- awake daytime ---------------------------------------------------
        # Optional morning hi (atmosphere wake window, not real calendar claim).
        if morning_on and not sess.morning_hi_done and self._in_morning_window(stamp, sess):
            blocked = (
                skip_morning
                or presence == "ghost"
                or kind in {"conflict", "deciding"}
                or private_field
            )
            if blocked:
                code = "morning_hi_tonight_off" if skip_morning else "morning_hi_skip"
                # Do not auto-speak; leave allow to rest of gate.
                if not explicit and (is_morning_text(clean) or presence == "lively"):
                    sess.last_reason_code = code
                    sess.last_reason_zh = REASON[code]
                    # Not a hard deny of all speech — only deny morning-hi proactive.
                    pass
            elif not explicit and (is_morning_text(clean) or presence in {"sensible", "lively"}):
                # Suggest morning hi as optional ambient; caller may still need WTS/quota.
                # Only emit as soft allow with action when others said morning or lively gap.
                if is_morning_text(clean) or (presence == "lively" and not clean):
                    return DailyRhythmVerdict(
                        True,
                        "morning_hi_ok",
                        REASON["morning_hi_ok"],
                        state=STATE_AWAKE,
                        action="morning_hi",
                        force_scale=0.7,
                        length_hint="brief",
                        morning_hi=True,
                    )

        # Day share slots: prefer gap-fill/memory; no invented brag schedule.
        if share_slots > 0 and sess.day_share_used < share_slots and not explicit:
            if kind in {"conflict", "deciding"} or private_field or presence == "ghost":
                pass
            elif gap_fill_candidate or (public_memory_snippet and presence == "lively"):
                # Soft hint — actual speak still goes through useful_proactive quota.
                return DailyRhythmVerdict(
                    True,
                    "day_share_ok",
                    REASON["day_share_ok"],
                    state=STATE_AWAKE,
                    action="day_share",
                    force_scale=0.75,
                    length_hint="brief",
                    day_share=True,
                )

        # Insomnia: default OFF; if enabled, extremely rare ambient line.
        if insomnia_on and not explicit and sess.insomnia_lines_today < 1:
            if kind not in {"conflict", "deciding"} and presence != "ghost":
                if self._insomnia_roll(sid, stamp, sess):
                    return DailyRhythmVerdict(
                        True,
                        "insomnia_ok",
                        REASON["insomnia_ok"],
                        state=STATE_INSOMNIAC,
                        action="insomnia_line",
                        force_scale=0.4,
                        length_hint="brief",
                    )

        if (
            allow_self_sleep
            and sess.state == STATE_AWAKE
            and not force_sleep
            and not explicit
            and _local_hour(stamp) >= 23
        ):
            if heat == "cold" and _unique_speakers(recent_nodes, since=stamp - 40 * 60, bot_id=bot_id) <= 1:
                self._enter_asleep(sess, stamp, kind="self", reason="majority_asleep")
                sess.last_reason_code = "asleep_ambient"
                sess.last_reason_zh = REASON["asleep_ambient"]
                self._note_why(sid, stamp, "asleep_ambient", REASON["asleep_ambient"])
                return DailyRhythmVerdict(
                    False,
                    "asleep_ambient",
                    REASON["asleep_ambient"],
                    state=STATE_ASLEEP_SELF,
                    force_scale=0.0,
                    length_hint="brief",
                    proactive_blocked=True,
                )

        return DailyRhythmVerdict(True, "ok", REASON["ok"], state=sess.state)

    def note_spoke(
        self,
        session_id: str,
        *,
        verdict: Optional[DailyRhythmVerdict] = None,
        now: Optional[float] = None,
        text: str = "",
    ) -> None:
        stamp = time.time() if now is None else float(now)
        sess = self._ensure(str(session_id or ""), stamp)
        if verdict is None:
            return
        if verdict.consume_goodnight_quota or verdict.action == "goodnight_reply":
            sess.goodnight_replied = True
            sess.goodnight_text_used = min(2, int(sess.goodnight_text_used) + 1)
            if sess.state == STATE_AWAKE:
                # Safety: speaking goodnight must land in winding_down, never asleep.
                self._enter_winding(sess, stamp, heat="normal")
        if verdict.action == "goodnight_reply" and sess.state != STATE_WINDING_DOWN:
            self._enter_winding(sess, stamp, heat="normal")
        if verdict.consume_day_share or verdict.day_share:
            sess.day_share_used = min(2, sess.day_share_used + 1)
        if verdict.morning_hi or verdict.action == "morning_hi":
            sess.morning_hi_done = True
        if verdict.action == "wake_reply":
            sess.wake_replies_today += 1
            sess.state = STATE_BRIEF_WAKE
            sess.brief_wake_until = stamp + 10 * 60
            sess.asleep_kind = sess.asleep_kind or "after_wind"
        if verdict.action == "insomnia_line":
            sess.state = STATE_INSOMNIAC
            sess.insomnia_lines_today = max(sess.insomnia_lines_today, 1)
            sess.insomnia_last_at = stamp
        if text and is_goodnight_text(text):
            sess.pre_sleep_bot_msg_hint = (text or "")[:80]

    def status(self, session_id: str = "", *, now: Optional[float] = None) -> Dict[str, Any]:
        stamp = time.time() if now is None else float(now)
        if session_id:
            sess = self._ensure(str(session_id), stamp)
            self._maybe_end_overnight_sleep(sess, stamp)
            return {
                "state": sess.state,
                "state_zh": STATE_LABEL_ZH.get(sess.state, sess.state),
                "goodnight_text_used": sess.goodnight_text_used,
                "day_share_used": sess.day_share_used,
                "morning_hi_done": sess.morning_hi_done,
                "wake_replies_today": sess.wake_replies_today,
                "insomnia_lines_today": sess.insomnia_lines_today,
                "last_reason_code": sess.last_reason_code,
                "last_reason_zh": sess.last_reason_zh,
                "wind_started_at": sess.wind_started_at,
                "asleep_since": sess.asleep_since,
                "last_wake_at": sess.last_wake_at,
                "timezone": sess.timezone,
                "brief_wake_until": sess.brief_wake_until,
            }
        # Aggregate: prefer most "interesting" state among sessions.
        if not self._sessions:
            return {
                "state": STATE_AWAKE,
                "state_zh": STATE_LABEL_ZH[STATE_AWAKE],
                "sessions": 0,
            }
        order = [
            STATE_INSOMNIAC,
            STATE_BRIEF_WAKE,
            STATE_WINDING_DOWN,
            STATE_ASLEEP_AFTER_WIND,
            STATE_ASLEEP_SELF,
            STATE_AWAKE,
        ]
        best = None
        for st in order:
            for sess in self._sessions.values():
                if sess.state == st:
                    best = sess
                    break
            if best is not None:
                break
        assert best is not None
        return {
            "state": best.state,
            "state_zh": STATE_LABEL_ZH.get(best.state, best.state),
            "sessions": len(self._sessions),
            "last_reason_code": best.last_reason_code,
            "last_reason_zh": best.last_reason_zh,
            "goodnight_text_used": best.goodnight_text_used,
            "day_share_used": best.day_share_used,
            "morning_hi_done": best.morning_hi_done,
        }

    def why_silent_rows(self, session_id: str = "", *, limit: int = 8) -> List[Dict[str, Any]]:
        if session_id:
            return list(self._why.get(str(session_id), []))[-limit:]
        rows: List[Dict[str, Any]] = []
        for items in self._why.values():
            rows.extend(items)
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return rows[:limit]

    def reset_session(self, session_id: str) -> None:
        sid = str(session_id)
        self._sessions.pop(sid, None)
        self._why.pop(sid, None)

    # --- internals ------------------------------------------------------------

    def _ensure(self, sid: str, stamp: float, *, timezone: Optional[str] = None) -> _SessionRhythm:
        sess = self._sessions.get(sid)
        if sess is None:
            sess = _SessionRhythm(sid=sid, timezone=timezone or "", day_key=_day_key(stamp, timezone or ""))
            self._sessions[sid] = sess
        if timezone is not None:
            sess.timezone = timezone
        day = _day_key(stamp, sess.timezone)
        if sess.day_key != day:
            sess.day_key = day
            sess.goodnight_text_used = 0
            sess.goodnight_replied = False
            sess.wake_replies_today = 0
            sess.morning_hi_done = False
            sess.day_share_used = 0
            sess.insomnia_lines_today = 0
            # Quota counters reset at local midnight. Sleep/winding must not:
            # 23:50 晚安 would otherwise abort into awake at 00:01, including
            # dashboard status() polls. Morning wake is _maybe_end_overnight_sleep.
        return sess

    def _note_why(self, sid: str, stamp: float, code: str, zh: str) -> None:
        bucket = self._why.setdefault(sid, [])
        bucket.append({"ts": stamp, "reason_code": code, "reason_zh": zh})
        if len(bucket) > 40:
            del bucket[:-30]

    def _enter_winding(self, sess: _SessionRhythm, stamp: float, *, heat: str) -> None:
        """HARD RULE: goodnight → winding_down only, never asleep_*."""
        if 6 <= _local_hour(stamp, sess.timezone) < 22:
            return
        if sess.state in ASLEEP_STATES:
            # Plain goodnight while asleep should not re-enter winding via this path.
            return
        if sess.state != STATE_WINDING_DOWN:
            sess.state = STATE_WINDING_DOWN
            sess.wind_started_at = stamp
            # Conservative 20–40 min cold window; hot chat pushes deadline out.
            base = 20 * 60 + int(hashlib.sha1(f"{sess.day_key}:{stamp:.0f}".encode()).hexdigest()[:4], 16) % (20 * 60)
            # base in [1200, 2399] ≈ 20–40min
            if heat == "hot":
                base = int(base * 1.5)
                sess.hot_delay_until = stamp + 15 * 60
                sess.last_reason_code = "wind_hot_delay"
                sess.last_reason_zh = REASON["wind_hot_delay"]
            sess.wind_sleep_deadline = stamp + float(base)
        elif heat == "hot":
            sess.wind_sleep_deadline = max(sess.wind_sleep_deadline, stamp + 15 * 60)
            sess.hot_delay_until = stamp + 15 * 60
            sess.last_reason_code = "wind_hot_delay"
            sess.last_reason_zh = REASON["wind_hot_delay"]

    def _enter_asleep(self, sess: _SessionRhythm, stamp: float, *, kind: str, reason: str) -> None:
        sess.state = STATE_ASLEEP_AFTER_WIND if kind == "after_wind" else STATE_ASLEEP_SELF
        sess.asleep_since = stamp
        sess.asleep_kind = kind
        sess.brief_wake_until = 0.0
        sess.last_reason_code = reason
        sess.last_reason_zh = REASON.get(reason, reason)

    def _tick_sleep(
        self,
        sess: _SessionRhythm,
        *,
        stamp: float,
        telemetrics: Any,
        recent_nodes: Sequence[Any],
        bot_id: str,
        sleep_after_wind: bool,
        allow_self_sleep: bool,
        force_sleep: bool,
    ) -> None:
        if sess.state == STATE_BRIEF_WAKE and sess.brief_wake_until and stamp >= sess.brief_wake_until:
            # Return to previous asleep kind.
            kind = sess.asleep_kind or "after_wind"
            self._enter_asleep(sess, stamp, kind=kind, reason="majority_asleep")
            return

        if sess.state == STATE_INSOMNIAC and sess.insomnia_last_at and stamp - sess.insomnia_last_at > 30 * 60:
            sess.state = STATE_AWAKE
            return

        if sess.state != STATE_WINDING_DOWN:
            return

        if not sleep_after_wind:
            return

        heat = _chat_heat(telemetrics, recent_nodes, stamp=stamp)
        if heat == "hot":
            sess.wind_sleep_deadline = max(sess.wind_sleep_deadline, stamp + 12 * 60)
            sess.hot_delay_until = stamp + 12 * 60
            sess.last_reason_code = "wind_hot_delay"
            sess.last_reason_zh = REASON["wind_hot_delay"]
            return

        # Must wait at least 20 minutes; uncertain + not cold → keep winding.
        elapsed = stamp - (sess.wind_started_at or stamp)
        if elapsed < 20 * 60:
            return
        deadline = sess.wind_sleep_deadline or (sess.wind_started_at + 30 * 60)
        if stamp < deadline and heat != "cold":
            return
        # Cold or past deadline with non-hot → majority asleep.
        if heat == "hot":
            return
        # Optional: if many still chatting lightly, wait until deadline hard cap 40min.
        if elapsed < 40 * 60 and heat == "normal" and stamp < deadline:
            return
        self._enter_asleep(sess, stamp, kind="after_wind", reason="majority_asleep")
        self._note_why(sess.sid or "",
            stamp,
            "majority_asleep",
            REASON["majority_asleep"],
        )

    def _eval_winding(
        self,
        sess: _SessionRhythm,
        *,
        stamp: float,
        text: str,
        explicit: bool,
        heat: str,
        gn_quota: int,
        sleep_after_wind: bool,
    ) -> DailyRhythmVerdict:
        if heat == "hot":
            sess.wind_sleep_deadline = max(sess.wind_sleep_deadline, stamp + 12 * 60)
            sess.last_reason_code = "wind_hot_delay"
            sess.last_reason_zh = REASON["wind_hot_delay"]

        if is_goodnight_text(text):
            if sess.goodnight_text_used < gn_quota and not sess.goodnight_replied:
                sess.last_reason_code = "wind_goodnight_ok"
                sess.last_reason_zh = REASON["wind_goodnight_ok"]
                return DailyRhythmVerdict(
                    True,
                    "wind_goodnight_ok",
                    REASON["wind_goodnight_ok"],
                    state=STATE_WINDING_DOWN,
                    action="goodnight_reply",
                    force_scale=0.85,
                    length_hint="brief",
                    consume_goodnight_quota=True,
                    proactive_blocked=True,
                )
            # Later goodnight waves: silence (optional emoji not modelled here).
            code = "wind_goodnight_done" if sess.goodnight_replied else "wind_later_silence"
            sess.last_reason_code = code
            sess.last_reason_zh = REASON[code]
            self._note_why(sess.sid or "",
            stamp,
                code,
                REASON[code],
            )
            if explicit:
                return DailyRhythmVerdict(
                    True, "ok", REASON["ok"], state=STATE_WINDING_DOWN,
                    length_hint="brief", force_scale=0.6, proactive_blocked=True,
                )
            return DailyRhythmVerdict(
                False, code, REASON[code], state=STATE_WINDING_DOWN,
                force_scale=0.0, length_hint="brief", proactive_blocked=True,
            )

        # Non-goodnight during wind-down: prefer silence for ambient; explicit ok short.
        if explicit:
            return DailyRhythmVerdict(
                True, "ok", REASON["ok"], state=STATE_WINDING_DOWN,
                length_hint="brief", force_scale=0.7, proactive_blocked=True,
            )
        sess.last_reason_code = "wind_later_silence"
        sess.last_reason_zh = REASON["wind_later_silence"]
        self._note_why(sess.sid or "",
            stamp,
            "wind_later_silence",
            REASON["wind_later_silence"],
        )
        return DailyRhythmVerdict(
            False,
            "wind_later_silence",
            REASON["wind_later_silence"],
            state=STATE_WINDING_DOWN,
            force_scale=0.0,
            length_hint="brief",
            proactive_blocked=True,
        )

    def _eval_asleep(
        self,
        sess: _SessionRhythm,
        *,
        stamp: float,
        text: str,
        explicit: bool,
        recent_nodes: Sequence[Any],
        bot_id: str,
        user_id: str,
        quoted_bot: bool,
        has_media: bool,
        media_toward_bot: bool,
        allow_wake: bool,
        command_prefix: str,
        force_sleep: bool,
    ) -> DailyRhythmVerdict:
        state = sess.state
        # Brief wake cooldown: only allow if still in window for follow-up? Prefer hush.
        if state == STATE_BRIEF_WAKE:
            if sess.brief_wake_until and stamp < sess.brief_wake_until:
                # Already woke once; ambient hush; explicit only if still whitelist and not second wake spam.
                if not explicit:
                    sess.last_reason_code = "brief_wake_cooldown"
                    sess.last_reason_zh = REASON["brief_wake_cooldown"]
                    self._note_why(sess.sid or "",
            stamp,
                        "brief_wake_cooldown",
                        REASON["brief_wake_cooldown"],
                    )
                    return DailyRhythmVerdict(
                        False,
                        "brief_wake_cooldown",
                        REASON["brief_wake_cooldown"],
                        state=STATE_BRIEF_WAKE,
                        force_scale=0.0,
                        length_hint="brief",
                        proactive_blocked=True,
                    )
                # Explicit during cooldown: allow short but do not extend wake chain much.
                return DailyRhythmVerdict(
                    True, "ok", REASON["ok"], state=STATE_BRIEF_WAKE,
                    length_hint="brief", force_scale=0.5, proactive_blocked=True,
                )

        # Wake whitelist
        wake_hit = False
        if allow_wake and not force_sleep:
            mentioned = explicit or _mentions_bot(text, recent_nodes, bot_id=bot_id, user_id=user_id)
            quoted = _quoted_bot(recent_nodes, bot_id=bot_id, quoted_bot=quoted_bot)
            help_hit = _help_toward_bot(text, explicit=explicit or mentioned)
            media_hit = bool(has_media and (media_toward_bot or mentioned or quoted))
            cmd_hit = _looks_command(text, command_prefix=command_prefix) and (mentioned or explicit or text.strip().startswith(command_prefix or "/"))
            # Cap @ goodnight wakes roughly once/day
            if mentioned and is_goodnight_text(text) and sess.wake_replies_today >= 1:
                wake_hit = False
            elif mentioned or quoted or help_hit or media_hit or cmd_hit:
                wake_hit = True

        if wake_hit:
            sess.last_reason_code = "asleep_wake_ok"
            sess.last_reason_zh = REASON["asleep_wake_ok"]
            return DailyRhythmVerdict(
                True,
                "asleep_wake_ok",
                REASON["asleep_wake_ok"],
                state=STATE_BRIEF_WAKE,
                action="wake_reply",
                force_scale=0.55,
                length_hint="brief",
                proactive_blocked=True,
            )

        # Plain goodnight / emoji / chatter ≠ wake
        if is_goodnight_text(text) and not explicit:
            sess.last_reason_code = "asleep_plain_gn"
            sess.last_reason_zh = REASON["asleep_plain_gn"]
            self._note_why(sess.sid or "",
            stamp,
                "asleep_plain_gn",
                REASON["asleep_plain_gn"],
            )
            return DailyRhythmVerdict(
                False,
                "asleep_plain_gn",
                REASON["asleep_plain_gn"],
                state=state if state in ASLEEP_STATES else STATE_ASLEEP_AFTER_WIND,
                force_scale=0.0,
                length_hint="brief",
                proactive_blocked=True,
            )

        # Ambient while asleep = 0
        if not explicit:
            sess.last_reason_code = "asleep_ambient"
            sess.last_reason_zh = REASON["asleep_ambient"]
            self._note_why(sess.sid or "",
            stamp,
                "asleep_ambient",
                REASON["asleep_ambient"],
            )
            return DailyRhythmVerdict(
                False,
                "asleep_ambient",
                REASON["asleep_ambient"],
                state=state if state in ASLEEP_STATES else STATE_ASLEEP_AFTER_WIND,
                force_scale=0.0,
                length_hint="brief",
                proactive_blocked=True,
            )

        # Explicit but not whitelist while asleep: still hush (懂事)
        sess.last_reason_code = "asleep_ambient"
        sess.last_reason_zh = REASON["asleep_ambient"]
        return DailyRhythmVerdict(
            False,
            "asleep_ambient",
            REASON["asleep_ambient"],
            state=state if state in ASLEEP_STATES else STATE_ASLEEP_AFTER_WIND,
            force_scale=0.0,
            length_hint="brief",
            proactive_blocked=True,
        )

    def _eval_insomnia(
        self,
        sess: _SessionRhythm,
        *,
        stamp: float,
        explicit: bool,
    ) -> DailyRhythmVerdict:
        if sess.insomnia_lines_today >= 1 and not explicit:
            sess.last_reason_code = "insomnia_cap"
            sess.last_reason_zh = REASON["insomnia_cap"]
            return DailyRhythmVerdict(
                False,
                "insomnia_cap",
                REASON["insomnia_cap"],
                state=STATE_INSOMNIAC,
                force_scale=0.0,
                length_hint="brief",
                proactive_blocked=True,
            )
        if explicit:
            return DailyRhythmVerdict(
                True, "ok", REASON["ok"], state=STATE_INSOMNIAC,
                length_hint="brief", force_scale=0.6, proactive_blocked=True,
            )
        return DailyRhythmVerdict(
            False,
            "insomnia_cap",
            REASON["insomnia_cap"],
            state=STATE_INSOMNIAC,
            proactive_blocked=True,
            force_scale=0.0,
            length_hint="brief",
        )

    def _maybe_end_overnight_sleep(self, sess: _SessionRhythm, stamp: float) -> None:
        """Leave overnight sleep when local daytime arrives.

        Wakes on the same calendar date (01:00 晚安 → 08:00 早安) and after a
        midnight rollover (23:50 晚安 → next-day 08:00). Midnight itself must
        not abort winding_down / asleep.
        """
        if sess.force_sleep:
            return
        now_hour = _local_hour(stamp, sess.timezone)
        if now_hour < 6 or now_hour >= 22:
            return
        started = 0.0
        if sess.state in ASLEEP_STATES or sess.state == STATE_BRIEF_WAKE:
            started = float(sess.asleep_since or 0)
        elif sess.state == STATE_WINDING_DOWN:
            started = float(sess.wind_started_at or 0)
        else:
            return
        if not started:
            return
        sess.state = STATE_AWAKE
        sess.last_wake_at = stamp
        sess.asleep_since = 0.0
        sess.brief_wake_until = 0.0
        sess.wind_started_at = 0.0
        sess.wind_sleep_deadline = 0.0
        sess.last_reason_code = "ok"
        sess.last_reason_zh = REASON["ok"]

    @staticmethod
    def _in_morning_window(stamp: float, sess: _SessionRhythm) -> bool:
        # Atmosphere prop: local morning band OR first stretch after waking from sleep.
        hour = _local_hour(stamp, sess.timezone)
        if 6 <= hour <= 11:
            return True
        if sess.last_wake_at and 0 <= stamp - sess.last_wake_at < 3 * 3600 and sess.state == STATE_AWAKE:
            return True
        return False

    @staticmethod
    def _insomnia_roll(sid: str, stamp: float, sess: _SessionRhythm) -> bool:
        # Extremely low probability + period hard cap (once/day already).
        if sess.insomnia_lines_today >= 1:
            return False
        hour = _local_hour(stamp, sess.timezone)
        if hour < 1 or hour > 4:
            return False
        # Deterministic sparse roll ~2% in late night band.
        digest = hashlib.sha1(f"insomnia:{sid}:{_day_key(stamp, sess.timezone)}".encode()).hexdigest()
        return int(digest[:4], 16) % 100 < 2
