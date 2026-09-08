"""Social poke (戳一戳) replies: variety, streak, optional poke-back.

Poke is a tap, not an attachment. Local short replies keep the host LLM from
treating an empty Poke event as a media file.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional, Sequence

from .vibe_analyzer import GroupChatMode


_ACK = (
    "嗯？",
    "干嘛啦",
    "戳我干嘛",
    "有事说事",
    "我在呢",
    "诶",
    "叫我？",
    "怎么了",
    "在的",
    "干哈",
)
_PLAYFUL = (
    "再戳我咬你",
    "手很闲啊",
    "戳什么戳",
    "别戳了别戳了",
    "哼",
    "再戳要生气了",
    "痒",
    "你很无聊吗",
    "戳回来了哈",
    "轻轻戳一下你",
)
_ANNOYED = (
    "停停停",
    "戳够了没",
    "再戳我不理你了",
    "有完没完",
    "……",
    "够了啊",
    "手酸不酸",
)

_STREAK_WINDOW = 90.0


@dataclass(frozen=True)
class PokeReplyDecision:
    speak: bool
    text: str
    poke_back: bool
    reason: str


class PokeReplyPolicy:
    """Picks a short social ack and whether to poke back."""

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._rng = rng if rng is not None else random.Random()

    def decide(
        self,
        *,
        at_bot: bool,
        presence: str = "sensible",
        streak: int = 1,
        vibe: Optional[GroupChatMode] = None,
    ) -> PokeReplyDecision:
        if not at_bot:
            return PokeReplyDecision(False, "", False, "poke_others")

        knob = str(presence or "sensible").strip().lower()
        if knob not in {"ghost", "sensible", "lively"}:
            knob = "sensible"
        count = max(1, int(streak or 1))
        banter = vibe == GroupChatMode.FAST_BANTER

        if count >= 6:
            if self._rng.random() < 0.45:
                return PokeReplyDecision(False, "", False, "poke_spam")
            if self._rng.random() < 0.35:
                return PokeReplyDecision(True, "", True, "poke_annoyed")
            return PokeReplyDecision(True, self._pick(_ANNOYED), False, "poke_annoyed")
        if count >= 3:
            pool = _PLAYFUL + _ANNOYED
            if self._rng.random() < (0.55 if knob == "lively" else 0.28):
                return PokeReplyDecision(True, "", True, "poke_repeat")
            return PokeReplyDecision(True, self._pick(pool), False, "poke_repeat")

        poke_back_p = {"ghost": 0.12, "sensible": 0.38, "lively": 0.68}.get(knob, 0.38)
        if banter:
            poke_back_p = min(0.9, poke_back_p + 0.12)
        if knob == "lively" and count <= 1 and self._rng.random() < 0.22:
            return PokeReplyDecision(True, "", True, "poke_back_only")

        pool: Sequence[str] = _ACK
        if knob == "lively" or banter or count >= 2:
            pool = _ACK + _PLAYFUL
        if self._rng.random() < poke_back_p:
            return PokeReplyDecision(True, "", True, "poke_ack")
        return PokeReplyDecision(True, self._pick(pool), False, "poke_ack")

    def _pick(self, pool: Sequence[str]) -> str:
        if not pool:
            return "嗯？"
        return pool[self._rng.randrange(len(pool))]


def next_poke_streak(
    store: dict[tuple[str, str], tuple[int, float]],
    *,
    session_id: str,
    user_id: str,
    now: float,
    window: float = _STREAK_WINDOW,
) -> int:
    """Increment a per-user poke streak; reset after ``window`` idle seconds."""
    key = (str(session_id or ""), str(user_id or ""))
    count, last = store.get(key, (0, 0.0))
    if now - last > window:
        count = 0
    count += 1
    store[key] = (count, now)
    if len(store) > 2000:
        cutoff = now - window
        stale = [item for item, value in store.items() if value[1] < cutoff]
        for item in stale:
            store.pop(item, None)
    return count


def drop_poke_streaks(store: dict[tuple[str, str], tuple[int, float]], session_id: str) -> None:
    sid = str(session_id or "")
    for key in [item for item in store if item[0] == sid]:
        store.pop(key, None)
