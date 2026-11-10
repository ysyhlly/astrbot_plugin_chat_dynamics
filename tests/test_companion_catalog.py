"""Capability inspection never calls partners or retains reloaded instances."""

from types import SimpleNamespace as NS
import gc
import weakref

from astrbot_plugin_chat_dynamics.core.integrations.catalog import companion_snapshot, find_companion
from astrbot_plugin_chat_dynamics.core.integrations.registry import IntegrationRegistry


def metadata(name, instance, **extra):
    return NS(name="astrbot_plugin_" + name, star_cls=instance, activated=True, version="1.0", **extra)


def test_reload_disable_and_uninstall_are_visible_without_a_ttl():
    class Partner:
        pass

    first = Partner()
    old = weakref.ref(first)
    meta = metadata("livingmemory", first)
    entries = [meta]
    registry = IntegrationRegistry(NS(get_all_stars=lambda: entries))
    assert registry.narrative_provider("livingmemory") is first
    second = Partner()
    meta.star_cls = second
    del first
    gc.collect()
    assert old() is None
    assert registry.narrative_provider("livingmemory") is second
    meta.activated = False
    assert registry.narrative_provider("livingmemory") is None
    entries.clear()
    assert registry.narrative_provider("livingmemory") is None


def test_snapshot_only_checks_interfaces_and_does_not_call_or_read_config():
    class Partner:
        def get_evidence_view(self, *args):
            raise AssertionError("read-only inspection must not recall")

        def ingest_events(self, *args):
            raise AssertionError("inspection must not ingest")

        @property
        def config(self):
            raise AssertionError("companion settings are not ours")

    context = NS(get_all_stars=lambda: [metadata("livingmemory", Partner())])
    result = companion_snapshot(context)
    memory = next(row for row in result["companions"] if row["id"] == "livingmemory")
    assert memory["state"] == "ready" and not memory["missing_methods"]
    assert result["blocking"] == []
    assert not any("config" == key for row in result["companions"] for key in row)


def test_required_boundary_is_distinct_from_optional_missing_plugins():
    result = companion_snapshot(NS(get_all_stars=lambda: []), {"autonomous_blocking_required": True})
    assert result["blocking"] == ["autonomous_blocking"]
    assert len(result["companions"]) == 8
    assert companion_snapshot(NS(get_all_stars=lambda: []))["blocking"] == []


def test_disabled_or_loading_companion_is_not_advertised_ready():
    disabled = metadata("livingmemory", NS(get_evidence_view=lambda: None, ingest_events=lambda: None))
    disabled.activated = False
    loading = metadata("self_learning", NS(initializer=NS(is_initialized=False), get_social_context=lambda: None))
    result = companion_snapshot(NS(get_all_stars=lambda: [disabled, loading]))
    rows = {row["id"]: row for row in result["companions"]}
    assert rows["livingmemory"]["state"] == "disabled"
    assert rows["self_learning"]["state"] == "loading"


def test_mapping_registry_and_exact_plugin_identity_are_supported():
    target = NS()
    context = NS(get_all_stars=lambda: {"data.plugins.astrbot_plugin_portrayal.main": {"plugin": target}})
    assert find_companion(context, "portrayal") is target
    assert find_companion(NS(get_all_stars=lambda: [metadata("portrayal_fake", target)]), "portrayal") is None


def test_narrative_dependencies_report_only_when_narrative_is_selected():
    context = NS(get_all_stars=lambda: [])
    assert companion_snapshot(context, {"narrative_enabled": True})["blocking"] == ["worldbook", "dynamic_life_state"]


def test_all_current_visible_settings_have_one_named_category():
    from pathlib import Path
    import json
    root = Path(__file__).resolve().parents[1]
    script = (root / 'pages/config/app.js').read_text(encoding='utf-8')
    raw = script.split('const CONFIG_GROUPS = ', 1)[1].split(';\n', 1)[0]
    groups = json.loads(raw)
    keys = [key for group in groups for key in group['keys']]
    assert len(keys) == len(set(keys))
    schema = json.loads((root / '_conf_schema.json').read_text(encoding='utf-8'))
    visible = {key for key, entry in schema.items() if not entry.get('invisible')}
    assert visible <= set(keys)
