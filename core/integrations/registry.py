"""One discovery owner and native-first integration routing for companion plugins."""
from __future__ import annotations

import os
from typing import Any

from .capabilities import Capability, provider_kind
from .legacy.selflearning_legacy import SelfLearningBridge as LegacySelfLearningBridge
from .legacy.selflearning_legacy import SelfLearningStatus
from .livingmemory import discover_livingmemory
from .selflearning import SelfLearningHubClient
from .semantic_provider import resolve_embedding_provider


class CapabilityRegistry:
    """Discover once at the boundary; business logic consumes named capabilities."""

    def __init__(self, context: Any = None):
        self.context = context
        self.discovery_errors: list[str] = []
        self.entries: tuple[Capability, ...] = ()

    def discover(self):
        # All old host registry shapes remain quarantined in the legacy adapter.
        catalog = LegacySelfLearningBridge(self.context)
        plugins = list(catalog._find_plugins())
        self.discovery_errors = list(catalog._discovery_errors)
        return plugins

    def update(self, providers, hub, *, enabled=True, embedding_id=""):
        entries = []
        for provider in providers:
            name, plugin = provider["name"], provider["plugin"]
            kind = provider_kind(name)
            if kind == "livingmemory":
                entries.extend(discover_livingmemory(name, plugin, ready=provider["ready"]))
            elif kind == "selflearning":
                native = bool(provider["hooks"])
                entries.append(Capability("selflearning.native_hook", name, native,
                    native and provider["ready"], native, "host_hook_owned" if native else "not_detected"))
                legacy = any(provider["capabilities"].values())
                entries.append(Capability("selflearning.legacy_python", name, legacy,
                    legacy and provider["ready"], False, "standby" if legacy else "not_detected"))
        for kind, keys in {
            "selflearning": ("native_hook", "legacy_python"),
            "livingmemory": ("native_recall", "search", "public_api", "embedding_api"),
        }.items():
            for key in keys:
                identifier = f"{kind}.{key}"
                if not any(item.name == identifier for item in entries):
                    entries.append(Capability(identifier, kind))
        entries.append(Capability("selflearning.hub_v1", "SelfLearning Hub v1",
            bool(hub.get("configured")), bool(hub.get("available")), False,
            str(hub.get("detail") or hub.get("status") or "not_configured"), "hub_v1_manifest_status"))
        host = resolve_embedding_provider(self.context, embedding_id)
        entries.append(Capability("host.embedding_provider", "AstrBot", host is not None,
            host is not None, host is not None, "host_provider" if host is not None else "not_configured"))
        if not enabled:
            from dataclasses import replace
            entries = [replace(item, selected=False) for item in entries]
        self.entries = tuple(entries)

    def snapshot(self):
        return [item.snapshot() for item in self.entries]


class IntegrationRegistry(LegacySelfLearningBridge):
    """Native hooks own normal requests. Legacy APIs are explicit compatibility only.

    Inheriting the quarantined adapter preserves notebook/admin compatibility;
    none of its probing calls are used for normal model context injection.
    """

    def __init__(self, context=None, *, enabled=True, hub_url="", hub_key_env="SELFLEARNING_HUB_API_KEY"):
        self.capability_registry = CapabilityRegistry(context)
        self.hub_key_env = hub_key_env
        self._hub_url = hub_url
        self.hub = SelfLearningHubClient(hub_url if enabled else "", os.environ.get(hub_key_env, ""))
        self.embedding_id = ""
        super().__init__(context, enabled=enabled)

    def _find_plugins(self):
        self.capability_registry.context = self.context
        providers = self.capability_registry.discover()
        self._discovery_errors = list(self.capability_registry.discovery_errors)
        return iter(providers)

    def refresh(self):
        result = super().refresh()
        self.capability_registry.update(self._providers, self.hub.snapshot(),
                                       enabled=self.enabled, embedding_id=self.embedding_id)
        return result

    def configure(self, *, enabled, context=None, hub_url=None, hub_key_env=None, embedding_id=None):
        if hub_key_env is not None:
            self.hub_key_env = hub_key_env
        if hub_url is not None:
            self._hub_url = hub_url
        if context is not None and context is not self.context:
            self.hub.configure("", "")
        self.hub.configure(self._hub_url if enabled else "", os.environ.get(self.hub_key_env, ""))
        if embedding_id is not None:
            self.embedding_id = embedding_id
        super().configure(enabled=enabled, context=context)

    async def discover(self):
        self.refresh()
        if self.enabled:
            await self.hub.discover()
        self.refresh()

    def snapshot(self):
        result = super().snapshot()
        hub = self.hub.snapshot()
        result.update(capability_registry=self.capability_registry.snapshot(), hub=hub,
                      injection_policy="native_hooks_first", legacy_mode="standby")
        if self.enabled and hub.get("available") and self.status == SelfLearningStatus.MISSING:
            result.update(status="connected", lamp="Hub 可用", detail="hub_v1_available")
        return result

    async def model_context(self, *, umo, peer_id, allow_memories=True):
        """Compatibility entrypoint: normal requests never duplicate companion context."""
        return {}

    async def context_for_request(self, *, event, query: str, native_hooks: bool) -> dict:
        if native_hooks or not self.enabled or event is None:
            return {}
        from ..platform_bridge import parse_group_event
        parsed = parse_group_event(event)
        if not parsed.group_id or not parsed.unified_msg_origin:
            return {}
        generation = self._generation
        data = await self.hub.context(group_id=parsed.group_id, user_id=parsed.sender_id, query=query)
        if generation != self._generation or not self.enabled:
            return {}
        return data

    async def close(self):
        await super().close()
        await self.hub.close()
