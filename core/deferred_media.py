"""Own local attachments while an event waits outside the host pipeline."""
from __future__ import annotations

import copy
import shutil
import tempfile
import weakref
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from .platform_bridge import iter_message_components


def preserve_event_media(event, *, archive=None, session=None):
    """Clone only local media; never transfer ownership of the host's files."""
    directory = None
    try:
        def clone_component(component):
            nonlocal directory
            cloned = copy.copy(component)
            # Pydantic v1's shallow copy shares __dict__; assigning a field
            # otherwise rewrites the host component's original local path.
            if hasattr(cloned, '__dict__'):
                object.__setattr__(cloned, '__dict__', dict(cloned.__dict__))
            # Quoted messages may contain attachments as well.
            chain = getattr(component, "chain", None)
            if isinstance(chain, list):
                cloned.chain = [clone_component(part) for part in chain]
            if type(component).__name__.lower() not in {"record", "image", "video", "file"}:
                return cloned
            copies = {}
            fields = ("file_", "path", "url") if hasattr(component, "file_") else ("file", "path", "url")
            for field in fields:
                raw = getattr(component, field, None)
                if not isinstance(raw, str) or not raw:
                    continue
                parsed = urlparse(raw)
                if parsed.scheme and parsed.scheme != "file" and not Path(raw).is_absolute():
                    continue
                uri_path = parsed.path if parsed.netloc in {'', 'localhost'} else '//' + parsed.netloc + parsed.path
                path = Path(url2pathname(uri_path) if parsed.scheme == "file" else raw)
                if not path.is_file():
                    continue
                key = str(path.resolve())
                if key not in copies:
                    directory = directory or (archive.create_directory(session) if archive is not None
                                              else tempfile.mkdtemp(prefix="chat-dynamics-media-"))
                    target = Path(directory) / f"{len(list(Path(directory).iterdir()))}{path.suffix}"
                    if archive is not None:
                        archive.reserve(path.stat().st_size)
                    shutil.copyfile(path, target)
                    copies[key] = target
                target = copies[key]
                setattr(cloned, field, target.as_uri() if parsed.scheme == "file" else str(target))
            return cloned

        components = [clone_component(part) for part in iter_message_components(event)]
        if directory is None:
            return event
        owned = copy.copy(event)
        owned.message_obj = copy.copy(event.message_obj)
        owned.message_obj.message = components
        if hasattr(event, "_extras"):
            owned._extras = dict(event._extras)
        if hasattr(event, "_temporary_local_files"):
            owned._temporary_local_files = []
        # Keep adapter delivery bound to the original event, including mocks
        # and adapters which keep mutable send receipts on the event itself.
        owned.send = event.send
        owned._chat_dynamics_media_directory = str(directory)
        owned._chat_dynamics_media_archive = archive
        owned._chat_dynamics_media_cleanup = (weakref.finalize(owned, archive.release, directory) if archive is not None
                                              else weakref.finalize(owned, shutil.rmtree, directory, ignore_errors=True))
        return owned
    except BaseException:
        if directory:
            if archive is not None:
                archive.release(directory)
            else:
                shutil.rmtree(directory, ignore_errors=True)
        raise


def release_event_media(event):
    cleanup = getattr(event, "_chat_dynamics_media_cleanup", None)
    if callable(cleanup):
        cleanup()


def without_event_media(event):
    """An owned text-only fallback must not let the host re-read attachments."""
    if event is None:
        return None
    owned = copy.copy(event)
    owned.send = event.send
    owned.message_obj = copy.copy(event.message_obj)
    owned.message_obj.message = [component for component in iter_message_components(event)
                                 if type(component).__name__.lower() in
                                 {"plain", "text", "at", "atall", "markdown", "mention"}]
    if hasattr(event, "_extras"):
        owned._extras = dict(event._extras)
        owned._extras.pop("provider_request", None)
    owned._chat_dynamics_media_understand = False
    return owned
