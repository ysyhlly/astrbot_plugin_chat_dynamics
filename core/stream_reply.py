"""Publish one complete, tool-free sentence; keep final delivery/history owned."""

import asyncio
import re
from types import SimpleNamespace


class PlainSentenceStream:
    def __init__(self):
        self.buffer = ""
        self.prefix = ""
        self.disabled = False

    def feed(self, chain):
        if self.prefix or self.disabled:
            return None
        if getattr(chain, "type", None) not in (None, "general", "llm_result"):
            return None
        parts = getattr(chain, "chain", ())
        if any(type(p).__name__.lower() not in {"plain", "text"} for p in parts):
            self.disabled = True
            return None
        text = "".join(getattr(p, "text", "") for p in parts)
        self.buffer += text
        if any(mark in self.buffer for mark in ("`", "*", "[CQ:", "<", '{"', "http://", "https://", "](")):
            self.disabled = True
            return None
        ends = [m.end() for m in re.finditer(r"[。！？!?][”’」』]?", self.buffer)]
        ends.extend(m.start() for m in re.finditer(r"\n\s*\n", self.buffer))
        end = next((end for end in sorted(ends) if 4 <= end <= 160), None)
        if end is not None:
            self.prefix = self.buffer[:end]
            return self.prefix
        if len(self.buffer) > 160:
            self.disabled = True
        return None


async def output_fragments(output, pacer, maximum, fragmenter, completed_fragmenter, fragments):
    """Populate the actual fragment list while waiting for the untouched tail."""
    prefix = getattr(output, "stream_prefix", "")
    if not prefix:
        fragments.extend(completed_fragmenter(output, pacer, maximum, fragmenter))
        for index, fragment in enumerate(fragments):
            yield index, fragment
        return
    fragments.append(prefix)
    yield 0, prefix
    task = getattr(output, "completion_task", None)
    if task is not None:
        await asyncio.shield(task)
    final_text = str(output.text or "")
    if not final_text.startswith(prefix):
        raise RuntimeError("stream_prefix_changed")
    tail = final_text[len(prefix) :].strip()
    if tail and maximum > 1:
        view = SimpleNamespace(text=tail, chains=[])
        remaining = completed_fragmenter(view, pacer, maximum - 1, fragmenter)
        for fragment in remaining:
            fragments.append(fragment)
            yield len(fragments) - 1, fragment
