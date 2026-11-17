from types import SimpleNamespace as NS

import pytest

from astrbot_plugin_chat_dynamics.main import ChatDynamicsPlugin
from .test_private_chat_chain import private_plugin as private_plugin, private_event
from .test_persona_model import flush, drain


def test_private_delivered_node_does_not_enter_group_topic_router():
    calls=[]
    plugin=NS(_mark_panel_runtime_dirty=lambda:calls.append('panel'),
              _runtime_config=NS(conversation_router_enabled=True),
              thread_router=NS(observe_bot_message=lambda runtime,node:(_ for _ in ()).throw(KeyError('routing'))))
    runtime=NS(scope_kind='private')
    node=NS(metadata={'dialogue_delivered':True})
    ChatDynamicsPlugin._observe_routed_bot(plugin,runtime,node)
    assert calls==['panel']
    assert node.metadata=={'dialogue_delivered':True}


def test_group_delivery_still_updates_topic_router():
    calls=[]
    plugin=NS(_mark_panel_runtime_dirty=lambda:None,
              _runtime_config=NS(conversation_router_enabled=True),
              thread_router=NS(observe_bot_message=lambda runtime,node:calls.append(node)))
    node=NS(metadata={})
    ChatDynamicsPlugin._observe_routed_bot(plugin,NS(scope_kind='group'),node)
    assert calls==[node]


@pytest.mark.asyncio
async def test_substantive_private_reply_finishes_bookkeeping_after_delivery(private_plugin):
    plugin,bridge=private_plugin
    event=private_event('想象一只戴红围巾的蓝鲸坐在月亮上钓星星，这画面会是什么样',message_id='natural-scene')
    try:
        await plugin.on_private_message(event)
        runtime=plugin._sessions[event.unified_msg_origin]
        assert runtime.scope_kind=='private'
        await flush(plugin,event)
        await drain(plugin)
        assert 'routing' not in runtime.dag.get_node('natural-scene').metadata
        assert len(event.replies_sent)==1
        assert bridge.commits
        assert runtime.last_bot_node is not None
        assert runtime.last_interlocutor==event.sender_id
        assert runtime.last_bot_node.metadata['dialogue_delivered'] is True
        assert 'routing' not in runtime.last_bot_node.metadata
    finally:
        await plugin.terminate()
