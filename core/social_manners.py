"""Social manners gates: relay baton, private field, hyped quota."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("astrbot_plugin_chat_dynamics.social_manners")


@dataclass(frozen=True)
class MannersVerdict:
    allow: bool
    reason_code: str
    reason_zh: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "allow": self.allow,
            "reason_code": self.reason_code,
            "reason_zh": self.reason_zh,
        }


_ALLOW = MannersVerdict(True, "ok", "分寸检查通过")


@dataclass
class _BanterSegment:
    started_at: float
    hyped_used: int = 0
    last_activity: float = 0.0


class SocialMannersGate:
    """Conservative social-manners gating for both legacy and persona paths."""

    def __init__(self) -> None:
        self._banter_segments: Dict[str, _BanterSegment] = {}
        self._why_silent: Dict[str, List[Dict[str, Any]]] = {}
        self._why_spoke: Dict[str, List[Dict[str, Any]]] = {}
        self._intervene_counts: Dict[str, int] = {}
        self._quiet_counts: Dict[str, int] = {}
        self._day_stamp: Dict[str, str] = {}

    # --- public API --------------------------------------------------------

    def evaluate(
        self,
        *,
        session_id: str,
        user_id: str,
        text: str,
        recent_nodes: Sequence[Any] = (),
        bot_id: str = "",
        explicit: bool = False,
        occasion_kind: str = "neutral",
        presence_knob: str = "sensible",
        social_manners_enabled: bool = True,
        relay_baton_enabled: bool = True,
        private_field_enabled: bool = True,
        hyped_quota_enabled: bool = True,
        now: Optional[float] = None,
    ) -> MannersVerdict:
        stamp = time.time() if now is None else float(now)
        self._roll_day(session_id, stamp)

        if presence_knob == "ghost" and not explicit:
            return self._deny(session_id, stamp, "presence_ghost", "分寸旋钮在隐身档，少开口")

        if not social_manners_enabled:
            return _ALLOW

        if explicit:
            # @ may reply; hyped quota still avoids meme stacking after a hyped join.
            if (
                hyped_quota_enabled
                and occasion_kind == "banter"
                and self._hyped_used(session_id) >= 1
            ):
                # Allow short reply but mark as non-hype; still allow speak.
                return MannersVerdict(True, "explicit_after_hype", "被点名可回，但不叠梗")
            return _ALLOW

        # Conflict occasions prefer silence even before manners.
        if occasion_kind == "conflict":
            return self._deny(session_id, stamp, "conflict_silence", "冲突场合，先安静不插话")

        if relay_baton_enabled and self._relay_baton_active(recent_nodes, bot_id=bot_id, user_id=user_id):
            return self._deny(session_id, stamp, "relay_baton", "两人正在互回，先靠边不插中间")

        if private_field_enabled and self._private_field_active(
            recent_nodes, bot_id=bot_id, user_id=user_id, text=text, occasion_kind=occasion_kind
        ):
            return self._deny(session_id, stamp, "private_field", "像是两人私场，不硬插")

        if hyped_quota_enabled and occasion_kind == "banter":
            self._touch_banter(session_id, stamp)
            if self._hyped_used(session_id) >= 1:
                return self._deny(session_id, stamp, "hyped_quota", "这段整活已经起哄过一次，先旁听")

        return _ALLOW

    def note_intervene(
        self,
        session_id: str,
        *,
        occasion_kind: str = "neutral",
        hyped: bool = False,
        now: Optional[float] = None,
        reason_code: str = "",
        reason_zh: str = "",
    ) -> None:
        stamp = time.time() if now is None else float(now)
        self._roll_day(session_id, stamp)
        self._intervene_counts[session_id] = self._intervene_counts.get(session_id, 0) + 1
        if hyped or occasion_kind == "banter":
            self._touch_banter(session_id, stamp)
            seg = self._banter_segments.get(session_id)
            if seg is not None:
                seg.hyped_used = min(1, seg.hyped_used + 1)
        code = str(reason_code or ("hyped_join" if hyped else "spoke"))
        zh = str(reason_zh or ("这段整活轻接了一下" if hyped else "接了一句"))
        bucket = self._why_spoke.setdefault(session_id, [])
        bucket.append({"ts": stamp, "reason_code": code, "reason_zh": zh, "action": "speak"})
        if len(bucket) > 80:
            del bucket[:-80]

    def note_quiet(self, session_id: str, *, reason_code: str, reason_zh: str, now: Optional[float] = None) -> None:
        stamp = time.time() if now is None else float(now)
        self._roll_day(session_id, stamp)
        self._quiet_counts[session_id] = self._quiet_counts.get(session_id, 0) + 1
        self._deny(session_id, stamp, reason_code, reason_zh, count=False)

    def today_stats(self, session_id: str = "") -> Dict[str, Any]:
        if session_id:
            return {
                "intervene": int(self._intervene_counts.get(session_id, 0)),
                "quiet": int(self._quiet_counts.get(session_id, 0)),
                "why_silent": list(self._why_silent.get(session_id, []))[-12:],
                "why_spoke": list(self._why_spoke.get(session_id, []))[-12:],
            }
        return {
            "intervene": sum(self._intervene_counts.values()),
            "quiet": sum(self._quiet_counts.values()),
            "why_silent": self._recent_why_silent(limit=12),
            "why_spoke": self._recent_why_spoke(limit=12),
        }

    def reset_session(self, session_id: str) -> None:
        self._banter_segments.pop(session_id, None)
        self._why_silent.pop(session_id, None)
        self._why_spoke.pop(session_id, None)
        self._intervene_counts.pop(session_id, None)
        self._quiet_counts.pop(session_id, None)
        self._day_stamp.pop(session_id, None)

    # --- internals ---------------------------------------------------------

    def _deny(
        self,
        session_id: str,
        stamp: float,
        code: str,
        zh: str,
        *,
        count: bool = True,
    ) -> MannersVerdict:
        if count:
            self._quiet_counts[session_id] = self._quiet_counts.get(session_id, 0) + 1
        bucket = self._why_silent.setdefault(session_id, [])
        bucket.append({"ts": stamp, "reason_code": code, "reason_zh": zh})
        if len(bucket) > 40:
            del bucket[:-40]
        logger.info("[ChatDynamics] why silent session=%s code=%s zh=%s", session_id[:12], code, zh)
        return MannersVerdict(False, code, zh)

    def _recent_why_silent(self, limit: int = 12) -> List[Dict[str, Any]]:
        return self._recent_bucket(self._why_silent, limit=limit)

    def _recent_why_spoke(self, limit: int = 12) -> List[Dict[str, Any]]:
        return self._recent_bucket(self._why_spoke, limit=limit)

    @staticmethod
    def _recent_bucket(store: Dict[str, List[Dict[str, Any]]], *, limit: int) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for sid, items in store.items():
            for item in items:
                row = dict(item)
                row["session_id"] = sid
                rows.append(row)
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return rows[:limit]

    def scene_track(self, session_id: str = "", *, limit: int = 64) -> List[Dict[str, Any]]:
        """Speak/silent ticks for the color-block replay rail. No message text."""
        rows: List[Dict[str, Any]] = []
        if session_id:
            for item in self._why_silent.get(session_id, []):
                row = dict(item)
                row["action"] = "silent"
                row["session_id"] = session_id
                rows.append(row)
            for item in self._why_spoke.get(session_id, []):
                row = dict(item)
                row["action"] = "speak"
                row["session_id"] = session_id
                rows.append(row)
        else:
            rows.extend(self._recent_why_silent(limit=limit * 2))
            for row in rows:
                row["action"] = row.get("action") or "silent"
            spoke = self._recent_why_spoke(limit=limit * 2)
            for row in spoke:
                row["action"] = row.get("action") or "speak"
            rows.extend(spoke)
        rows.sort(key=lambda r: float(r.get("ts") or 0))
        if session_id:
            return rows[-limit:]
        return rows[-limit:]

    def _roll_day(self, session_id: str, stamp: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(stamp))
        if self._day_stamp.get(session_id) != day:
            self._day_stamp[session_id] = day
            self._intervene_counts[session_id] = 0
            self._quiet_counts[session_id] = 0

    def _touch_banter(self, session_id: str, stamp: float) -> None:
        seg = self._banter_segments.get(session_id)
        if seg is None or stamp - seg.last_activity > 180.0:
            self._banter_segments[session_id] = _BanterSegment(started_at=stamp, hyped_used=0, last_activity=stamp)
        else:
            seg.last_activity = stamp

    def _hyped_used(self, session_id: str) -> int:
        seg = self._banter_segments.get(session_id)
        return int(seg.hyped_used) if seg else 0

    @staticmethod
    def _node_user(node: Any) -> str:
        return str(getattr(node, "user_id", "") or "")

    @staticmethod
    def _node_reply_to(node: Any) -> str:
        return str(getattr(node, "reply_to_id", "") or "")

    @staticmethod
    def _node_mentions(node: Any) -> List[str]:
        raw = getattr(node, "mentioned_users", None) or []
        return [str(x) for x in raw]

    def _relay_baton_active(
        self,
        recent_nodes: Sequence[Any],
        *,
        bot_id: str,
        user_id: str,
    ) -> bool:
        """Two humans exchanging: stay out unless gap/@/cold."""
        humans = [n for n in list(recent_nodes)[-8:] if self._node_user(n) and self._node_user(n) != bot_id]
        if len(humans) < 3:
            return False
        a = self._node_user(humans[-3])
        b = self._node_user(humans[-2])
        c = self._node_user(humans[-1])
        if len({a, b, c}) != 2:
            return False
        # Pattern A-B-A or A-B-B where last is continuing private exchange.
        pair = {a, b}
        if c not in pair:
            return False
        # If current user is a third party, not a baton case.
        if user_id and user_id not in pair and user_id != c:
            return False
        # Mentions of bot break the baton.
        for node in humans[-3:]:
            if bot_id and bot_id in self._node_mentions(node):
                return False
        # Explicit reply chain between the two humans strengthens the signal.
        id_map = {getattr(n, "msg_id", ""): self._node_user(n) for n in humans[-6:]}
        reply_links = 0
        for node in humans[-3:]:
            parent = self._node_reply_to(node)
            parent_user = id_map.get(parent, "")
            if parent_user and parent_user != self._node_user(node) and parent_user in pair:
                reply_links += 1
        # Conservative: require either reply link or clear A-B-A alternation.
        if reply_links >= 1:
            return True
        return a != b and c == a

    def _private_field_active(
        self,
        recent_nodes: Sequence[Any],
        *,
        bot_id: str,
        user_id: str,
        text: str,
        occasion_kind: str,
    ) -> bool:
        if occasion_kind in {"banter", "serious_help"}:
            return False
        clean = (text or "").strip()
        if any(x in clean for x in ("帮我", "求助", "怎么弄", "怎么办", "@")):
            return False
        humans = [n for n in list(recent_nodes)[-10:] if self._node_user(n) and self._node_user(n) != bot_id]
        if len(humans) < 2:
            return False
        # Look for A↔B reply pairs.
        id_to_user = {str(getattr(n, "msg_id", "")): self._node_user(n) for n in humans}
        pairs: Dict[Tuple[str, str], int] = {}
        for node in humans:
            parent = self._node_reply_to(node)
            parent_user = id_to_user.get(parent, "")
            me = self._node_user(node)
            if not parent_user or parent_user == me:
                continue
            if bot_id and (bot_id == parent_user or bot_id in self._node_mentions(node)):
                continue
            key = tuple(sorted((me, parent_user)))
            pairs[key] = pairs.get(key, 0) + 1
        if not pairs:
            # Fallback: last 4 messages only involve two humans and current continues.
            last = humans[-4:]
            users = {self._node_user(n) for n in last}
            if len(users) == 2 and user_id in users:
                # Conservative false positive OK: treat as private if no third speaker.
                third = any(self._node_user(n) not in users for n in humans[-6:-4])
                return not third and len(last) >= 3
            return False
        top = max(pairs.values())
        return top >= 2
