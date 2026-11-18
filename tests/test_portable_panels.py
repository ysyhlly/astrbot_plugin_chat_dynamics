"""The workspace routes only to operations offered by the currently active panel."""

from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics import series_workspace as workspace


@pytest.mark.asyncio
async def test_workspace_uses_target_registration_and_keeps_its_guard(monkeypatch):
    identity = "astrbot_plugin_chat_dynamics"
    context = SimpleNamespace(registered_web_apis=[])
    owner = SimpleNamespace(context=context)
    panel = SimpleNamespace(owner=owner, identity=identity)
    view = workspace.PanelWorkspace(panel)
    monkeypatch.setattr(workspace, "_error", lambda message, code: code)

    def register(*row):
        context.registered_web_apis.append(row)

    calls = []

    async def guarded(item):
        calls.append(item)
        return 403

    route = f"/{identity}/examples/<item>"
    workspace.register_panel_api(owner, register, route, guarded, ["GET"], "Example")
    assert await view.run("GET", "examples/first") == 403
    assert calls == ["first"]
    assert await view.run("POST", "examples/first") == 404
    assert await view.run("GET", "other") == 404
    context.registered_web_apis.clear()
    assert await view.run("GET", "examples/second") == 409
    assert calls == ["first"]


def test_workspace_requires_an_enabled_participating_companion():
    identity = "astrbot_plugin_dynamics_memory"
    rows = []
    context = SimpleNamespace(get_all_stars=lambda: rows)
    owner = SimpleNamespace(context=context)
    panel = owner._series_panels = SimpleNamespace(owner=owner, identity=identity)
    panel.workspace = workspace.PanelWorkspace(panel)
    row = SimpleNamespace(name=identity, activated=True, star_cls=owner)
    rows.append(row)
    assert panel.workspace._target(identity) is panel.workspace
    assert panel.workspace._target("external_plugin") is None
    row.activated = False
    assert panel.workspace._target(identity) is None
    row.activated = True
    rows.append(SimpleNamespace(name=identity, activated=True))
    assert panel.workspace._target(identity) is None
    rows.pop()
    panel.workspace.closed = True
    assert panel.workspace._target(identity) is None
