"""Expose current fictional action facts without dumping the whole world."""
import math

SCENE_GUIDANCE=(
    'scene 是角色的程序维护状态。地点、房间、随身物品、书签、水壶余量、湿度和行动回执优先于旧聊天叙述。'
    'action 仍在执行时，只能描述正在做或打算做；有 completed 回执才能说对应动作已经完成。'
    '取消流程不倒退已经发生的结果，失败不能说成成功。未携带的伞不能当作已经撑着，空壶不能说又喝了一口，书签未到末页不能说读完这本书。'
    '可以表达计划，但普通聊天不直接执行世界动作；不要为回应一句话编造拿到、放下、补水、跨房间移动等行为。'
    '先回应当前话题。场景细节平时留在背景，只有对方问起、约好的进展确实发生、它实际限制回应，或它能推进当前话题时才简短提及；不把动作、物品或位置写成无关闲聊的开场。'
    '物品清单只覆盖本版跟踪的书、雨伞、水壶、纸质笔记本和电脑，未列出的人设物品不等于不存在；不能因此编造新物品转移。'
    '笔记和电脑草稿只有保存回执才算已写下；开机不等于已完成工作。电脑是世界内的虚拟设施，不据此声称真实上网、搜索网页、发消息或操作宿主设备。'
    'room_design 是当前房间已完成的布置，家具摆位、灯光、窗帘和物品placement优先于旧照片和历史叙述。布置中的动作仍须等回执，不说已完成；纸页展示不自动公开正文。'
    'projects 是持续创作的实际进度：已有构思、初稿、修订与定稿分开。暂停不等于放弃，保存草稿不等于完成作品。不自动公开项目全文或把小说内容当成见闻。'
    'causal_life 是程序维护的生活资料：prepared_food仅表示做好但尚未食用，consumed_food才表示实际食用。日程planned/deferred不等于已执行，邀请accepted不等于到场，只有visiting代表双方已到场。perceived_events仅包含角色实际感知的世界事件；背景角色的言语仍是角色陈述，不证明其中描述的外部事实。'
    'mailbox、keepsakes、garden 与 residents 为小屋信箱、纪念格、花圃和居民的轻量摘要；仅引用已记录条目，不编造未收到的来信、未拥有的纪念品或未到场的访客。'
)


def scene_context(snapshot):
    source=snapshot.get('scene')
    if not isinstance(source,dict) or not source.get('available'):
        return None
    result={'fact_kind':'fiction','tracked_item_types':['book','umbrella','water','notebook','computer'],'location':{'place':str(source.get('place_name') or '待定位')[:60],
             'room':str(source.get('room_name') or '待确认')[:60],'sheltered':source.get('sheltered')},'inventory':[],'visible_items':[]}
    wet=source.get('wetness')
    if type(wet) in (int,float) and math.isfinite(wet):
        result['clothing_wetness_percent']=wet
    for item in source.get('items',[])[:32]:
        if not item.get('carried') and not item.get('visible'):
            continue
        row={k:item[k] for k in ('name','kind','open','wet','page','pages','servings','capacity','powered','note_count','document_count','gaming_minutes','organize_count','completed_sessions','placement') if k in item}
        for key in ('notes','documents'):
            entries=item.get(key) or []
            if entries:
                row['latest_'+key]={'title':str(entries[-1].get('title',''))[:60],'text':str(entries[-1].get('text',''))[:240]}
        result['inventory' if item.get('carried') else 'visible_items'].append(row)
    designs=source.get('room_designs')
    current=next((d for d in designs[:32] if isinstance(d,dict) and d.get('room_id')==source.get('room_id')),None) if isinstance(designs,list) else None
    if current:
        result['room_design']={k:str(current.get(k) or '')[:100] for k in ('room_name','lighting_label','curtains_label')}
        result['room_design']['furniture']=[{k:str(f.get(k) or '')[:60] for k in ('name','position_label')} for f in current.get('furniture',[])[:12] if isinstance(f,dict)]
        history=current.get('history')
        result['room_design']['recent_changes']=[{k:(v if k=='occurred_at' and type(v) in (int,float) else str(v)[:120]) for k,v in c.items() if k in {'name','before','after','occurred_at'}} for c in history[-3:] if isinstance(c,dict)] if isinstance(history,list) else []
    action=source.get('action')
    projects=source.get('projects')
    if isinstance(projects,list):
        result['projects']=[{k:(str(v)[:120] if isinstance(v,str) else v) for k,v in p.items() if k in {'id','title','status','stage','stage_label','draft_parts','revised_parts','target_parts','updated_at','paused_by'} and isinstance(v,(str,int,float))} for p in projects[:12] if isinstance(p,dict)]
    if action:
        result['action']={k:action.get(k) for k in ('status','label','index','step_ends_at')}
        result['action']['total_steps']=len(action.get('steps',[]))
    last=source.get('last_action')
    if last:
        result['last_action']={k:last.get(k) for k in ('status','reason','finished_at')}
    result['receipts']=[]
    for receipt in source.get('receipts',[])[:6]:
        if receipt.get('kind') not in {'step.completed','action.completed','action.failed','action.cancelled'}:
            continue
        row={k:receipt.get(k) for k in ('event_id','kind','operation','occurred_at','reason','step','origin') if receipt.get(k) is not None}
        if isinstance(receipt.get('result'),dict):
            row['result']={k:v for k,v in receipt['result'].items() if k in {'name','kind','page','pages','servings','capacity','carried','open','wetness','powered','note_count','document_count','gaming_minutes','organize_count','completed_sessions','last_mode','before','after','project_id','project_title','project_stage','project_status','draft_parts','revised_parts','target_parts','section'}}
            saved=receipt['result'].get('saved_record')
            if isinstance(saved,dict):
                row['result']['saved_record']={'title':str(saved.get('title',''))[:60],'text':str(saved.get('text',''))[:240]}
        result['receipts'].append(row)
        if len(result['receipts'])>=3:
            break
    causal=snapshot.get('causal_life')
    if isinstance(causal,dict) and causal.get('viewer')=='actor':
        ecology=snapshot.get('ecology') or {}
        result['causal_life']={
            'body':{k:v for k,v in (causal.get('body') or {}).items() if k in {'label','level','basis','energy','satiety','hydration'}},
            'agenda':[{k:p.get(k) for k in ('activity','start','end','status','reason')} for p in causal.get('agenda',[])[-8:] if isinstance(p,dict)],
            'prepared_food':[{'title':str(p.get('title',''))[:60],'kind':p.get('kind')} for p in ecology.get('prepared',[])[:6] if isinstance(p,dict)],
            'consumed_food':[{'title':str(p.get('title',''))[:60],'consumed_at':p.get('consumed_at')} for p in ecology.get('consumed',[])[-3:] if isinstance(p,dict)],
            'invitations':[{k:p.get(k) for k in ('status','when','reason')} for p in causal.get('invitations',[])[-4:] if isinstance(p,dict)],
            'perceived_events':[{'id':p.get('id'),'kind':p.get('kind'),'text':str(p.get('summary',''))[:240]} for p in causal.get('events',[]) if isinstance(p,dict) and p.get('kind') in {'speech.perceived','invitation.proposed','invitation.responded','visit.arrived','visit.completed'}][:6],
        }
    mailbox = source.get('mailbox') if isinstance(source.get('mailbox'), dict) else snapshot.get('mailbox')
    if isinstance(mailbox, dict):
        recent_letters = []
        for entry in (mailbox.get('recent') or mailbox.get('items') or mailbox.get('letters') or ())[:4]:
            if not isinstance(entry, dict):
                continue
            row = {}
            for k, lim in (('id', 48), ('from', 48), ('sender_name', 48), ('kind', 32), ('category', 32), ('title', 80), ('summary', 140), ('status', 32)):
                val = entry.get(k)
                if isinstance(val, str) and val.strip():
                    row[k] = val.strip()[:lim]
            if row:
                recent_letters.append(row)
        mb_out = {}
        unread = mailbox.get('unread_count')
        if isinstance(unread, int) and not isinstance(unread, bool) and unread >= 0:
            mb_out['unread_count'] = unread
        if recent_letters:
            mb_out['recent'] = recent_letters
        if mb_out:
            result['mailbox'] = mb_out
    keepsakes = source.get('keepsakes') if isinstance(source.get('keepsakes'), list) else snapshot.get('keepsakes')
    if isinstance(keepsakes, list):
        ks_rows = []
        for item in keepsakes[:6]:
            if not isinstance(item, dict):
                continue
            row = {}
            for k, lim in (('id', 48), ('title', 80), ('name', 80), ('kind', 32), ('slot', 32), ('sender_name', 48), ('from', 48), ('summary', 140)):
                val = item.get(k)
                if isinstance(val, str) and val.strip():
                    row[k] = val.strip()[:lim]
            if row:
                ks_rows.append(row)
        if ks_rows:
            result['keepsakes'] = ks_rows
    garden = source.get('garden') if isinstance(source.get('garden'), dict) else snapshot.get('garden')
    if isinstance(garden, dict):
        g_out = {}
        for k, lim in (('status', 48), ('season', 32), ('summary', 160), ('next_event', 80)):
            val = garden.get(k)
            if isinstance(val, str) and val.strip():
                g_out[k] = val.strip()[:lim]
        plants = []
        for plant in (garden.get('plants') or garden.get('plots') or ())[:6]:
            if not isinstance(plant, dict):
                continue
            p_row = {}
            for k, lim in (('name', 48), ('stage', 32), ('status', 32)):
                val = plant.get(k)
                if isinstance(val, str) and val.strip():
                    p_row[k] = val.strip()[:lim]
            if isinstance(plant.get('watered'), bool):
                p_row['watered'] = plant['watered']
            if p_row:
                plants.append(p_row)
        if plants:
            g_out['plants'] = plants
        if g_out:
            result['garden'] = g_out
    residents = source.get('residents') if isinstance(source.get('residents'), list) else snapshot.get('residents')
    if isinstance(residents, list):
        res_rows = []
        for resident in residents[:8]:
            if not isinstance(resident, dict):
                continue
            r_row = {}
            for k, lim in (('name', 48), ('status', 32), ('location', 48), ('relation', 48), ('mood', 32), ('activity', 60)):
                val = resident.get(k)
                if isinstance(val, str) and val.strip():
                    r_row[k] = val.strip()[:lim]
            if r_row:
                res_rows.append(r_row)
        if res_rows:
            result['residents'] = res_rows
    return result
