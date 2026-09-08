"""Useful proactive: gap-fill whitelist, quotas, newcomer caution, pace align.

Wired into DynamicsDecisionGate after manners/media/occasion. Not a parallel
planner and not LLM-decided. Degrades quietly when group_memory is missing.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("astrbot_plugin_chat_dynamics.useful_proactive")

# --- reason codes (zh surface) -------------------------------------------------

REASON = {
    "deciding_no_banter": "决策中不插科",
    "proactive_quota": "主动配额用尽",
    "newcomer_caution": "新人·更收",
    "gap_wait": "等待缺口闭合",
    "gap_fill_ok": "缺口补全主动",
    "cold_nudge_ok": "冷场公共记忆轻唤",
    "no_gap": "无缺口不主动",
    "private_or_conflict": "私场/冲突不主动",
    "ok": "主动检查通过",
}

_QUESTION_TAIL = ("?", "？", "吗", "呢", "嘛", "么")
_PASS_THROUGH_TIPS = {
    "嗯", "哦", "啊", "哈", "呃", "额", "嗯嗯", "哦哦", "哈哈",
    "6", "66", "666", "+",
}
_TIME_MARKERS = (
    "点", "点半", "点整", "今晚", "明天", "后天", "周", "星期", "上午", "下午",
    "中午", "晚上", "凌晨", "号", "月", ":", "：",
)
_PLACE_MARKERS = ("在", "地点", "那边", "门口", "车站", "地铁", "店", "馆", "楼")
_APPOINT_HINTS = ("约", "见面", "集合", "碰头", "聚会", "局", "出来玩", "一起去")
_HELP_FOLLOW_MARKERS = ("怎么办", "怎么弄", "帮我看", "求助", "报错", "请教")


@dataclass(frozen=True)
class UsefulProactiveVerdict:
    allow: bool
    reason_code: str
    reason_zh: str
    gap_kind: str = ""  # hanging_question|appointment_gap|help_followup|cold_memory_nudge|""
    gap_fingerprint: str = ""
    force_scale: float = 1.0
    length_hint: str = "normal"  # brief|normal|detailed
    delay_scale: float = 1.0  # >1 slower; pace align only
    proactive: bool = False  # True when this ambient turn is gap-fill proactive
    quota_used: int = 0
    quota_cap: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allow": self.allow,
            "reason_code": self.reason_code,
            "reason_zh": self.reason_zh,
            "gap_kind": self.gap_kind,
            "gap_fingerprint": self.gap_fingerprint,
            "force_scale": self.force_scale,
            "length_hint": self.length_hint,
            "delay_scale": self.delay_scale,
            "proactive": self.proactive,
            "quota_used": self.quota_used,
            "quota_cap": self.quota_cap,
        }


_PASS = UsefulProactiveVerdict(True, "ok", REASON["ok"])


def _cfg_int(cfg: Any, key: str, default: int) -> int:
    if cfg is None:
        return default
    val = getattr(cfg, key, None)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _fp(*parts: str) -> str:
    blob = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


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


def _is_question(text: str) -> bool:
    clean = (text or "").strip()
    if not clean:
        return False
    if "?" in clean or "？" in clean:
        return True
    return any(clean.endswith(t) for t in _QUESTION_TAIL)


def _is_pass_through_tip(text: str) -> bool:
    """True for filler that should not hide an older hanging question."""
    clean = re.sub(r"[。！？!?~\s]+", "", (text or "").strip())
    if not clean:
        return True
    return clean.lower() in _PASS_THROUGH_TIPS or (len(clean) <= 1 and not _is_question(text))


class UsefulProactiveGate:
    """Gap-fill proactive + newcomer caution + pace align + quotas."""

    def __init__(self) -> None:
        # session -> hour_bucket -> count
        self._hour_counts: Dict[str, Dict[str, int]] = {}
        # session -> topic_id -> count
        self._topic_counts: Dict[str, Dict[str, int]] = {}
        # session -> set of gap fingerprints already nudged
        self._gap_seen: Dict[str, set[str]] = {}
        # session -> user_id -> speak count (for newcomer)
        self._user_speaks: Dict[str, Dict[str, int]] = {}
        # session -> last help ts
        self._help_ts: Dict[str, float] = {}
        # session -> topic segment id (coarse)
        self._topic_seg: Dict[str, str] = {}

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
        group_memory: Any = None,
        public_memory_snippet: str = "",
        cfg: Any = None,
        now: Optional[float] = None,
        private_field: bool = False,
        media_privacy: bool = False,
    ) -> UsefulProactiveVerdict:
        stamp = time.time() if now is None else float(now)
        sid = str(session_id or "")
        uid = str(user_id or "")
        clean = (text or "").strip()
        presence = str(presence_knob or "sensible").lower()
        kind = str(occasion_kind or "neutral").lower()

        gap_fill_on = bool(getattr(cfg, "gap_fill_proactive_enabled", True)) if cfg is not None else True
        cold_on = bool(getattr(cfg, "cold_memory_nudge_enabled", True)) if cfg is not None else True
        newcomer_on = bool(getattr(cfg, "newcomer_caution_enabled", True)) if cfg is not None else True
        pace_on = bool(getattr(cfg, "pace_align_enabled", True)) if cfg is not None else True
        quota_on = bool(getattr(cfg, "proactive_quota_enabled", True)) if cfg is not None else True
        hour_cap = _cfg_int(cfg, "proactive_quota_per_hour", 2)
        topic_cap = _cfg_int(cfg, "proactive_quota_per_topic", 1)

        # Track speaker frequency always (for newcomer), even on explicit.
        if uid:
            bucket = self._user_speaks.setdefault(sid, {})
            bucket[uid] = int(bucket.get(uid, 0)) + 1

        if kind in {"serious_help"} or any(m in clean for m in _HELP_FOLLOW_MARKERS):
            self._help_ts[sid] = stamp

        # Pace hints (delay/length only) — applied regardless of allow/deny.
        delay_scale, length_hint = self._pace_hints(telemetrics, pace_on=pace_on)
        if kind == "deciding":
            length_hint = "brief"

        used, cap = self._quota_snapshot(sid, stamp, hour_cap=hour_cap)

        # Explicit @ / strong address: never block by proactive quota; still
        # return pace/newcomer length hints. Newcomer caution only blocks ambient naming.
        if explicit:
            return UsefulProactiveVerdict(
                True, "ok", REASON["ok"],
                force_scale=1.0, length_hint=length_hint, delay_scale=delay_scale,
                quota_used=used, quota_cap=cap,
            )

        # Hard no for private / conflict / privacy media ambient.
        if private_field or media_privacy or kind == "conflict":
            return UsefulProactiveVerdict(
                False, "private_or_conflict", REASON["private_or_conflict"],
                force_scale=0.0, length_hint="brief", delay_scale=delay_scale,
                quota_used=used, quota_cap=cap,
            )

        # Deciding: forbid banter / meme ambient; allow only short clarification gaps.
        if kind == "deciding":
            gap = self._detect_gap(
                sid=sid,
                stamp=stamp,
                text=clean,
                recent_nodes=recent_nodes,
                bot_id=bot_id,
                group_memory=group_memory,
                public_memory_snippet=public_memory_snippet,
                presence=presence,
                cold_on=False,  # no cold nudge while deciding
                telemetrics=telemetrics,
            )
            if gap is None or gap[0] not in {"hanging_question", "appointment_gap"}:
                return UsefulProactiveVerdict(
                    False, "deciding_no_banter", REASON["deciding_no_banter"],
                    force_scale=0.0, length_hint="brief", delay_scale=delay_scale,
                    quota_used=used, quota_cap=cap,
                )
            # fall through with gap for quota/fingerprint checks below
        else:
            gap = None
            if gap_fill_on:
                gap = self._detect_gap(
                    sid=sid,
                    stamp=stamp,
                    text=clean,
                    recent_nodes=recent_nodes,
                    bot_id=bot_id,
                    group_memory=group_memory,
                    public_memory_snippet=public_memory_snippet,
                    presence=presence,
                    cold_on=cold_on,
                    telemetrics=telemetrics,
                )

        # Newcomer caution: raise bar; never ambient-name newcomers.
        if newcomer_on and uid and self._is_newcomer(sid, uid):
            # Ambient proactive toward newcomer is denied; short replies only if gap is not naming them.
            if gap is not None and gap[0] in {"hanging_question", "help_followup"}:
                # Allow brief gap-fill without naming — still mark caution in force.
                pass
            elif gap is None:
                return UsefulProactiveVerdict(
                    False, "newcomer_caution", REASON["newcomer_caution"],
                    force_scale=0.2, length_hint="brief", delay_scale=max(delay_scale, 1.25),
                    quota_used=used, quota_cap=cap,
                )
            else:
                return UsefulProactiveVerdict(
                    False, "newcomer_caution", REASON["newcomer_caution"],
                    force_scale=0.2, length_hint="brief", delay_scale=max(delay_scale, 1.25),
                    quota_used=used, quota_cap=cap,
                )

        # Ghost / sensible: only gap-fill proactive (no idle chatter opener).
        if presence in {"ghost", "sensible"}:
            if gap is None:
                return UsefulProactiveVerdict(
                    False, "no_gap", REASON["no_gap"],
                    force_scale=0.0, length_hint=length_hint, delay_scale=delay_scale,
                    quota_used=used, quota_cap=cap,
                )
        else:
            # lively: may speak without gap, but still subject to quota when proactive-ish
            if gap is None:
                # Not counting as gap-fill proactive — leave to WTS; do not consume quota.
                return UsefulProactiveVerdict(
                    True, "ok", REASON["ok"],
                    force_scale=1.0, length_hint=length_hint, delay_scale=delay_scale,
                    proactive=False, quota_used=used, quota_cap=cap,
                )

        assert gap is not None
        gap_kind, fingerprint, gap_force, gap_reason = gap

        # Same gap once.
        seen = self._gap_seen.setdefault(sid, set())
        if fingerprint and fingerprint in seen:
            return UsefulProactiveVerdict(
                False, "gap_wait", REASON["gap_wait"],
                gap_kind=gap_kind, gap_fingerprint=fingerprint,
                force_scale=0.0, length_hint="brief", delay_scale=delay_scale,
                quota_used=used, quota_cap=cap,
            )

        # Quotas (tight defaults).
        if quota_on and not self._quota_available(sid, stamp, hour_cap=hour_cap, topic_cap=topic_cap):
            used2, cap2 = self._quota_snapshot(sid, stamp, hour_cap=hour_cap)
            return UsefulProactiveVerdict(
                False, "proactive_quota", REASON["proactive_quota"],
                gap_kind=gap_kind, gap_fingerprint=fingerprint,
                force_scale=0.0, length_hint="brief", delay_scale=delay_scale,
                quota_used=used2, quota_cap=cap2,
            )

        if newcomer_on and uid and self._is_newcomer(sid, uid):
            length_hint = "brief"
            delay_scale = max(delay_scale, 1.2)
            gap_force = min(gap_force, 0.55)

        used_now, cap_now = self._quota_snapshot(sid, stamp, hour_cap=hour_cap)
        return UsefulProactiveVerdict(
            True,
            "gap_fill_ok" if gap_kind != "cold_memory_nudge" else "cold_nudge_ok",
            gap_reason,
            gap_kind=gap_kind,
            gap_fingerprint=fingerprint,
            force_scale=gap_force,
            length_hint=length_hint if length_hint == "brief" else "brief",
            delay_scale=delay_scale,
            proactive=True,
            quota_used=used_now,
            quota_cap=cap_now,
        )

    def note_proactive(
        self,
        session_id: str,
        *,
        gap_fingerprint: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Record that a gap-fill proactive was spoken (consumes quota + fingerprint)."""
        stamp = time.time() if now is None else float(now)
        sid = str(session_id or "")
        hour_key = time.strftime("%Y%m%d%H", time.localtime(stamp))
        hours = self._hour_counts.setdefault(sid, {})
        hours[hour_key] = int(hours.get(hour_key, 0)) + 1
        topic = self._topic_seg.get(sid) or hour_key
        topics = self._topic_counts.setdefault(sid, {})
        topics[topic] = int(topics.get(topic, 0)) + 1
        if gap_fingerprint:
            self._gap_seen.setdefault(sid, set()).add(gap_fingerprint)

    def quota_status(self, session_id: str = "", *, now: Optional[float] = None, hour_cap: int = 2) -> Dict[str, int]:
        stamp = time.time() if now is None else float(now)
        if session_id:
            used, cap = self._quota_snapshot(str(session_id), stamp, hour_cap=hour_cap)
            return {"proactive_used": used, "proactive_cap": cap}
        # Aggregate across sessions for dashboard.
        hour_key = time.strftime("%Y%m%d%H", time.localtime(stamp))
        used = sum(int(v.get(hour_key, 0)) for v in self._hour_counts.values())
        return {"proactive_used": used, "proactive_cap": int(hour_cap)}

    def reset_session(self, session_id: str) -> None:
        sid = str(session_id)
        self._hour_counts.pop(sid, None)
        self._topic_counts.pop(sid, None)
        self._gap_seen.pop(sid, None)
        self._user_speaks.pop(sid, None)
        self._help_ts.pop(sid, None)
        self._topic_seg.pop(sid, None)

    def touch_topic(self, session_id: str, topic_id: str = "") -> None:
        sid = str(session_id)
        tid = (topic_id or "").strip() or time.strftime("%Y%m%d%H")
        prev = self._topic_seg.get(sid)
        if prev != tid:
            self._topic_seg[sid] = tid
            self._topic_counts.setdefault(sid, {})[tid] = self._topic_counts.get(sid, {}).get(tid, 0)

    # --- internals ------------------------------------------------------------

    def _quota_snapshot(self, sid: str, stamp: float, *, hour_cap: int) -> Tuple[int, int]:
        hour_key = time.strftime("%Y%m%d%H", time.localtime(stamp))
        used = int(self._hour_counts.get(sid, {}).get(hour_key, 0))
        return used, max(0, int(hour_cap))

    def _quota_available(self, sid: str, stamp: float, *, hour_cap: int, topic_cap: int) -> bool:
        used, cap = self._quota_snapshot(sid, stamp, hour_cap=hour_cap)
        if used >= cap:
            return False
        topic = self._topic_seg.get(sid) or time.strftime("%Y%m%d%H", time.localtime(stamp))
        t_used = int(self._topic_counts.get(sid, {}).get(topic, 0))
        if t_used >= max(0, int(topic_cap)):
            return False
        return True

    def _is_newcomer(self, sid: str, uid: str, *, threshold: int = 3) -> bool:
        return int(self._user_speaks.get(sid, {}).get(uid, 0)) <= int(threshold)

    @staticmethod
    def _pace_hints(telemetrics: Any, *, pace_on: bool) -> Tuple[float, str]:
        if not pace_on:
            return 1.0, "normal"
        mpm = float(getattr(telemetrics, "mpm", 0) or 0)
        # slow / normal / fast — delay & length only; never grants more proactive.
        if mpm > 0 and mpm < 3.0:
            return 1.35, "brief"
        if mpm >= 12.0:
            return 0.85, "brief"
        return 1.0, "normal"

    def _detect_gap(
        self,
        *,
        sid: str,
        stamp: float,
        text: str,
        recent_nodes: Sequence[Any],
        bot_id: str,
        group_memory: Any,
        public_memory_snippet: str,
        presence: str,
        cold_on: bool,
        telemetrics: Any,
    ) -> Optional[Tuple[str, str, float, str]]:
        """Return (kind, fingerprint, force, reason_zh) or None."""
        # 1) hanging question: last human Q unanswered beyond threshold.
        hang = self._hanging_question(recent_nodes, bot_id=bot_id, stamp=stamp)
        if hang is not None:
            q_text, q_user, q_ts = hang
            return (
                "hanging_question",
                _fp("hq", sid, q_user, q_text[:40]),
                0.85,
                REASON["gap_fill_ok"],
            )

        # 2) appointment gap from notebook (degrade if missing).
        appt = self._appointment_gap(group_memory, sid, stamp=stamp)
        if appt is not None:
            return (
                "appointment_gap",
                _fp("ag", sid, appt),
                0.7,
                REASON["gap_fill_ok"],
            )

        # 3) strong help then long silence.
        help_ts = float(self._help_ts.get(sid, 0) or 0)
        if help_ts and stamp - help_ts >= 90.0:
            # Only if recent traffic is quiet / no bot reply after help.
            if not self._bot_spoke_since(recent_nodes, bot_id=bot_id, since=help_ts):
                return (
                    "help_followup",
                    _fp("hf", sid, str(int(help_ts))),
                    0.75,
                    REASON["gap_fill_ok"],
                )

        # 4) cold memory nudge — lively only.
        if cold_on and presence == "lively":
            mpm = float(getattr(telemetrics, "mpm", 0) or 0)
            chill = mpm > 0 and mpm < 2.5
            snippet = (public_memory_snippet or "").strip()
            if not snippet and group_memory is not None:
                snippet = self._public_memory_line(group_memory, sid, stamp=stamp)
            if chill and snippet:
                return (
                    "cold_memory_nudge",
                    _fp("cm", sid, snippet[:48]),
                    0.55,
                    REASON["cold_nudge_ok"],
                )
            # No notebook / no snippet → stay quiet (do not invent group history).
        return None

    def _hanging_question(
        self,
        recent_nodes: Sequence[Any],
        *,
        bot_id: str,
        stamp: float,
        hang_seconds: float = 45.0,
    ) -> Optional[Tuple[str, str, float]]:
        if not recent_nodes:
            return None
        nodes = list(recent_nodes)
        # Walk backward. The live DAG appends the current flush first, so the
        # tip is often a fresh non-question ("嗯") or the question itself at age 0.
        for node in reversed(nodes):
            if bot_id and _node_user(node) == str(bot_id):
                return None
            text = _node_text(node)
            ts = _node_ts(node) or 0.0
            age = stamp - ts if ts else hang_seconds
            if not _is_question(text):
                if ts and age < hang_seconds and _is_pass_through_tip(text):
                    continue
                return None
            if ts and age < hang_seconds:
                continue
            return text, _node_user(node), ts
        return None

    def _appointment_gap(self, group_memory: Any, sid: str, *, stamp: float) -> Optional[str]:
        if group_memory is None:
            return None
        try:
            data = group_memory.list_all(sid) if hasattr(group_memory, "list_all") else None
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if float(data.get("mute_until") or 0) > stamp:
            return None
        for row in data.get("reminders") or []:
            if row.get("expired") or row.get("nudged"):
                continue
            text = str(row.get("text") or "")
            if not text:
                continue
            if not any(h in text for h in _APPOINT_HINTS):
                continue
            has_time = any(m in text for m in _TIME_MARKERS)
            has_place = any(m in text for m in _PLACE_MARKERS)
            if not has_time or not has_place:
                return str(row.get("id") or text[:40])
        return None

    def _public_memory_line(self, group_memory: Any, sid: str, *, stamp: float) -> str:
        try:
            data = group_memory.list_all(sid) if hasattr(group_memory, "list_all") else None
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        if float(data.get("mute_until") or 0) > stamp:
            return ""
        for row in data.get("anniversaries") or []:
            title = str(row.get("title") or "").strip()
            if title:
                return title
        for row in data.get("reminders") or []:
            if row.get("expired"):
                continue
            text = str(row.get("text") or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _bot_spoke_since(recent_nodes: Sequence[Any], *, bot_id: str, since: float) -> bool:
        if not bot_id:
            return False
        for node in recent_nodes:
            if _node_user(node) != str(bot_id):
                continue
            ts = _node_ts(node)
            if not ts or ts >= since:
                return True
        return False
