"""Bounded weather context, without credentials, coordinates or narration orders."""
import math

WEATHER_GUIDANCE = (
    "environment 是角色所处世界的天气背景。参考城市只提供气象条件，不代表角色或聊天者真实位于该城市。"
    "先回应当前话题；天气仅在谈出行、穿着、体感、当前活动或对方明确询问时自然影响回复。"
    "无关话题不提天气，不每轮报温度、城市、数据源，也不固定用天气开场。"
    "天气不会自动授权主动联系。室内和睡眠不因室外小变化立即改变；不要编造淋雨、换衣、关窗等已经发生的动作。"
    "current 是带来源的当前条件参考，forecast 只是未来预报；available=false 或 stale 时不推断天气。"
)


def activity_text(snapshot):
    text=str(snapshot.get('activity',''))
    # Older providers without a structured field retain their legacy summary.
    if isinstance(snapshot.get('environment'),dict):text=text.split('；环境参考（',1)[0]
    return text


def _number(value):
    return value if type(value) in (int,float) and math.isfinite(value) else None


def _point(value):
    if not isinstance(value,dict):return None
    temperature=_number(value.get('temperature'));stamp=_number(value.get('time'))
    if temperature is None or stamp is None:return None
    point={k:_number(value.get(k)) for k in ('temperature','apparent_temperature','wind_speed','precipitation','precipitation_probability','time')}
    point={k:v for k,v in point.items() if v is not None}
    point['condition']=str(value.get('label','未知'))[:40]
    point['is_day']=value.get('is_day') if type(value.get('is_day')) is bool else None
    hours=_number(value.get('precipitation_hours'))
    if hours is not None and hours>0:point['precipitation_hours']=hours
    return point


def environment_context(snapshot,now=0,forecast_limit=4):
    env=snapshot.get('environment')
    if not isinstance(env,dict):return None
    result={'available':False,'status':str(env.get('status','unknown'))[:24],
            'fact_kind':'fiction' if env.get('fact_kind')=='fiction' else 'external_weather',
            'source':str(env.get('source',''))[:60],'location_reference':str(env.get('location',''))[:100]}
    if env.get('available') is not True or env.get('stale') or env.get('status')!='ready':return result
    current=_point(env.get('current'))
    if current is None:return result
    # A frozen bundle is evaluated at its capture time. Avoid retaining old
    # weather after source TTL when this function is used on a later snapshot.
    expires=_number(env.get('expires_at'))
    if now and expires is not None and expires<=now:return dict(result,status='stale')
    result.update(available=True,current=current,timezone=str(env.get('timezone',''))[:80])
    result['units']={'temperature':'°C','wind_speed':'km/h','precipitation':'mm; period is precipitation_hours when supplied',
                     'precipitation_probability':'%','time':'Unix seconds'}
    for field in ('sunrise','sunset'):
        value=_number(env.get(field))
        if value is not None:result[field]=value
    points=[_point(p) for p in (env.get('forecast') or [])[:48]]
    points=sorted([p for p in points if p and (not now or now<p['time']<=now+86400)],key=lambda p:p['time'])
    selected=[]
    if points:
        selected.append(points[0])
        wet=next((p for p in points if p['time']<=now+21600 and (p.get('precipitation',0)>.1 or p.get('precipitation_probability',0)>=60)),None)
        if wet and wet not in selected:selected.append(wet)
        for hours in (3,6,12,24):
            point=min(points,key=lambda p:abs(p['time']-(now+hours*3600)))
            if point not in selected:selected.append(point)
    result['forecast']=sorted(selected[:forecast_limit],key=lambda p:p['time'])
    return result
