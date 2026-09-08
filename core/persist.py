"""Shared filename sanitization and crash-safe JSON writes."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# Keep letters, digits, underscore, dot, dash, and @. Strip ':' so Windows
# NTFS does not treat `platform:type:id` UMO tokens as alternate data streams.
_SAFE_TOKEN = re.compile(r"[^\w.\-@]+")


def safe_umo(umo: str) -> str:
    token = _SAFE_TOKEN.sub("_", str(umo or "unknown")).strip("._")
    return (token or "unknown")[:120]


def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=0)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
