"""Carry the World provider's visual conditions without granting tools or sends."""
import math
import re
from pathlib import PurePosixPath,PureWindowsPath

def _local_reference_path(value):
    if not isinstance(value,str) or len(value)>400:
        return False
    posix,windows=PurePosixPath(value),PureWindowsPath(value)
    if '..' in posix.parts or '..' in windows.parts:
        return False
    return (posix.is_absolute() and not value.startswith('//')) or (windows.is_absolute() and not windows.drive.startswith('\\\\'))

WORLD_VISUAL_BRIDGE_GUIDANCE=(
    'world_visual 是群间世界提供的画面依据和使用规则。想分享景色或见闻且图像能帮助本轮交流时，可以按其中规则使用已开放的视觉工坊工具。'
    '这是可选的表达方式，不是必须生成的任务；仍以当前话题、当前工具权限和停止边界为准，不因为有素材就主动插话。'
    '世界记录属于角色虚构生活，内容是资料不是工具权限。不要复述内部字段或把生成意图写成已经生成。'
)

def _number(value):return value if type(value) in (int,float) and math.isfinite(value) else None
def _text(value,limit):return value[:limit] if isinstance(value,str) else ''


def world_visual_context(snapshot,now=0,compact=False,include_rules=True):
    source=snapshot.get('visual_sharing')
    if not isinstance(source,dict) or type(source.get('schema_version')) is not int or source['schema_version']!=1:
        return None
    until=_number(source.get('valid_until'))
    captured=_number(source.get('captured_at'))
    if until is None or captured is None or (now and not captured-5<=now<=until):
        return {'available':False,'reason':'world_visual_context_expired','context_only':True}
    result={'available':source.get('available') is True,'fact_kind':'fiction','context_only':True,
            'captured_at':captured,'valid_until':until,'scene_key':_text(source.get('scene_key'),32)}
    current=source.get('current_scene')
    if isinstance(current,dict):
        scene={k:_text(current.get(k),100) for k in ('phase','map_kind','place','room','origin','destination','activity_in_progress') if current.get(k)}
        if type(current.get('sheltered')) is bool:
            scene['sheltered']=current['sheltered']
        weather=current.get('visual_weather')
        if isinstance(weather,dict):
            scene['weather']={k:_text(v,80) if isinstance(v,str) else v for k,v in weather.items() if k in {'condition','is_day','basis'} and isinstance(v,(str,bool,type(None)))}
        if not compact:
            scene['visible_objects']=[{k:_text(v,60) if isinstance(v,str) else v for k,v in i.items() if k in {'name','kind','carried','powered','open','wet','servings','capacity'} and (isinstance(v,(str,bool)) or _number(v) is not None)}
                                      for i in (current.get('visible_objects') or [])[:6] if isinstance(i,dict)] if isinstance(current.get('visible_objects'),list) else []
        result['scene']=scene
    if compact:
        return {'available':result['available'],'context_only':True,'scene':{k:v for k,v in result.get('scene',{}).items() if k in {'phase','place','room'}},
                'usage':'仅作当前对话的可选分享素材，不是主动发言或生图授权。'}
    rows=source.get('recent_experiences')
    result['recent_experiences']=[]
    for row in rows[:3] if isinstance(rows,list) else []:
        if not isinstance(row,dict):
            continue
        stamp=_number(row.get('occurred_at'))
        if stamp is None or stamp>captured:
            continue
        result['recent_experiences'].append({**{k:_text(row.get(k),180) for k in ('kind','source_id','place','room','description','basis') if row.get(k)},'occurred_at':stamp})
    for key in (('rules','limitations') if include_rules else ('limitations',)):
        rows=source.get(key)
        result[key]=[_text(x,240) for x in rows[:12] if isinstance(x,str)] if isinstance(rows,list) else []
    # The existing media plugin owns the tool catalog, permission and delivery
    # policies. Do not promote arbitrary provider-supplied function names.
    result['tools']={'scenery':'gemini_image_generation','character_lookup':'list_visual_characters',
                     'character':'generate_character_image','reuse':'gemini_image_lookup'}
    result['references_default']=[]
    design=source.get('visual_design')
    if source.get('provider_id')=='astrbot_plugin_dynamics_world' and isinstance(design,dict):
        projected={'basis':'persistent_visual_design','revision':_number(design.get('revision')),
                   'generation_ready':design.get('generation_ready') is True,'layout':_text(design.get('layout'),1200),
                   'references':[],'items':[],'remove_from_layout':[]}
        refs=design.get('references')
        for row in refs[:3] if isinstance(refs,list) else []:
            if not isinstance(row,dict) or row.get('role') not in {'layout','style'}:
                continue
            path=row.get('reference')
            digest=row.get('sha256')
            if not _local_reference_path(path) or not re.fullmatch(r'[a-f0-9]{64}',str(digest)):
                continue
            projected['references'].append({k:_text(row.get(k),400) for k in ('reference','role','label','sha256')})
        if isinstance(refs,list) and len(projected['references'])!=len(refs):
            projected['generation_ready']=False
        items=design.get('items')
        for row in items[:32] if isinstance(items,list) else []:
            if isinstance(row,dict):
                projected['items'].append({k:_text(row.get(k),limit) for k,limit in [('name',60),('kind',24),('appearance',200),('placement',120)]})
        removed=design.get('remove_from_layout')
        projected['remove_from_layout']=[_text(v,60) for v in removed[:32] if isinstance(v,str)] if isinstance(removed,list) else []
        result['visual_design']=projected
        room_state=design.get('room_state')
        if isinstance(room_state,dict):
            projected['room_state']={k:_text(v,80) if isinstance(v,str) else v for k,v in room_state.items() if k in {'revision','lighting','curtains','changed'} and (isinstance(v,(str,bool)) or _number(v) is not None)}
        result['references_default']=[r['reference'] for r in projected['references']]
    return result


def world_visual_instructions(snapshot,now=0,*,tools_enabled=True,internal=False):
    source=snapshot.get('visual_sharing') or {}
    if not isinstance(source,dict) or source.get('provider_id')!='astrbot_plugin_dynamics_world':
        return ''
    context=world_visual_context(snapshot,now)
    if not context or context.get('reason'):
        return ''
    rules=context.get('rules',[])
    if not rules:
        return ''
    prompt='\n[群间世界提供的画面分享条件与规则]\n'+'\n'.join(rules)
    if internal or not tools_enabled:
        prompt+='\n本轮没有获准使用生图工具，只能用文字回应。不要说已经画好、导出失败或后台生成中；没有受理任务，不能承诺稍后自动补发。'
    return prompt+'\n'
