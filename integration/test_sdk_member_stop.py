"""Real AstrBot SDK contracts for member stop and native delivery ownership."""

from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


try:
    astrbot = importlib.import_module("astrbot")
except ImportError:
    if os.environ.get("CHAT_DYNAMICS_REQUIRE_REAL_SDK") == "1":
        raise RuntimeError("real AstrBot SDK is required for this integration job")
    pytest.skip("real AstrBot SDK is not installed", allow_module_level=True)
if getattr(astrbot, "__spec__", None) is None:
    if os.environ.get("CHAT_DYNAMICS_REQUIRE_REAL_SDK") == "1":
        raise RuntimeError("tests/conftest.py SDK double is not a real AstrBot installation")
    pytest.skip("tests/conftest.py SDK double is not a real AstrBot installation", allow_module_level=True)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLUGIN_DIR.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from astrbot.api.platform import MessageType  # noqa: E402
from astrbot.core.message.message_event_result import MessageEventResult  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.star.filter.command import CommandFilter  # noqa: E402
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter  # noqa: E402
from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402

from astrbot_plugin_chat_dynamics.core.native_delivery import NativeDeliveryGuard  # noqa: E402
from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin  # noqa: E402


class RealEvent(AstrMessageEvent):
    """Minimal concrete event using the SDK's real AstrMessageEvent base."""

    def __init__(self) -> None:
        message_obj = SimpleNamespace(
            type=MessageType.GROUP_MESSAGE,
            message=[],
            message_id="incoming",
            group_id="room",
            sender=SimpleNamespace(user_id="member", nickname="member"),
            self_id="bot",
        )
        platform_meta = SimpleNamespace(
            id="sdk-test",
            name="sdk-test",
            support_proactive_message=False,
            support_streaming_message=False,
        )
        # AstrBot strips the command prefix before CommandFilter sees a
        # post-waking command event.
        super().__init__("dynamics_stop", message_obj, platform_meta, "room")
        self.calls: list[object] = []

    async def send(self, message):
        self.calls.append(message)
        return "native-head-id"


def _handler(method) -> object:
    full_name = f"{method.__module__}_{method.__name__}"
    metadata = star_handlers_registry.get_handler_by_full_name(full_name)
    assert metadata is not None, full_name
    return metadata


def _command_filter(metadata, command_name: str) -> CommandFilter:
    command_filters = [item for item in metadata.event_filters if isinstance(item, CommandFilter)]
    command = next((item for item in command_filters if item.command_name == command_name), None)
    assert command is not None, (metadata.handler_name, command_name)
    return command


def test_real_sdk_command_registry_keeps_stop_member_access_and_dynamics_admin():
    stop_metadata = _handler(ChatDynamicsPlugin.cmd_dynamics_stop)
    dynamics_metadata = _handler(ChatDynamicsPlugin.cmd_dynamics)
    stop_command = _command_filter(stop_metadata, "dynamics_stop")
    dynamics_command = _command_filter(dynamics_metadata, "dynamics")

    stop_permissions = [
        item for item in stop_metadata.event_filters if isinstance(item, PermissionTypeFilter)
    ]
    dynamics_permissions = [
        item for item in dynamics_metadata.event_filters if isinstance(item, PermissionTypeFilter)
    ]
    assert not stop_permissions
    assert any(item.permission_type == PermissionType.ADMIN for item in dynamics_permissions)

    event = RealEvent()
    event.is_at_or_wake_command = True
    assert stop_command.filter(event, None) is True
    event.message_str = "dynamics"
    assert dynamics_command.filter(event, None) is True
    assert any(item.filter(event, None) is False for item in dynamics_permissions)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", [False, True])
async def test_native_guard_real_sdk_event_records_head_outcome(failing):
    event = RealEvent()
    result = MessageEventResult().message("首段")
    event.set_result(result)

    async def send(message):
        event.calls.append(message)
        if failing:
            raise RuntimeError("native send failed")
        return "native-head-id"

    event.send = send
    guard = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard.install() is True
    try:
        if failing:
            with pytest.raises(RuntimeError, match="native send failed"):
                await event.send(event.get_result())
            assert guard.outcome is not None and guard.outcome.success is False
        else:
            returned = await event.send(event.get_result())
            assert returned == "native-head-id"
            assert guard.outcome is not None and guard.outcome.success is True
        assert guard.started is True
        assert len(event.calls) == 1
    finally:
        guard.restore()
