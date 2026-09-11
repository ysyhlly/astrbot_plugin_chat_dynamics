"""Shared filename sanitization and crash-safe JSON writes."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

# Keep letters, digits, underscore, dot, dash, and @. Strip ':' so Windows
# NTFS does not treat `platform:type:id` UMO tokens as alternate data streams.
_SAFE_TOKEN = re.compile(r"[^\w.\-@]+")
logger = logging.getLogger("astrbot_plugin_chat_dynamics.persist")


def safe_umo(umo: str) -> str:
    raw = str(umo or "")
    prefix = legacy_safe_umo(raw)[:64]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def legacy_safe_umo(umo: str) -> str:
    """Old, lossy filename token; use only to locate preserved legacy files."""
    token = _SAFE_TOKEN.sub("_", str(umo or "unknown")).strip("._")
    return (token or "unknown")[:120]


def read_umo_json(data_dir: Path, prefix: str, umo: str) -> dict[str, Any]:
    """Load only records whose stored owner matches the requested session.

    Pre-identity legacy files cannot be assigned safely: even an unchanged token
    such as ``room`` can also originate from ``:room``. Leave those files intact
    for explicit recovery instead of copying another session's memories.
    """
    path = data_dir / f"{prefix}_{safe_umo(umo)}.json"
    try:
        if not path.exists():
            path = data_dir / f"{prefix}_{legacy_safe_umo(umo)}.json"
        if not path.is_file() or path.is_symlink():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Unable to load session state from %s (%s); using empty state", path, type(exc).__name__)
        return {}
    if isinstance(raw, dict) and raw.get("umo") == str(umo or ""):
        return raw
    return {}


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
