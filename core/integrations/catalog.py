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
    Companion("stealer", "表情包小偷", "文字与表情的联合回复",
              "在表情插件中设置候选、自动发送和作用域；此处沿用回复时机与发送校验。",
              ("start_joint_reply_prefetch",), "media"),
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


def find_companion(context: Any, name: str) -> Any:
    """Resolve a fresh active instance; reload/disable takes effect immediately."""
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
        active = item is not None and bool(_value(item, "activated", True))
        required = ((spec.key == "autonomous_blocking" and bool(config.get("autonomous_blocking_required")))
                    or (spec.key in {"worldbook", "dynamic_life_state"} and bool(config.get("narrative_enabled"))))
        missing = [method for method in spec.methods if not callable(getattr(instance, method, None))] if active else []
        initializer = getattr(instance, "initializer", None)
        loading = initializer is not None and getattr(initializer, "is_initialized", True) is False
        stopping = getattr(instance, "_terminating", False) is True or getattr(instance, "_shutting_down", False) is True
        state = ("missing" if item is None else "disabled" if not active else "loading" if instance is None or loading or stopping
                 else "partial" if missing else "ready")
        version = _value(item, "version", "") if item is not None else ""
        rows.append(dict(id=spec.key, name=spec.title, plugin="astrbot_plugin_" + spec.key,
                         version=version[:80] if isinstance(version, str) else "", state=state,
                         required=required, role=spec.role, settings=spec.settings,
                         config_group=spec.config_group, missing_methods=missing))
    return dict(schema_version=1, companions=rows,
                blocking=[row["id"] for row in rows if row["required"] and row["state"] != "ready"])
