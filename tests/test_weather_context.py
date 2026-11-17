import copy
import json

from astrbot_plugin_chat_dynamics.core.environment_context import environment_context,WEATHER_GUIDANCE
from astrbot_plugin_chat_dynamics.core.narrative import TurnContextBundle,REPLY_GUIDANCE
from astrbot_plugin_chat_dynamics.core.narrative_projection import reply_material


def material():
    current={'time':1000,'temperature':18,'wind_speed':20,'precipitation':.5,'precipitation_probability':70,'label':'雨','is_day':True}
    env={'available':True,'status':'ready','source':'MET Norway','location':'深圳（参考城市）','fact_kind':'external_weather','timezone':'Asia/Shanghai',
         'current':current,'expires_at':5000,'sunrise':100,'sunset':3000,'forecast':[dict(current,time=1000+i*3600) for i in range(1,25)],
         'settings':{'private_key':'never-in-model'},'latitude':22.5,'longitude':114,'cache_key':'internal'}
    return {'scope':{'platform':'qq','umo':'qq:GroupMessage:test','persona_id':'p','instance_id':'p'},'captured_at':1100,'speaker_id':'one',
            'snapshot':{'activity':'在屋内读书；环境参考（MET Norway）：深圳，雨 18℃，白天','environment':env,'revision':1,'scope_revision':1,'availability':'available'},
            'character':{},'context_items':[]}


def test_weather_reaches_both_chat_projections_without_duplicate_weather_prose():
    data=material()
    before=copy.deepcopy(data)
    bundle=TurnContextBundle('test',json.dumps(data))
    decision=bundle.decision_context()
    reply=reply_material(data)['snapshot']
    for output in [decision,reply]:
        assert output['activity']=='在屋内读书'
        assert output['environment']['current']['wind_speed']==20
        assert output['environment']['current']['precipitation']==.5
        assert output['environment']['units']['wind_speed']=='km/h'
        assert output['environment']['forecast']
        assert 'never-in-model' not in json.dumps(output) and 'latitude' not in output['environment']
    assert len(decision['environment']['forecast'])<=2 and len(reply['environment']['forecast'])<=4
    assert data==before and WEATHER_GUIDANCE in REPLY_GUIDANCE
    assert '无关话题不提天气' in REPLY_GUIDANCE and '不代表角色或聊天者真实位于该城市' in REPLY_GUIDANCE


def test_stale_or_expired_weather_is_not_presented_as_current_fact():
    data=material()
    data['snapshot']['environment']['stale']=True
    projected=environment_context(data['snapshot'],1100)
    assert projected['available'] is False and 'current' not in projected and 'forecast' not in projected
    data['snapshot']['environment']['stale']=False
    projected=environment_context(data['snapshot'],6000)
    assert not projected['available'] and projected['status']=='stale'


def test_old_providers_and_simulated_weather_keep_correct_meaning():
    data=material()
    data['snapshot'].pop('environment')
    assert '环境参考' in reply_material(data)['snapshot']['activity']
    assert 'environment' not in reply_material(data)['snapshot']
    data=material()
    data['snapshot']['environment']['fact_kind']='fiction'
    assert reply_material(data)['snapshot']['environment']['fact_kind']=='fiction'
