"""Refresh only this plugin's modules when a scoped source update is loaded."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def ensure_current(package: str, main_module: str, entry_path: str, checks=(), version=None):
    """Drop stale owned imports before the main module imports its dependencies.

    Args:
        package: The plugin's exact import package.
        main_module: Currently executing entry module, retained throughout.
        entry_path: Current plugin main file.
        checks: Existing module/class/field signatures that distinguish old code.
        version: Optional package version used by its own main registration.
    """
    parent = sys.modules.get(package)
    if parent is None or not package:
        return
    root = Path(entry_path).resolve().parent
    digest = hashlib.sha256()
    excluded = {"tests", "data", ".git", "node_modules", ".venv", "__pycache__"}
    try:
        sources = sorted(
            root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()
        )
    except OSError:
        # An unreadable source tree must not break plugin import. Leave the
        # recorded fingerprint and the loaded modules untouched instead of
        # degrading to an empty digest (which would silently disable change
        # detection, or trigger a bogus purge).
        return
    for path in sources:
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        try:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
        except OSError:
            # A partially readable tree yields a bogus fingerprint; abandon the
            # comparison rather than purging modules based on it.
            return
    stamp = digest.hexdigest()
    previous = getattr(parent, "_narrative_loaded_source_fingerprint", None)
    incompatible = False
    for module_name, class_name, attribute in checks:
        module = sys.modules.get(package + "." + module_name)
        candidate = getattr(module, class_name, None)
        if candidate is not None and not (hasattr(candidate, attribute)
                or attribute in getattr(candidate, "__dataclass_fields__", {})
                or attribute in getattr(candidate, "model_fields", {})):
            incompatible = True
    if incompatible or previous is not None and previous != stamp:
        prefix = package + "."
        keep = {main_module, __name__}
        for name in tuple(sys.modules):
            if not name.startswith(prefix) or name in keep:
                continue
            module = sys.modules.pop(name)
            owner_name, attribute = name.rsplit(".", 1)
            owner = sys.modules.get(owner_name)
            if owner is not None and getattr(owner, attribute, None) is module:
                delattr(owner, attribute)
    parent._narrative_loaded_source_fingerprint = stamp
    if version is not None:
        parent.__version__ = version
