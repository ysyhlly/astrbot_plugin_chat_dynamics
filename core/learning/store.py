"""Bounded append-only sample store, disabled unless a caller enables it.

The store is deliberately dumb: JSON lines, a hard cap, no rewriting of history.
It never holds message text, so a leak exposes decisions rather than chat.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .sample import InvalidSample, LearningSample

logger = logging.getLogger("astrbot_plugin_chat_dynamics.learning")

DEFAULT_LIMIT = 20000
DEFAULT_FILENAME = "learning_samples.jsonl"


class SampleStore:
    """Append labelled decisions; drop the oldest once the cap is reached."""

    def __init__(self, root: Path, *, enabled: bool = False, limit: int = DEFAULT_LIMIT,
                 filename: str = DEFAULT_FILENAME) -> None:
        self.path = Path(root) / filename
        self.enabled = bool(enabled)
        self.limit = max(1, int(limit))
        self.dropped = 0

    def append(self, samples: list[LearningSample]) -> int:
        """Write what validates; return how many rows were stored."""
        if not self.enabled or not samples:
            return 0
        rows = []
        for sample in samples:
            try:
                rows.append(json.dumps(sample.to_dict(), ensure_ascii=False, allow_nan=False))
            except (InvalidSample, ValueError, TypeError) as exc:
                logger.warning("[Learning] sample rejected: %s", type(exc).__name__)
        if not rows:
            return 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(rows) + "\n")
            self._enforce_limit()
        except OSError as exc:
            logger.warning("[Learning] store unavailable: %s", type(exc).__name__)
            return 0
        return len(rows)

    def read(self, limit: int = 0) -> list[LearningSample]:
        """Newest last; unreadable rows are skipped rather than fatal."""
        if not self.path.exists():
            return []
        result: list[LearningSample] = []
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        result.append(LearningSample.from_dict(json.loads(line)))
                    except (InvalidSample, ValueError, TypeError):
                        continue
        except OSError as exc:
            logger.warning("[Learning] store unreadable: %s", type(exc).__name__)
            return []
        return result[-limit:] if limit > 0 else result

    def _enforce_limit(self) -> None:
        rows = self.read()
        if len(rows) <= self.limit:
            return
        keep = rows[-self.limit:]
        self.dropped += len(rows) - len(keep)
        try:
            payload = "\n".join(json.dumps(s.to_dict(), ensure_ascii=False, allow_nan=False)
                                for s in keep) + "\n"
            self.path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            logger.warning("[Learning] trim failed: %s", type(exc).__name__)

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return
