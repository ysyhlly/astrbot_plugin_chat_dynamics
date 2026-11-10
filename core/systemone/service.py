"""Load existing native models after adapter registration or plugin reload."""
import asyncio
from importlib import import_module
import logging

catalog_bridge = import_module('.catalog_bridge', __package__)

logger = logging.getLogger("astrbot_plugin_chat_dynamics.systemone")


class SystemOneService:
    native_decision_refresh = True

    def __init__(self, host):
        self.host = host
        self._lock = asyncio.Lock()
        self._catalog_wrapper = catalog_bridge.install()

    def close(self):
        catalog_bridge.uninstall(self._catalog_wrapper)

    async def refresh_providers(self):
        async with self._lock:
            # Package attributes can outlive sys.modules during a hot reload.
            # Resolve the actual modules, rather than "from . import provider".
            provider = import_module('.provider', __package__)
            decisions = import_module('.decisions', __package__)
            provider.register_adapter()
            decisions.register_adapter()
            adapters = {provider.PROVIDER_TYPE: provider.SystemOneProvider,
                        decisions.PROVIDER_TYPE: decisions.OpenAIDecisionsProvider}
            manager = getattr(self.host.context, "provider_manager", None)
            if manager is None:
                return
            for config in list(manager.providers_config):
                try:
                    merged = manager.get_merged_provider_config(config)
                    adapter = adapters.get(merged.get("type"))
                    if adapter is None or not merged.get("enable"):
                        continue
                    current = manager.inst_map.get(merged["id"])
                    if current is None:
                        await manager.load_provider(config)
                    elif getattr(current, "is_systemone_provider", False) and type(current) is not adapter:
                        await manager.reload(config)
                    loaded = manager.inst_map.get(merged['id'])
                    if isinstance(loaded,provider.DecisionProvider):
                        # A class migration can leave old closed chat instances
                        # behind when the host's old isinstance guard misses them.
                        chat = getattr(manager,'provider_insts',None)
                        if isinstance(chat,list):
                            stale = [p for p in chat if getattr(p,'provider_config',{}).get('id') == merged['id']]
                            chat[:] = [p for p in chat if p not in stale]
                            if getattr(manager,'curr_provider_inst',None) in stale:
                                manager.curr_provider_inst = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # One broken model must not prevent @ replies or the other
                    # configured models from loading. Never log its credentials.
                    logger.warning("[ChatDynamics] Jev provider refresh failed type=%s", type(exc).__name__)
