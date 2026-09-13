"""Serializable capability discovery, distinct from a successful invocation."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Capability:
    name: str
    provider: str
    detected: bool = False
    ready: bool = False
    selected: bool = False
    detail: str = "not_detected"
    source: str = "discovery"

    def snapshot(self) -> dict:
        return asdict(self)


def provider_kind(name: str) -> str:
    normalized = "".join(char for char in str(name).casefold() if char.isalnum())
    if "livingmemory" in normalized or "liveingmemory" in normalized:
        return "livingmemory"
    if "selflearning" in normalized:
        return "selflearning"
    return "unknown"
