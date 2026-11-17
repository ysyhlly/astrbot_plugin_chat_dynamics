from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.core.integrations.catalog import companion_snapshot, find_companion


def provider(name="custom_world"):
    manifest={"schema_version":1,"enabled":True,"ready":True,"capabilities":{"character":1,"life":1}}
    plugin=NS(get_dynamics_capabilities=lambda:manifest,get_character_context=lambda:None,get_snapshot=lambda:None,
              ingest_events=lambda:None,get_intent=lambda:None)
    entry=NS(name=name,activated=True,star_cls=plugin,version="v0.1")
    return entry,manifest


def test_one_native_provider_owns_both_roles_and_reload_is_immediate():
    entry,manifest=provider();entries=[entry];context=NS(get_all_stars=lambda:entries)
    for role in ("worldbook","dynamic_life_state"):
        assert find_companion(context,role) is entry.star_cls
    rows=companion_snapshot(context,{"narrative_enabled":True})["companions"]
    assert [r["plugin"] for r in rows if r["id"] in ("worldbook","dynamic_life_state")] == ["custom_world"]*2
    replacement,_=provider();entries[:]=[replacement]
    assert find_companion(context,"worldbook") is replacement.star_cls
    replacement.activated=False
    assert find_companion(context,"worldbook") is None


def test_legacy_keeps_working_until_native_enabled_and_conflicts_are_visible():
    entry,manifest=provider();legacy=NS(name="astrbot_plugin_worldbook",activated=True,star_cls=object())
    context=NS(get_all_stars=lambda:[entry,legacy]);manifest["enabled"]=False
    assert find_companion(context,"worldbook") is legacy.star_cls
    manifest["enabled"]=True
    assert find_companion(context,"worldbook") is None
    row=next(r for r in companion_snapshot(context)["companions"] if r["id"]=="worldbook")
    assert row["state"] == "conflict" and len(row["providers"]) == 2
    legacy.activated=False
    assert find_companion(context,"worldbook") is entry.star_cls


@pytest.mark.parametrize("change",[{"ready":False},{"schema_version":2},{"schema_version":True},{"enabled":"true"},{"capabilities":{"character":True}}])
def test_invalid_or_unready_declarations_are_not_selected(change):
    entry,manifest=provider();manifest.update(change)
    assert find_companion(NS(get_all_stars=lambda:[entry]),"worldbook") is None


def test_mapping_registry_and_duplicate_entries_do_not_create_false_conflict():
    entry,_=provider();context=NS(get_all_stars=lambda:{"a":entry,"b":entry})
    assert find_companion(context,"worldbook") is entry.star_cls


def test_hot_reload_drops_old_catalog_even_if_parent_lost_its_fingerprint(tmp_path, monkeypatch):
    import sys
    from types import ModuleType
    from astrbot_plugin_chat_dynamics.reload_bootstrap import ensure_current
    package="world_reload_regression"
    parent=ModuleType(package)
    registry=ModuleType(package+".core.integrations.registry")
    registry.IntegrationRegistry=type("LegacyRegistry",(),{"_ownership_catalog_v1":True})
    catalog=ModuleType(package+".core.integrations.catalog")
    monkeypatch.setitem(sys.modules,package,parent)
    monkeypatch.setitem(sys.modules,registry.__name__,registry)
    monkeypatch.setitem(sys.modules,catalog.__name__,catalog)
    entry=tmp_path/"main.py";entry.write_text("# replacement entrypoint\n")
    ensure_current(package,package+".main",str(entry),checks=[
        ("core.integrations.registry","IntegrationRegistry","_dynamics_world_capabilities_v1")])
    assert registry.__name__ not in sys.modules
    assert catalog.__name__ not in sys.modules
    assert sys.modules[package] is parent
