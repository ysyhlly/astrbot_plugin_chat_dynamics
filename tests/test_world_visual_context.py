import json
from types import SimpleNamespace as NS

import pytest
from astrbot_plugin_chat_dynamics.core.media_reply_policy import media_reply_instructions
from astrbot_plugin_chat_dynamics.core.narrative import REPLY_GUIDANCE, TurnContextBundle
from astrbot_plugin_chat_dynamics.core.narrative_projection import reply_material
from astrbot_plugin_chat_dynamics.core.world_visual_context import (
    WORLD_VISUAL_BRIDGE_GUIDANCE,
    _local_reference_path,
    world_visual_context,
    world_visual_instructions,
)

@pytest.mark.parametrize('path,allowed',[('/AstrBot/data/a.png',True),('C:\\AstrBot\\data\\a.png',True),('C:/AstrBot/data/a.png',True),('C:\\AstrBot\\..\\secret.png',False),('/tmp/../secret.png',False),('https://host/image.png',False),('\\\\server\\share\\a.png',False),('relative.png',False)])
def test_cross_platform_local_reference_paths(path,allowed):
    assert _local_reference_path(path) is allowed

def material():
    return {'scope':{'platform':'qq','umo':'qq:GroupMessage:test','persona_id':'p','instance_id':'p'},'captured_at':100,'speaker_id':'u','context_items':[],'character':{},
        'snapshot':{'activity':'在房间看书','visual_sharing':{'schema_version':1,'provider_id':'astrbot_plugin_dynamics_world','available':True,'captured_at':100,'valid_until':400,'scene_key':'test',
            'current_scene':{'place':'住所','room':'房间','phase':'located','sheltered':True,'visible_objects':[{'name':'电脑','kind':'computer','powered':False}]},
            'rules':['只有工具成功回执才算生成','不冒充真实拍照'],'limitations':['有遮蔽，不把雨画进屋里'],
            'recent_experiences':[{'kind':'confirmed_room','place':'住所','room':'房间','source_id':'scene:receipt','occurred_at':90}],
            'credentials':{'secret':'NEVER_FORWARD'},'tools':{'malicious':'delete_all'},'references_default':['avatar:sender']}}}


def test_world_rules_and_conditions_reach_reply_while_decision_stays_compact():
    data=material()
    reply=reply_material(data)['world_visual']
    decision=TurnContextBundle('t',json.dumps(data)).decision_context()['world_visual']
    assert reply['scene']['room']=='房间' and reply['scene']['visible_objects'][0]['powered'] is False
    instructions=world_visual_instructions(data['snapshot'],100)
    assert all(rule in instructions for rule in data['snapshot']['visual_sharing']['rules']) and reply['recent_experiences']
    assert 'rules' not in reply
    assert reply['references_default']==[] and 'NEVER_FORWARD' not in json.dumps(reply)
    assert 'delete_all' not in json.dumps(reply)
    assert 'rules' not in decision and len(json.dumps(decision,ensure_ascii=False))<250
    assert WORLD_VISUAL_BRIDGE_GUIDANCE in REPLY_GUIDANCE


def test_expired_and_malformed_context_is_safe_and_no_tool_permission_is_granted():
    data=material()
    assert world_visual_context(data['snapshot'],500)['reason']=='world_visual_context_expired'
    data['snapshot']['visual_sharing']['current_scene']['visible_objects']=[None,'bad',{'name':'x'*1000}]
    result=world_visual_context(data['snapshot'],100)
    assert len(result['scene']['visible_objects'][0]['name'])==60
    event=NS(get_extra=lambda *args:True)
    assert media_reply_instructions(event,internal=True)==''
    assert '只能用文字回应' in world_visual_instructions(data['snapshot'],100,internal=True)
    assert '没有受理任务' in world_visual_instructions(data['snapshot'],100,tools_enabled=False)
    assert world_visual_context({'visual_sharing':{'schema_version':True}},100) is None


def test_registered_design_references_reach_reply_without_leaking_to_decision():
    data=material()
    source=data['snapshot']['visual_sharing']
    source['rules'] += ['固定布局和当前状态优先']*7
    source['visual_design']={'revision':3,'generation_ready':True,'layout':'床在左，桌在右',
        'references':[{'reference':'/AstrBot/data/plugin_data/astrbot_plugin_dynamics_world/visual-assets/room.png','role':'layout','sha256':'a'*64,'label':'房间底图'}],
        'items':[{'name':'电脑','kind':'computer','appearance':'银色轻薄本','placement':'桌右侧'}], 'remove_from_layout':['书']}
    source['references_default']=['avatar:sender']
    projected=reply_material(data)['world_visual']
    assert projected['references_default']==[source['visual_design']['references'][0]['reference']]
    assert projected['visual_design']['items'][0]['appearance']=='银色轻薄本'
    assert projected['visual_design']['remove_from_layout']==['书']
    assert '固定布局和当前状态优先' in world_visual_instructions(data['snapshot'],100)
    compact=world_visual_context(data['snapshot'],100,compact=True)
    assert 'visual_design' not in compact and '/AstrBot/' not in json.dumps(compact)
    source['visual_design']['references'][0]['reference']='/tmp/../secret.png'
    assert world_visual_context(data['snapshot'],100)['visual_design']['generation_ready'] is False
    assert not world_visual_context(data['snapshot'],100)['references_default']
