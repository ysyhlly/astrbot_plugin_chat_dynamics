"""Group-facing /群间 feature card and /群间 自检 health summary builders."""

from __future__ import annotations

import inspect
from typing import Any

from .series_hub import CORE_SERIES_PLUGINS, build_series_health

_PLUGIN_CARD_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "astrbot_plugin_chat_dynamics",
        "title": "对话主控",
        "commands": [
            "/dynamics_stop — 立即停止机器人尚未发给你的回复",
            "/群间 — 查看本群已启用的群间生态玩法",
        ],
    },
    {
        "id": "astrbot_plugin_dynamics_world",
        "title": "群间世界 · 3D 小屋",
        "commands": [
            "/小屋 · /小屋实况 [全景|近景|院门] — 查看角色此刻状态与小屋实况",
            "/投递 [明信片|鲜花|点心|礼物] <内容> · /探访 — 给小屋投递心意或探访",
        ],
    },
    {
        "id": "astrbot_plugin_dynamics_memory",
        "title": "群间记忆",
        "commands": [
            "/我的记忆 — 查看角色记得你的核心记忆卡片",
            "/纠错 <说明> — 提交记忆纠偏提案",
        ],
    },
    {
        "id": "astrbot_plugin_dynamics_learning",
        "title": "群间学习",
        "commands": [
            "/我的印象 — 查看角色对你的印象与相处氛围卡片",
            "/群黑话 · /纠正黑话 — 查看或纠正本群黑话理解",
        ],
    },
    {
        "id": "astrbot_plugin_dynamics_horizons",
        "title": "群间见闻",
        "commands": [
            "/安利 <链接/BV/书名> [推荐语] — 给角色投喂好内容，等 TA 读完聊感想",
        ],
    },
    {
        "id": "astrbot_plugin_dynamics_games",
        "title": "群间游戏",
        "commands": [
            "/共读 · /约局 · /接龙 · /挑战 · /共创 — 发起或参与群协作活动",
            "/吃什么 · /21点 · /今日小猪 — 群内轻松小游戏",
        ],
    },
    {
        "id": "astrbot_plugin_visual_media",
        "title": "视觉工坊",
        "commands": [
            "/做表情 [情绪] · /meme pref — 制作角色表情包或调整表情偏好",
        ],
    },
    {
        "id": "astrbot_plugin_group_chronicle",
        "title": "群文化沿革 · 史记",
        "commands": [
            "史记帮助 · 史记列表 · /我的群年报 — 回顾群史卷册与年度记忆",
            "史记隐藏 · 史记恢复 — 自主管理个人在群史中的可见性",
        ],
    },
)


def _probe_plugin_enabled_for_umo(plugin: Any, pid: str, umo: str, group_id: str) -> tuple[bool, bool]:
    """Return (loaded, enabled_for_group) for one series plugin without throwing."""
    from .. import series_panels

    if pid == "astrbot_plugin_chat_dynamics":
        enabled = bool(
            getattr(plugin, "enabled", True)
            and (plugin.is_group_takeover_enabled(group_id) if group_id else True)
        )
        return True, enabled

    owner = series_panels.find_plugin_owner(getattr(plugin, "context", None), pid)
    if owner is None:
        return False, False

    getter = getattr(owner, "get_series_scope_status", None)
    if callable(getter) and not inspect.iscoroutinefunction(getter):
        try:
            raw = getter(umo or group_id)
            if inspect.isawaitable(raw):
                if inspect.iscoroutine(raw):
                    raw.close()
            elif isinstance(raw, dict) and isinstance(raw.get("enabled"), bool):
                return True, bool(raw["enabled"])
        except Exception:
            pass

    cap_fn = getattr(owner, "get_dynamics_capabilities", None)
    if callable(cap_fn) and not inspect.iscoroutinefunction(cap_fn):
        try:
            cap = cap_fn()
            if isinstance(cap, dict):
                if cap.get("enabled") is False:
                    return True, False
                if cap.get("ready") is True or cap.get("enabled") is True:
                    return True, True
        except Exception:
            pass

    return True, True


def build_group_ecosystem_card(plugin: Any, *, umo: str = "", group_id: str = "", is_admin: bool = False) -> str:
    """Build the C-end /群间 feature discovery card reflecting active modules in this group."""
    enabled_sections: list[str] = []
    paused_names: list[str] = []
    uninstalled_count = 0

    for spec in _PLUGIN_CARD_SPECS:
        loaded, enabled = _probe_plugin_enabled_for_umo(plugin, spec["id"], umo, group_id)
        if not loaded:
            uninstalled_count += 1
            continue
        if enabled:
            cmd_lines = "\n".join(f"  · {line}" for line in spec["commands"])
            enabled_sections.append(f"【{spec['title']}】\n{cmd_lines}")
        else:
            paused_names.append(spec["title"])

    header = "✨ 【群间生态 · 本群玩法名片】"
    parts = [header]
    if enabled_sections:
        parts.append("\n\n".join(enabled_sections))
    else:
        parts.append("本群目前尚未开启任何群间系列模块。")
        if is_admin:
            parts.append("管理员可发送 `/dynamics on` 一键开启本群对话主控，或在 Web 面板「快速引导」中批量启用。")

    footer_bits: list[str] = []
    if paused_names:
        footer_bits.append(f"已安装但本群未启用：{'、'.join(paused_names)}")
    if uninstalled_count:
        footer_bits.append(f"另有 {uninstalled_count} 个系列模块未安装")
    if is_admin:
        footer_bits.append("管理员可用：`/群间 自检` · `/dynamics status | why | on | off | presence`")
    if footer_bits:
        parts.append("—\n" + "\n".join(footer_bits))

    return "\n\n".join(parts)


def build_series_self_check_report(plugin: Any) -> str:
    """Format build_series_health() into a concise admin chat diagnostics report."""
    health = build_series_health(plugin)
    summary = health.get("summary") or {}
    overall = str(health.get("overall") or "unknown").upper()
    installed = int(summary.get("installed_count", 0))
    ready = int(summary.get("ready_count", 0))
    total = int(summary.get("total_core_plugins", len(CORE_SERIES_PLUGINS)))
    issue_count = int(summary.get("issue_count", 0))

    lines = [
        f"🩺 【群间系列自检报告】状态：{overall}",
        f"• 核心插件：已就绪 {ready}/{installed}（共 {total} 个系列模块）",
        f"• 需处理告警：{issue_count} 项",
    ]

    panels_sync = health.get("series_panels_sync") or {}
    drifted = panels_sync.get("drifted") or []
    canonical_hash = str(panels_sync.get("canonical_sha256") or "")
    if drifted:
        drift_names = ", ".join(str(d.get("plugin_id") or "") for d in drifted[:6])
        lines.append(f"• 共享底座校验：⚠️ 检测到 {len(drifted)} 处哈希漂移（{drift_names}），请同步 series_panels.py")
    else:
        checked_n = int(panels_sync.get("checked_count", 0))
        lines.append(
            f"• 共享底座校验：✅ 一致（已校验 {checked_n} 个伴生插件"
            + (f"，sha256:{canonical_hash[:8]}" if canonical_hash else "")
            + "）"
        )

    env_warnings = health.get("environment_warnings") or []
    strong_warns = [w for w in env_warnings if w.get("severity") in {"error", "warning"}]
    overlap_warns = [w for w in env_warnings if w.get("code") == "plugin_feature_overlap"]

    if strong_warns:
        lines.append("• 强冲突/环境告警：")
        for w in strong_warns[:5]:
            lines.append(f"  - [{w.get('severity')}] {w.get('title')}: {w.get('detail')}")
    else:
        lines.append("• 强冲突/环境告警：无")

    if overlap_warns:
        lines.append("• 生态共存温和提醒（不阻断运行）：")
        for w in overlap_warns[:3]:
            lines.append(f"  - {w.get('detail')}")

    issues = health.get("issues") or []
    if issues:
        lines.append("• 插件待处理项：")
        for item in issues[:5]:
            lines.append(f"  - {item.get('plugin_label') or item.get('plugin_id')}: {item.get('title')}")

    return "\n".join(lines)
