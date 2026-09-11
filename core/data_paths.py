"""Stable host-owned storage with non-destructive legacy file preservation."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("astrbot_plugin_chat_dynamics.storage")
PLUGIN_NAME = "astrbot_plugin_chat_dynamics"


def _copy_missing(source: Path, target: Path) -> None:
    """Publish a complete copy atomically, never replace an existing target."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as original:
                shutil.copyfileobj(original, stream)
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def resolve_data_root(legacy_dir: Path) -> Path:
    """Use AstrBot's persistent plugin directory; preserve old memory files.

    The fallback supports offline unit doubles and old SDKs lacking StarTools.
    Real host directory failures propagate instead of silently changing storage.
    """
    legacy_dir = Path(legacy_dir)
    try:
        from astrbot.api.star import StarTools
    except ImportError:
        legacy_dir.mkdir(parents=True, exist_ok=True)
        return legacy_dir
    getter = getattr(StarTools, "get_data_dir", None)
    if not callable(getter):
        legacy_dir.mkdir(parents=True, exist_ok=True)
        return legacy_dir
    target = Path(getter(PLUGIN_NAME)).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if legacy_dir.resolve() == target or not legacy_dir.is_dir():
        return target
    for source in legacy_dir.iterdir():
        if (source.suffix != ".json" or not source.name.startswith(("mood_", "notebook_"))
                or source.is_symlink() or not source.is_file()):
            continue
        destination = target / source.name
        if destination.exists():
            continue
        try:
            _copy_missing(source, destination)
        except OSError as exc:
            logger.warning("Legacy memory copy deferred type=%s", type(exc).__name__)
    return target
