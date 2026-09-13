"""AstrBot embedding provider selection; no companion-internal vector APIs."""
from typing import Any


def provider_id(provider: Any) -> str:
    if provider is None:
        return ""
    meta = getattr(provider, "meta", None)
    if meta is not None:
        for attr in ("id", "provider_id"):
            value = getattr(meta, attr, None)
            if value:
                return str(value)
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict) and config.get("id"):
        return str(config["id"])
    for attr in ("id", "provider_id"):
        value = getattr(provider, attr, None)
        if value:
            return str(value)
    return ""


def resolve_embedding_provider(context: Any, configured_id: str = "") -> Any:
    if context is None:
        return None
    if configured_id:
        getter = getattr(context, "get_provider_by_id", None)
        if callable(getter):
            try:
                found = getter(configured_id)
            except Exception:
                found = None
            if found is not None:
                return found
    listing = getattr(context, "get_all_embedding_providers", None)
    if not callable(listing):
        return None
    try:
        providers = list(listing() or [])
    except Exception:
        return None
    if configured_id:
        return next((provider for provider in providers if provider_id(provider) == configured_id), None)
    return providers[0] if providers else None
