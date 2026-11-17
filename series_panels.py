"""Read-only companion presence. Navigation is handled by the host bridge."""

from __future__ import annotations

import inspect
from pathlib import Path

from astrbot.api.web import json_response, request

CATALOG = (
    ("astrbot_plugin_chat_dynamics", "Chat Dynamics", "app", "对话调度与回复"),
    ("astrbot_plugin_dynamics_learning", "群间学习", "review", "学习与审查"),
    ("astrbot_plugin_dynamics_world", "群间世界", "world", "角色与生活状态"),
    ("astrbot_plugin_dynamics_memory", "群间记忆", "dashboard", "记忆与证据"),
    ("astrbot_plugin_dynamics_horizons", "群间见闻", "horizons", "信息源与兴趣见闻"),
    (
        "astrbot_plugin_dynamics_games",
        "群间游戏",
        "games",
        "共读、约局、接龙、挑战与共创",
    ),
    ("astrbot_plugin_visual_media", "视觉工坊", "workshop", "表情、图片与视觉表达"),
    (
        "astrbot_plugin_group_chronicle",
        "群史记",
        "culture",
        "收录有价值的结果与群史传记",
    ),
    ("astrbot_plugin_autonomous_blocking", "成员边界", None, "回复与资料边界"),
    (
        "astrbot_plugin_self_learning",
        "旧 SelfLearning",
        None,
        "兼容检查；已由群间学习接管",
    ),
)

# These four IDs and titles mirror Dynamics Games' runtime.LEGACY_PLUGINS.
LEGACY_CONFLICTS = (
    (
        "astrbot_plugin_self_learning",
        "旧 SelfLearning",
        "astrbot_plugin_dynamics_learning",
    ),
    ("astrbot_plugin_truth_game", "真心话", "astrbot_plugin_dynamics_games"),
    ("astrbot_plugin_blackjack21", "21点", "astrbot_plugin_dynamics_games"),
    ("astrbot_plugin_rollpig", "今日小猪", "astrbot_plugin_dynamics_games"),
    ("astrbot_plugin_what_to_eat", "是啊，吃什么", "astrbot_plugin_dynamics_games"),
)
LEGACY_GAME_IDS = frozenset(
    identity
    for identity, _label, owner in LEGACY_CONFLICTS
    if owner == "astrbot_plugin_dynamics_games"
)


def value(row, key, default=None):
    return (
        row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)
    )


def registered_names(key, row):
    names = set()
    for item in (
        key,
        value(row, "name"),
        value(row, "root_dir_name"),
        value(row, "module_path"),
    ):
        if isinstance(item, str):
            names.update(item.split("."))
    return names


def presence(context, current, root=None):
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    raw = context.get_all_stars()
    entries = (
        list(raw.items()) if isinstance(raw, dict) else [("", r) for r in raw or []]
    )
    registered_entries = [(registered_names(key, row), row) for key, row in entries]
    output = []
    for identity, label, page, role in CATALOG:
        matches = []
        for names, row in registered_entries:
            if identity in names and all(existing is not row for existing in matches):
                matches.append(row)
        installed = bool(matches) or (root / identity).is_dir()
        data = {
            "id": identity,
            "label": label,
            "role": role,
            "page": page,
            "association": identity == "astrbot_plugin_group_chronicle",
            "current": identity == current,
            "installed": installed,
            "activated": False,
            "loaded": False,
            "ready": None,
            "runtime_enabled": None,
            "version": "",
            "can_open": False,
        }
        if not matches:
            data["state"] = "unloaded" if installed else "missing"
        elif len(matches) > 1:
            data["state"] = "conflict"
        else:
            row = matches[0]
            data["activated"] = value(row, "activated", False) is True
            data["version"] = str(value(row, "version", "") or "")[:128]
            owner = next(
                (
                    value(row, k)
                    for k in ("star_cls", "star", "plugin")
                    if value(row, k) is not None and not isinstance(value(row, k), type)
                ),
                None,
            )
            data["loaded"] = owner is not None
            data["can_open"] = bool(
                data["activated"]
                and data["loaded"]
                and page
                and any(
                    (root / identity / folder / page / "index.html").is_file()
                    for folder in ("pages", "views")
                )
            )
            if not data["activated"]:
                data["state"] = "disabled"
            elif not data["loaded"]:
                data["state"] = "unloaded"
            else:
                data["state"] = "enabled"
                getter = getattr(owner, "get_dynamics_capabilities", None)
                if callable(getter) and not inspect.iscoroutinefunction(getter):
                    try:
                        caps = getter()
                        if inspect.isawaitable(caps):
                            if inspect.iscoroutine(caps):
                                caps.close()
                        elif (
                            isinstance(caps, dict)
                            and type(caps.get("schema_version")) is int
                            and caps["schema_version"] == 1
                        ):
                            data["ready"] = (
                                caps.get("ready")
                                if type(caps.get("ready")) is bool
                                else None
                            )
                            data["runtime_enabled"] = (
                                caps.get("enabled")
                                if type(caps.get("enabled")) is bool
                                else None
                            )
                            data["state"] = (
                                "inactive"
                                if data["runtime_enabled"] is False
                                else "ready"
                                if data["ready"] is True
                                else "not_ready"
                                if data["ready"] is False
                                else "enabled"
                            )
                    except Exception as exc:
                        data.update(state="error", error=type(exc).__name__)
        output.append(data)

    active_ids = {
        identity
        for names, row in registered_entries
        if value(row, "activated", False) is True
        for identity in names
    }
    conflicts = [
        {"id": identity, "label": label, "owner": owner}
        for identity, label, owner in LEGACY_CONFLICTS
        if identity in active_ids and owner in active_ids
    ]

    return {
        "schema_version": 1,
        "current": current,
        "plugins": output,
        "legacy_conflict": bool(conflicts),
        "legacy_conflicts": conflicts,
    }


class SeriesPanels:
    def __init__(self, owner, identity):
        self.owner = owner
        self.identity = identity
        self.path = "/" + identity + "/series_presence"

    async def status(self):
        try:
            from astrbot.dashboard.api.auth import require_dashboard_user

            raw = getattr(request, "_request", None)
            if raw is None:
                raise PermissionError()
            username = await require_dashboard_user(raw)
            expected = (
                self.owner.context.get_config().get("dashboard", {}).get("username")
            )
            if not username or expected and username != expected:
                raise PermissionError()
            return json_response(
                {"status": "ok", "data": presence(self.owner.context, self.identity)}
            )
        except PermissionError:
            return json_response(
                {"status": "error", "message": "需要 Dashboard 管理员登录"},
                status_code=403,
            )
        except Exception:
            return json_response(
                {"status": "error", "message": "互通状态暂不可用"}, status_code=503
            )

    def close(self):
        registered = getattr(self.owner.context, "registered_web_apis", None)
        if isinstance(registered, list):
            registered[:] = [
                r
                for r in registered
                if not (
                    len(r) >= 2
                    and r[0] == self.path
                    and getattr(r[1], "__self__", None) is self
                )
            ]


def register_series_panels(owner, identity):
    old = getattr(owner, "_series_panels", None)
    if old:
        old.close()
    panel = SeriesPanels(owner, identity)
    owner._series_panels = panel
    owner.context.register_web_api(
        panel.path, panel.status, ["GET"], "群间系列插件状态"
    )
