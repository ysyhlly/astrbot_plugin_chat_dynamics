"""Load existing native models after adapter registration or plugin reload."""
import asyncio

from astrbot.api import logger

from . import provider


class SystemOneService:
    def __init__(self, host):
        self.host = host
        self._lock = asyncio.Lock()

    async def refresh_providers(self):
        async with self._lock:
            provider.register_adapter()
            manager = getattr(self.host.context, "provider_manager", None)
            if manager is None:
                return
            for config in list(manager.providers_config):
                try:
                    merged = manager.get_merged_provider_config(config)
                    if merged.get("type") != provider.PROVIDER_TYPE or not merged.get("enable"):
                        continue
                    current = manager.inst_map.get(merged["id"])
                    if current is None:
                        await manager.load_provider(config)
                    elif getattr(current, "is_systemone_provider", False) and type(current) is not provider.SystemOneProvider:
                        await manager.reload(config)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # One broken model must not prevent @ replies or the other
                    # configured models from loading. Never log its credentials.
                    logger.warning("[ChatDynamics] Jev provider refresh failed type=%s", type(exc).__name__)
