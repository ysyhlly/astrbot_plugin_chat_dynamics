"""Model discovery for our native decision providers on current AstrBot."""
from functools import wraps

from astrbot.core.provider.register import provider_cls_map
from astrbot.core.utils.llm_metadata import LLM_METADATAS
from astrbot.dashboard.services.config_service import ProviderConfigService

OWNED_TYPES = frozenset({'systemone_catalog','openai_decisions'})


def install():
    current = ProviderConfigService.list_provider_source_models
    original = getattr(current,'_cd_decision_catalog_original',current)

    @wraps(original)
    async def list_models(service, source_id):
        source = service._find_provider_source(source_id)
        if not source or source.get('type') not in OWNED_TYPES:
            return await original(service,source_id)
        meta = provider_cls_map.get(source['type'])
        adapter = meta.cls_type if meta else None
        if adapter is None or not getattr(adapter,'is_chat_dynamics_builtin',False):
            return await original(service,source_id)
        instance = adapter(source,{})
        try:
            models = await instance.get_models() or []
            return {'models':models,'provider_source_id':source_id,
                    'model_metadata':{name:LLM_METADATAS[name] for name in models if name in LLM_METADATAS}}
        finally:
            await instance.terminate()

    list_models._cd_decision_catalog_original = original
    ProviderConfigService.list_provider_source_models = list_models
    return list_models


def uninstall(wrapper):
    if ProviderConfigService.list_provider_source_models is wrapper:
        ProviderConfigService.list_provider_source_models = wrapper._cd_decision_catalog_original
