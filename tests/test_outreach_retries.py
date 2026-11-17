"""Pre-send retries are durable, bounded, and isolated per candidate."""
import asyncio
import copy
import json
from contextlib import closing
from unittest.mock import AsyncMock

import pytest
from .test_outreach_dispatch import harness as _harness_fixture
from astrbot_plugin_chat_dynamics.core.narrative_store import NarrativeJournal

harness = _harness_fixture

def dispatcher(harness):
    manager,p,event,item,*_=harness
    item['target_umo']=item['scope']['umo']
    items={item['intent_id']:item}
    async def rpc(provider,method,*args,**kwargs):
        if method=='list_due_intents':
            return [copy.deepcopy(i) for i in items.values() if i['status']=='pending']
        if method=='get_intent':
            return {'item':copy.deepcopy(items[args[0]])}
        if method=='update_intent':
            items[args[0]]['status']=args[1]
            return {'changed':True}
    manager.call=AsyncMock(side_effect=rpc)
    return manager,p,event,item,items

@pytest.mark.asyncio
async def test_failed_head_does_not_block_later_candidate(harness):
    manager,p,event,item,items=dispatcher(harness)
    other=dict(item,intent_id='second',recipient_user_id='456')
    items['second']=other
    async def send(candidate):
        if candidate['intent_id']==item['intent_id']:
            raise RuntimeError('synthetic outage')
        manager.journal.intent_status('second','sent')
        other['status']='sent'
    manager.send_intent=AsyncMock(side_effect=send)
    await manager.dispatch_intents()
    assert manager.send_intent.await_count==2
    assert dict((i['intent_id'],s) for i,s in manager.journal.intents())=={item['intent_id']:'pending','second':'sent'}
    await manager.dispatch_intents()
    assert manager.send_intent.await_count==2
    event.send.assert_not_awaited()

@pytest.mark.asyncio
async def test_retry_survives_restart_and_exhausts_without_new_send(harness):
    manager,p,event,item,_=dispatcher(harness)
    manager.send_intent=AsyncMock(side_effect=RuntimeError('synthetic outage'))
    now=[10000.0]
    p.time_service.wall_time=lambda:now[0]
    await manager.dispatch_intents()
    manager.journal=NarrativeJournal(manager.journal.path)
    await manager.dispatch_intents()
    assert manager.send_intent.await_count==1
    for delay in (60,120,300,900,1800):
        now[0]+=delay
        item['expires_at']=now[0]+7200
        # Existing persisted window also has to remain valid during retries.
        with closing(manager.journal._connect()) as db,db:
            db.execute('UPDATE intents SET payload=? WHERE intent_id=?',(json.dumps(item),item['intent_id']))
        await manager.dispatch_intents()
    assert manager.send_intent.await_count==6
    assert item['status']=='failed'
    assert manager.journal.intents()[0][1]=='failed'
    event.send.assert_not_awaited()

@pytest.mark.asyncio
async def test_success_then_history_error_never_reopens_sent_candidate(harness):
    manager,p,event,item,*_=harness
    item['target_umo']=item['scope']['umo']
    manager.journal.add_intent(item)
    p.persona_engine.bridge.commit.side_effect=RuntimeError('synthetic history failure')
    await manager.dispatch_intents()
    assert item['status']=='sent'
    event.send.assert_awaited_once()
    await manager.dispatch_intents()
    event.send.assert_awaited_once()
    with closing(manager.journal._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM intent_retries').fetchone()[0]==0

@pytest.mark.asyncio
async def test_task_cancellation_is_not_a_provider_retry(harness):
    manager,p,event,item,_=dispatcher(harness)
    manager.send_intent=AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await manager.dispatch_intents()
    assert manager.journal.intent_retry_ready(item['intent_id'],10000)
    event.send.assert_not_awaited()
