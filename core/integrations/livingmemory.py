"""Read-only public surface discovery. No recall/search is executed by CD."""
from .capabilities import Capability


def discover_livingmemory(name, plugin, *, ready=False) -> list[Capability]:
    native = callable(getattr(plugin, "handle_memory_recall", None))
    # The upstream search method is an admin command, not a callable retrieval API.
    search_command = callable(getattr(plugin, "search", None))
    page_api = getattr(plugin, "page_api", None) is not None
    return [
        Capability("livingmemory.native_recall", name, native, native and ready, native,
                   "host_hook_owned" if native else "not_detected"),
        Capability("livingmemory.search", name, search_command, False, False,
                   "admin_command_only" if search_command else "not_detected"),
        Capability("livingmemory.public_api", name, page_api, page_api and ready, False,
                   "dashboard_api_only" if page_api else "not_detected"),
        Capability("livingmemory.embedding_api", name, False, False, False, "no_verified_contract"),
    ]
