"""Topic candidates the way the router recorded them, in either schema.

`routing.topic_candidates` has been written in two shapes: the legacy
`[score, id]` pair, and a structured row that carries an explicit rank and the
evidence that produced it. Positional indexing reads the first correctly and the
second not at all, so parsing lives here once and every consumer shares it.

The distinction that decides whether an error can be attributed at all is not
the shape but the presence:

* the key is **absent** — the router recorded nothing, so nothing can be said
  about whether the labelled topic was ever offered;
* the key is an **empty list** — the router looked and proposed nothing, which
  is a candidate-generation miss whenever a labelled topic existed.

Collapsing those two turns "we cannot attribute this error" into "retrieval
failed", and that is a claim the data does not support.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import NamedTuple

# A candidate list is a diagnostic, not an input: bound it so a corrupt store
# cannot turn an offline report into an unbounded one.
MAX_CANDIDATES = 64
MAX_TOPIC_ID = 256
MAX_RANK = 1_000_000


class Candidate(NamedTuple):
    """One offered topic; `rank` is 1-based and reflects the recorded order."""

    topic_id: str
    rank: int
    score: float | None


class Candidates(NamedTuple):
    """What one routing record said, including whether it said anything."""

    recorded: bool
    items: tuple
    dropped: int

    @property
    def ids(self) -> tuple:
        return tuple(item.topic_id for item in self.items)

    def top(self, limit: int) -> tuple:
        return self.ids[:max(0, limit)]


def _score(value: object) -> float | None:
    """A usable score, or None. Booleans are not numbers here by accident."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None
    return float(value)


def _topic_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if text and len(text) <= MAX_TOPIC_ID else ""


def _pair(entry: object) -> tuple | None:
    """Legacy `[score, topic_id]`; anything else is not a candidate."""
    if not isinstance(entry, Sequence) or isinstance(entry, (str, bytes)) or len(entry) != 2:
        return None
    topic_id = _topic_id(entry[1])
    return None if not topic_id else (_score(entry[0]), topic_id, None)


def _row(entry: Mapping) -> tuple | None:
    """Structured row; `rank` and `final_score` are each optional.

    An entry that only carries `topic_id` is still a candidate: it proves the
    topic was offered, which is what recall counts. It simply cannot take part
    in the score ordering, and it is never read as a score of zero.
    """
    topic_id = _topic_id(entry.get("topic_id"))
    if not topic_id:
        return None
    rank = entry.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= MAX_RANK:
        rank = None
    return (_score(entry.get("final_score")), topic_id, rank)


def _ordered(rows: list) -> list:
    """Explicit rank first, then descending score, then the order as written.

    The legacy shape stores an already sorted list, so its order is a valid
    rank. A list written the other way round is caught by the score fallback,
    which is why the stored order is only the last resort: reading it as rank
    silently reverses a list that was sorted by score before it was saved.
    """
    if any(row[2] is not None for row in rows):
        return sorted(rows, key=lambda row: (row[2] is None, row[2] or 0))
    if rows and all(row[0] is not None for row in rows):
        return sorted(rows, key=lambda row: row[0], reverse=True)
    return list(rows)


def parse_candidates(payload: object) -> Candidates:
    """Parse either schema, keeping "nothing recorded" apart from "none found".

    A value that is neither a sequence nor parseable is reported as not
    recorded rather than as an empty list: the store could not be read, which
    is not evidence that the router proposed nothing.
    """
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Sequence):
        return Candidates(False, (), 0)
    rows: list = []
    dropped = 0
    for entry in list(payload)[:MAX_CANDIDATES]:
        parsed = _row(entry) if isinstance(entry, Mapping) else _pair(entry)
        if parsed is None:
            dropped += 1
            continue
        rows.append(parsed)
    ordered = _ordered(rows)
    return Candidates(True, tuple(Candidate(topic_id, rank, score)
                                  for rank, (score, topic_id, _) in enumerate(ordered, 1)),
                      dropped)
