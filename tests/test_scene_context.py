import json
from astrbot_plugin_chat_dynamics.core.narrative import TurnContextBundle,REPLY_GUIDANCE
from astrbot_plugin_chat_dynamics.core.narrative_projection import reply_material
from astrbot_plugin_chat_dynamics.core.scene_context import SCENE_GUIDANCE

def test_scene_truth_reaches_chat_without_distant_items_or_future_completion():
    scene={'available':True,'place_name':'住所','room_name':'房间','sheltered':True,'wetness':0,
           'items':[{'id':'book','name':'书','kind':'book','carried':True,'page':10,'pages':120,'visible':True},
                    {'id':'secret','name':'别处物品','kind':'book','carried':False,'visible':False}],
           'action':{'status':'running','label':'阅读：书','index':0,'steps':[{'kind':'read','target_id':'book'}],'step_ends_at':200},
           'receipts':[{'kind':'step.completed','operation':'take','occurred_at':100,'result':{'name':'书','carried':True,'page':10}}]}
    data={'scope':{'platform':'qq','umo':'qq:GroupMessage:1','persona_id':'p','instance_id':'p'},'snapshot':{'activity':'阅读','scene':scene},'captured_at':110,'speaker_id':'u','context_items':[],'character':{}}
    for projected in [reply_material(data)['snapshot'],TurnContextBundle('x',json.dumps(data)).decision_context()]:
        assert projected['scene']['inventory'][0]['page']==10
        assert projected['scene']['action']['status']=='running'
        assert '别处物品' not in json.dumps(projected,ensure_ascii=False)
    assert SCENE_GUIDANCE in REPLY_GUIDANCE and '不能当作已经撑着' in REPLY_GUIDANCE
