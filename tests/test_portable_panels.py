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


@pytest.mark.asyncio
async def test_workspace_supports_delete_passthrough_and_csrf_guard(monkeypatch):
    import sys
    import types

    from astrbot_plugin_chat_dynamics import series_panels

    identity = "astrbot_plugin_chat_dynamics"
    rows = []
    registered = []
    context = SimpleNamespace(
        registered_web_apis=registered,
        get_all_stars=lambda: rows,
        get_config=lambda: {"dashboard": {"username": "admin"}},
        register_web_api=lambda *row: registered.append(row),
    )
    owner = SimpleNamespace(context=context)
    panel = owner._series_panels = SimpleNamespace(owner=owner, identity=identity)
    view = panel.workspace = workspace.PanelWorkspace(panel)
    rows.append(SimpleNamespace(name=identity, activated=True, star_cls=owner))
    view.register()

    panel_route = next(
        row for row in registered if row[0].endswith("/series_workspace/panel/<target>/<path:operation>")
    )
    assert "DELETE" in panel_route[2]
    assert "PUT" in panel_route[2]
    assert "PATCH" in panel_route[2]

    deleted = []

    async def delete_handler():
        deleted.append("ok")
        return {"status": "ok", "status_code": 200}

    workspace.register_panel_api(
        owner,
        context.register_web_api,
        f"/{identity}/series_templates",
        delete_handler,
        ["DELETE"],
        "Delete template",
    )

    dash = sys.modules.get("astrbot.dashboard") or types.ModuleType("astrbot.dashboard")
    dash_api = sys.modules.get("astrbot.dashboard.api") or types.ModuleType("astrbot.dashboard.api")
    dash_auth = types.ModuleType("astrbot.dashboard.api.auth")

    async def fake_require_user(_raw):
        return "admin"

    dash_auth.require_dashboard_user = fake_require_user
    monkeypatch.setitem(sys.modules, "astrbot.dashboard", dash)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api", dash_api)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.api.auth", dash_auth)
    monkeypatch.setattr(
        series_panels,
        "json_response",
        lambda data, status_code=200: {"status_code": status_code, **data},
    )

    # Cross-site DELETE rejected with 403
    monkeypatch.setattr(
        series_panels,
        "request",
        SimpleNamespace(
            _request=object(),
            username="admin",
            method="DELETE",
            headers={"Host": "dashboard.local", "Origin": "https://evil.local"},
            cookies={},
        ),
    )
    denied = await view.call(identity, "series_templates")
    assert denied["status_code"] == 403
    assert deleted == []

    # Missing CSRF headers on DELETE rejected with 403
    monkeypatch.setattr(
        series_panels,
        "request",
        SimpleNamespace(
            _request=object(),
            username="admin",
            method="DELETE",
            headers={"Host": "dashboard.local"},
            cookies={},
        ),
    )
    denied_stripped = await view.call(identity, "series_templates")
    assert denied_stripped["status_code"] == 403
    assert deleted == []

    # Same-origin admin DELETE succeeds and hits target handler
    monkeypatch.setattr(
        series_panels,
        "request",
        SimpleNamespace(
            _request=object(),
            username="admin",
            method="DELETE",
            headers={
                "Host": "dashboard.local",
                "Origin": "https://dashboard.local",
                "Sec-Fetch-Site": "same-origin",
            },
            cookies={},
        ),
    )
    allowed = await view.call(identity, "series_templates")
    assert allowed["status_code"] == 200
    assert deleted == ["ok"]
