"""Native delivery's synchronous adapter contracts and structured outcomes."""
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from astrbot_plugin_chat_dynamics.core.native_delivery import NativeDeliveryGuard, _as_send_result
from astrbot_plugin_chat_dynamics.core.platform_bridge import SendResult


@pytest.mark.parametrize("raw,success,message_id", [
    (0,False,None), (1,True,None), (False,False,None), (None,True,None),
    ({"status": 201,"id": 4},True,"4"), ({"status": 500},False,None),
    ({"success": 0},False,None), ({"ok": "false"},False,None),
    ({"error": "rejected", "msg_id": 2},False,"2"),
    (NS(success=False,error="rejected",message_id=7),False,"7"),
    (NS(id=8),True,"8"), ("sent",True,"sent"), ({},True,None),
])
def test_raw_adapter_outcome_contract(raw, success, message_id):
    outcome = _as_send_result(raw)
    assert outcome.success is success
    assert outcome.message_id == message_id


def test_result_identity_and_bad_attribute_adapter():
    outcome = SendResult(False, error="failed")
    assert _as_send_result(outcome) is outcome
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("adapter unavailable")
    assert _as_send_result(Broken()).success


@pytest.mark.parametrize("stale,bypass", [(False,False),(True,False),(True,True)])
def test_sync_native_send_guard(stale, bypass):
    result = NS(chain=[object()])
    send = Mock(return_value={"id": "native"})
    event = NS(send=send, get_result=lambda: result)
    guard = NativeDeliveryGuard(event, result, lambda: not stale, lambda: bypass)
    assert guard.install() and guard.install()
    event.send(chain=result.chain)
    assert send.call_count == (0 if stale and not bypass else 1)
    assert guard.cancelled is (stale and not bypass)
    assert guard.started is (not stale and not bypass)
    if bypass:
        assert guard.outcome is None
    guard.restore()
    assert event.send is send


def test_sync_failure_is_recorded_and_unrelated_send_passes_through():
    result = NS(chain=[object()])
    send = Mock(side_effect=RuntimeError("delivery failed"))
    event = NS(send=send, get_result=lambda: result)
    guard = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard.install()
    with pytest.raises(RuntimeError):
        event.send("unrelated")
    assert guard.outcome is None
    with pytest.raises(RuntimeError):
        event.send(message=result)
    assert guard.outcome == SendResult(False, error="delivery failed")
    guard.restore()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False,True])
async def test_sync_adapter_returning_awaitable(fail):
    result = NS(chain=[object()])
    async def deliver():
        if fail:
            raise RuntimeError("failed")
        return {"id": "native"}
    event = NS(send=lambda *args: deliver(), get_result=lambda: result)
    guard = NativeDeliveryGuard(event, result, lambda: True, lambda: False)
    assert guard.install()
    if fail:
        with pytest.raises(RuntimeError):
            await event.send(result)
    else:
        assert await event.send(result) == {"id": "native"}
    assert guard.outcome.success is (not fail)
    guard.restore()


def test_missing_send_and_unreadable_ownership_fail_closed():
    result = NS(chain=42)
    assert not NativeDeliveryGuard(NS(), result, None, None).install()
    assert not NativeDeliveryGuard(NS(send=None), result, None, None).install()
    event = NS(send=Mock(), get_result=Mock(side_effect=RuntimeError))
    guard = NativeDeliveryGuard(event,result,Mock(side_effect=RuntimeError),Mock(side_effect=RuntimeError))
    assert guard.install()
    event.send(result)
    assert guard.cancelled and not guard.started
    assert guard.outcome.success is False
    guard.restore()
