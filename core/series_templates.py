"""Built-in and persistent custom group onboarding templates for the 8-plugin series."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .group_profiles import sanitize_group_profile
from .series_hub import CORE_SERIES_PLUGINS

_CORE_PLUGIN_IDS: tuple[str, ...] = tuple(pid for pid, _label, _mark, _page in CORE_SERIES_PLUGINS)
_CORE_PLUGIN_SET: frozenset[str] = frozenset(_CORE_PLUGIN_IDS)
_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_MAX_CUSTOM_TEMPLATES = 64
_MAX_NAME_LEN = 64
_MAX_DESC_LEN = 240
_MAX_HINT_LEN = 160


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _full_states(enabled_ids: set[str]) -> dict[str, bool]:
    return {pid: (pid in enabled_ids) for pid in _CORE_PLUGIN_IDS}


BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "full_companion",
        "name": "全能陪伴群",
        "description": "主控与全部 7 个伴生模块全开，适合主力活跃陪伴群。",
        "builtin": True,
        "readonly": True,
        "strategy_hint": "全模块协同，适合核心陪伴群",
        "states": _full_states(set(_CORE_PLUGIN_IDS)),
        "group_profile": {
            "presence_knob": "sensible",
            "reply_probability_threshold": 70.0,
            "shadow_mode": False,
            "natural_reply_rhythm_enabled": True,
        },
        "updated_at": "",
    },
    {
        "id": "light_chat",
        "name": "轻量陪聊群",
        "description": "开启对话主控、群间记忆与视觉工坊，其余扩展模块暂停。",
        "builtin": True,
        "readonly": True,
        "strategy_hint": "轻量化对话与视觉表达，低资源占用",
        "states": _full_states(
            {
                "astrbot_plugin_chat_dynamics",
                "astrbot_plugin_dynamics_memory",
                "astrbot_plugin_visual_media",
            }
        ),
        "group_profile": {
            "presence_knob": "ghost",
            "reply_probability_threshold": 80.0,
            "shadow_mode": False,
        },
        "updated_at": "",
    },
    {
        "id": "observer_archive",
        "name": "观察与归档模式",
        "description": "开启对话主控（可配合观察模式）、群间记忆与群文化沿革，关闭主动发散类模块。",
        "builtin": True,
        "readonly": True,
        "strategy_hint": "推荐配合观察模式，专注记忆沉淀与群史归档",
        "states": _full_states(
            {
                "astrbot_plugin_chat_dynamics",
                "astrbot_plugin_dynamics_memory",
                "astrbot_plugin_group_chronicle",
            }
        ),
        "group_profile": {
            "presence_knob": "ghost",
            "shadow_mode": True,
        },
        "updated_at": "",
    },
    {
        "id": "core_only",
        "name": "仅主控对话",
        "description": "仅开启对话主控，全部 7 个伴生插件暂停。",
        "builtin": True,
        "readonly": True,
        "strategy_hint": "最小化部署，仅由主控接管对话",
        "states": _full_states({"astrbot_plugin_chat_dynamics"}),
        "group_profile": {},
        "updated_at": "",
    },
)

BUILTIN_TEMPLATE_IDS: frozenset[str] = frozenset(t["id"] for t in BUILTIN_TEMPLATES)


def _clone_template(item: dict[str, Any]) -> dict[str, Any]:
    raw_gp = item.get("group_profile")
    try:
        gp = sanitize_group_profile(raw_gp) if isinstance(raw_gp, dict) and raw_gp else {}
    except ValueError:
        gp = {}
    return {
        "id": str(item["id"]),
        "name": str(item["name"]),
        "description": str(item.get("description") or ""),
        "builtin": bool(item.get("builtin", False)),
        "readonly": bool(item.get("readonly", False)),
        "strategy_hint": str(item.get("strategy_hint") or ""),
        "states": {pid: bool(item.get("states", {}).get(pid, False)) for pid in _CORE_PLUGIN_IDS},
        "group_profile": gp,
        "updated_at": str(item.get("updated_at") or ""),
    }


def validate_template_states(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("states must be a non-empty object")
    unknown = sorted(k for k in raw if k not in _CORE_PLUGIN_SET)
    if unknown:
        raise ValueError("unsupported plugin_id in states: " + ", ".join(unknown))
    normalized: dict[str, bool] = {}
    for pid in _CORE_PLUGIN_IDS:
        if pid in raw:
            val = raw[pid]
            if not isinstance(val, bool):
                raise ValueError(f"states[{pid}] must be a boolean")
            normalized[pid] = val
        else:
            normalized[pid] = False
    return normalized


class SeriesTemplateStore:
    """Atomic JSON store for custom group templates alongside 4 built-in templates."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        self.file_path = Path(file_path) if file_path is not None else None
        self._lock = threading.RLock()
        self._custom: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.file_path is None or not self.file_path.is_file():
            return
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        items = raw.get("templates") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return
        loaded: dict[str, dict[str, Any]] = {}
        for entry in items:
            if not isinstance(entry, dict):
                continue
            tid = str(entry.get("id") or "").strip().lower()
            name = str(entry.get("name") or "").strip()[:_MAX_NAME_LEN]
            if not tid or tid in BUILTIN_TEMPLATE_IDS or not _TEMPLATE_ID_RE.fullmatch(tid) or not name:
                continue
            try:
                states = validate_template_states(entry.get("states"))
            except ValueError:
                continue
            try:
                group_profile = sanitize_group_profile(entry.get("group_profile"))
            except ValueError:
                group_profile = {}
            loaded[tid] = {
                "id": tid,
                "name": name,
                "description": str(entry.get("description") or "").strip()[:_MAX_DESC_LEN],
                "builtin": False,
                "readonly": False,
                "strategy_hint": str(entry.get("strategy_hint") or "").strip()[:_MAX_HINT_LEN],
                "states": states,
                "group_profile": group_profile,
                "updated_at": str(entry.get("updated_at") or "").strip()[:64],
            }
            if len(loaded) >= _MAX_CUSTOM_TEMPLATES:
                break
        self._custom = loaded

    def _save_locked(self) -> None:
        if self.file_path is None:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "templates": [
                _clone_template(item)
                for item in sorted(self._custom.values(), key=lambda x: x["id"])
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.file_path.parent,
                prefix=".series-templates-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.file_path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            builtins = [_clone_template(t) for t in BUILTIN_TEMPLATES]
            customs = [
                _clone_template(t)
                for t in sorted(
                    self._custom.values(),
                    key=lambda x: (x.get("updated_at") or "", x["id"]),
                    reverse=True,
                )
            ]
            return builtins + customs

    def get(self, template_id: str) -> dict[str, Any] | None:
        tid = str(template_id or "").strip().lower()
        if not tid:
            return None
        for item in BUILTIN_TEMPLATES:
            if item["id"] == tid:
                return _clone_template(item)
        with self._lock:
            found = self._custom.get(tid)
            return _clone_template(found) if found is not None else None

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("template payload must be an object")
        raw_id = payload.get("id")
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            tid = f"custom_{uuid.uuid4().hex[:8]}"
        elif isinstance(raw_id, str):
            tid = raw_id.strip().lower()
            if not _TEMPLATE_ID_RE.fullmatch(tid):
                raise ValueError("template id must match ^[a-z0-9][a-z0-9_-]{0,47}$")
        else:
            raise ValueError("template id must be a string")

        if tid in BUILTIN_TEMPLATE_IDS:
            raise ValueError("内置模板不可修改或覆盖")

        raw_name = payload.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("模板名称不能为空")
        name = raw_name.strip()
        if len(name) > _MAX_NAME_LEN:
            raise ValueError(f"模板名称最多 {_MAX_NAME_LEN} 个字符")

        raw_desc = payload.get("description", "")
        if raw_desc is None:
            raw_desc = ""
        if not isinstance(raw_desc, str):
            raise ValueError("description must be a string")
        description = raw_desc.strip()[:_MAX_DESC_LEN]

        raw_hint = payload.get("strategy_hint", "")
        if raw_hint is None:
            raw_hint = ""
        if not isinstance(raw_hint, str):
            raise ValueError("strategy_hint must be a string")
        strategy_hint = raw_hint.strip()[:_MAX_HINT_LEN]

        states = validate_template_states(payload.get("states"))
        group_profile = sanitize_group_profile(payload.get("group_profile"))

        with self._lock:
            if tid not in self._custom and len(self._custom) >= _MAX_CUSTOM_TEMPLATES:
                raise ValueError(f"自定义模板数量已达上限 ({_MAX_CUSTOM_TEMPLATES})")
            record = {
                "id": tid,
                "name": name,
                "description": description,
                "builtin": False,
                "readonly": False,
                "strategy_hint": strategy_hint,
                "states": states,
                "group_profile": group_profile,
                "updated_at": _now_iso(),
            }
            self._custom[tid] = record
            self._save_locked()
            return _clone_template(record)

    def delete(self, template_id: str) -> bool:
        tid = str(template_id or "").strip().lower()
        if not tid:
            raise ValueError("template id required")
        if tid in BUILTIN_TEMPLATE_IDS:
            raise ValueError("内置模板不可删除")
        with self._lock:
            if tid not in self._custom:
                return False
            del self._custom[tid]
            self._save_locked()
            return True

    def catalog_payload(self) -> dict[str, Any]:
        return {
            "templates": self.list_all(),
            "plugins": [
                {"id": pid, "label": label, "mark": mark}
                for pid, label, mark, _page in CORE_SERIES_PLUGINS
            ],
        }
