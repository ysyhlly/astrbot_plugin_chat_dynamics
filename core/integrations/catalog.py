"""Companion ownership and read-only capability discovery.

Only AstrBot's current registry is authoritative. No companion is imported,
called, configured or retained by this catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Companion:
    key: str
    title: str
    role: str
    settings: str
    methods: tuple[str, ...] = ()
    config_group: str = "memory"


COMPANIONS = (
    Companion("dynamics_learning", "群间学习", "表达与社交学习、调整审查和回合反馈",
              "在群间学习中配置生效会话、分析/审查模型与审查方式；审批后的增量按会话和人格生效。",
              ("learning_capabilities", "ingest_dynamics_event", "get_learning_context")),
    Companion("dynamics_horizons", "群间见闻", "外部信息源阅读、暂存兴趣与待讨论问题",
              "在群间见闻中按角色配置订阅源、书架与双模型审查；接话与主动分享统一由群间裁决。",
              ("horizons_capabilities", "get_horizons_context", "ingest_dynamics_event", "list_due_intents")),
    Companion("autonomous_blocking", "自主屏蔽", "成员屏蔽、工具与发送边界",
              "在自主屏蔽插件中管理成员策略；此处只决定服务缺失时是否停止回复。",
              ("resolve_context", "peek_policy", "policy_snapshot", "filter_frames"), "boundaries"),
    Companion("worldbook", "世界书", "角色卡与人格资料",
              "在世界书中编辑角色卡和人格绑定；此处只选择持续生活的生效范围。",
              ("get_character_context",), "narrative"),
    Companion("dynamic_life_state", "动态生活", "生活状态、事件推进与主动联系动机",
              "在动态生活插件中配置叙事模式和人格范围；此处设置接入范围与联系许可。",
              ("get_snapshot", "ingest_events", "get_intent"), "narrative"),
    Companion("livingmemory", "LivingMemory", "长期记忆与可核对的来源证据",
              "在 LivingMemory 中设置记忆召回、模型和存储；Chat Dynamics 保留来源与会话校验。",
              ("get_evidence_view", "ingest_events")),
    Companion("self_learning", "SelfLearning", "关系状态、黑话与互动学习",
              "在 SelfLearning 中设置学习与关系规则；此处只控制是否接入。",
              ("get_social_context",)),
    Companion("portrayal", "用户画像", "成员画像与背景资料",
              "在用户画像插件中管理采集和画像；此处只读取当前成员获准的资料。",
              ("get_profiles",)),
    Companion("stealer", "视觉表达", "按聊天语境选择文字、表情或生图",
              "在视觉插件中管理图库、角色和生图供应商；Chat Dynamics 沿用回复时机、工具权限与发送校验。",
              ("start_joint_reply_prefetch",), "media"),
    Companion("dynamics_games", "群间游戏", "共读、约局、接龙、挑战与娱乐引擎",
              "在群间游戏中配置协作活动与群聊娱乐引擎；Chat Dynamics 负责接话时机、进度联动与边界校验。",
              ("get_dynamics_capabilities", "get_group_activity_context", "get_group_activity_detail"), "narrative"),
    Companion("group_chronicle", "群史记", "长期群史、人物传记与群文化沿革",
              "在群史记中管理历史收录与文化条目；Chat Dynamics 按需联动收录高价值协作成果。",
              ("get_dynamics_capabilities", "get_culture_context", "record_valuable_outcome"), "narrative"),
    Companion("memory_guard", "内存回收", "归还进程分配器中的空闲页",
              "独立运行，无需修改聊天模型或插件的业务缓存。", (), ""),
)


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def companion_entries(context: Any) -> list[tuple[str, Any]]:
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return []
    entries = getter()
    return list(entries.items()) if isinstance(entries, dict) else [("", item) for item in (entries or ())]


def _matches(key: str, item: Any, name: str) -> bool:
    expected = "astrbot_plugin_" + name
    names = (key, _value(item, "name"), _value(item, "root_dir_name"), _value(item, "module_path"))
    return any(value == expected or (isinstance(value, str) and expected in value.split(".")) for value in names)


def _instance(item: Any) -> Any:
    instance = _value(item, "star_cls") or _value(item, "star") or _value(item, "plugin")
    return instance if instance is not None and not isinstance(instance, type) else None


CAPABILITY_ROLES = {"worldbook": "character", "dynamic_life_state": "life", "livingmemory": "memory", "self_learning":"social", "stealer":"media"}


def _manifest(instance: Any) -> dict:
    """Only a synchronous, versioned, data-only declaration participates."""
    getter = getattr(instance, "get_dynamics_capabilities", None)
    if not callable(getter):
        return {}
    import inspect
    if inspect.iscoroutinefunction(getter):
        return {}
    try:
        result = getter()
    except Exception:
        return {}
    if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        return {}
    return result


def _role_candidates(entries, name, *, include_inactive: bool = False):
    role = CAPABILITY_ROLES.get(name)
    found = []
    seen = set()
    for key, item in entries:
        active = bool(_value(item, "activated", True))
        if not include_inactive and not active:
            continue
        instance = _instance(item)
        manifest = _manifest(instance) if (role and instance is not None) else {}
        capabilities = manifest.get("capabilities", {})
        role_declared = (isinstance(capabilities, dict) and type(capabilities.get(role)) is int
                         and capabilities.get(role) == 1)
        declared = role_declared and (include_inactive or manifest.get("enabled") is True)
        if _matches(key, item, name) or declared:
            marker = id(instance) if instance is not None else id(item)
            if marker not in seen:
                seen.add(marker)
                found.append((key, item, instance, manifest))
    return found


def _entry_disabled(item: Any, manifest: dict[str, Any] | None = None) -> bool:
    if not _value(item, "activated", True):
        return True
    if _value(item, "enabled", True) is False:
        return True
    return bool(manifest and manifest.get("enabled") is False)


def find_companion(context: Any, name: str) -> Any:
    """Resolve a fresh active instance; reload/disable takes effect immediately."""
    if name in CAPABILITY_ROLES:
        candidates = _role_candidates(companion_entries(context), name)
        if len(candidates) != 1:
            return None  # Never silently choose between two state owners.
        _, _, instance, manifest = candidates[0]
        if instance is None or (manifest and manifest.get("ready") is not True):
            return None
        return instance
    for key, item in companion_entries(context):
        if _matches(key, item, name) and _value(item, "activated", True):
            instance = _instance(item)
            if instance is not None:
                return instance
    return None


def companion_snapshot(context: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return ownership and interface presence, never business data or secrets."""
    config = config or {}
    entries = companion_entries(context)
    rows = []
    for spec in COMPANIONS:
        matches = [(key, item) for key, item in entries if _matches(key, item, spec.key)]
        item = next((item for _, item in matches if _value(item, "activated", True)), matches[0][1] if matches else None)
        instance = _instance(item) if item is not None else None
        manifest = _manifest(instance) if instance is not None else {}
        active = item is not None and not _entry_disabled(item, manifest)
        required = ((spec.key == "autonomous_blocking" and bool(config.get("autonomous_blocking_required")))
                    or (spec.key in {"worldbook", "dynamic_life_state"} and bool(config.get("narrative_enabled"))))
        missing = [method for method in spec.methods if not callable(getattr(instance, method, None))] if active and instance is not None else []
        initializer = getattr(instance, "initializer", None)
        loading = initializer is not None and getattr(initializer, "is_initialized", True) is False
        stopping = getattr(instance, "_terminating", False) is True or getattr(instance, "_shutting_down", False) is True
        state = ("missing" if item is None else "disabled" if not active else "loading" if instance is None or loading or stopping
                 else "loading" if manifest and manifest.get("ready") is not True
                 else "partial" if missing else "ready")
        selected_plugin = "astrbot_plugin_" + spec.key
        title, settings = spec.title, spec.settings
        providers = []
        if spec.key in CAPABILITY_ROLES:
            candidates = _role_candidates(entries, spec.key)
            providers = [str(_value(entry, "name", key) or key) for key, entry, _, _ in candidates]
            if len(candidates) > 1:
                state = "conflict"
            elif candidates:
                key, item, instance, manifest = candidates[0]
                selected_plugin = str(_value(item, "name", key) or key)
                if _entry_disabled(item, manifest) or instance is None:
                    missing = []
                    state = "loading" if _value(item, "activated", True) and manifest.get("enabled") is not False and instance is None else "disabled"
                else:
                    missing = [method for method in spec.methods if not callable(getattr(instance, method, None))]
                    state = "loading" if manifest and manifest.get("ready") is not True else "partial" if missing else "ready"
                display = manifest.get("display_name")
                if isinstance(display, str) and display.strip():
                    display = display.strip()[:80]
                    title = display + {"worldbook": " · 角色与世界", "dynamic_life_state": " · 持续生活", "livingmemory": " · 长期记忆", "self_learning": " · 社交与学习", "stealer": " · 视觉表达"}[spec.key]
                    settings = "在「" + display + "」中管理资料与状态；Chat Dynamics 负责接入范围和回复调度。"
            else:
                inactive_candidates = _role_candidates(entries, spec.key, include_inactive=True)
                if inactive_candidates:
                    key, item, instance, manifest = inactive_candidates[0]
                    selected_plugin = str(_value(item, "name", key) or key)
                    providers = [selected_plugin]
                    state = "disabled" if _entry_disabled(item, manifest) else "loading"
                    display = manifest.get("display_name")
                    if isinstance(display, str) and display.strip():
                        display = display.strip()[:80]
                        title = display + {"worldbook": " · 角色与世界", "dynamic_life_state": " · 持续生活", "livingmemory": " · 长期记忆", "self_learning": " · 社交与学习", "stealer": " · 视觉表达"}[spec.key]
                        settings = "在「" + display + "」中管理资料与状态；Chat Dynamics 负责接入范围和回复调度。"
        version = _value(item, "version", "") if item is not None else ""
        rows.append(dict(id=spec.key, name=title, plugin=selected_plugin, providers=providers,
                         version=version[:80] if isinstance(version, str) else "", state=state,
                         required=required, role=spec.role, settings=settings,
                         config_group=spec.config_group, missing_methods=missing))
    return dict(schema_version=1, companions=rows,
                blocking=[row["id"] for row in rows if row["required"] and row["state"] != "ready"])
