"""Optional, cached expression adaptation. Never calls a model or sends a message."""
import json
import re
from copy import deepcopy

from . import dynamics_learning as bridge
from .scene_learning import labels, report

GUIDANCE = '''expression_style是本群已审查的表达习惯，明确允许你在自己的性格内采用这些说话形式，而不只是描述对方。
保留身份、立场和自身喜恶；安静或温和不等于必须严肃、完整、正式。日常闲聊可以短句、碎句、省略、语气词、轻吐槽，自然收住。
结合本轮具体内容，少复述、少无必要总结和建议、少习惯性反问；不是每句都缩短或装俏皮。不得照搬示例的事实或经历。
示例是加工过的写法参考，不是固定台词。不要连续使用同一个口头禅、开头、结尾或相同句式；没有相称的梗就不用梗。
认真求助、倾诉、纠正和明确边界优先；保留必要信息、代码和格式。语义不明时只调整措辞，不擅自添加玩笑。'''


def corrections(query):
    if any(mark in query for mark in ('“', '”', '「', '」', '"', '?', '？')):
        return []
    first = re.split(r'[，,。！!\n]', query.strip(), maxsplit=1)[0].strip()
    groups = [({'太正式', '太正式了', '你太正式了', '太正经了', '别这么正经', '别那么正式'}, '这轮直接用自然口语回应。不解释或评价自己刚才的口吻，不宣布调整计划。'),
              ({'别解释', '不用解释', '不用解释这么多', '别说那么多'}, '停止展开当前说明，只需简短接住，不扩大成拒绝说话。'),
              ({'别每次都问', '不要老问', '别老反问', '不用每次都问'}, '本轮不追加习惯性追问。无需解释、安抚或保证以后如何。'),
              ({'不要老用这个词', '别老用这个词'}, '这轮换个说法。不重复该词，不自责、不解释，也不声称永久记住；不编造具体禁词。')]
    return [instruction for phrases, instruction in groups if first in phrases]


def validate(packet, scope):
    if (not isinstance(packet, dict) or packet.get('schema_version') != 1 or ':GroupMessage:' not in scope['scope']
            or any(packet.get(k) != scope.get(k) for k in ('scope', 'persona', 'member'))):
        raise ValueError('invalid expression identity')
    rows = packet.get('items')
    if not isinstance(rows, list) or len(rows) > 1 or len(json.dumps(packet, ensure_ascii=False, allow_nan=False)) > 4000:
        raise ValueError('expression budget exceeded')
    for r in rows:
        if (not isinstance(r, dict) or r.get('category') != 'expression_style' or r.get('member') != ''
                or r.get('source_umo') != scope['scope'] or not isinstance(r.get('source_id'), str)
                or type(r.get('revision')) is not int or r['revision'] < 1 or not isinstance(r.get('value'), dict)
                or not isinstance(r.get('selection'), dict) or not isinstance(r.get('source_uids'), list)
                or r.get('provenance_complete') is not True):
            raise ValueError('invalid expression item')
    if packet.get('versions') != [r['source_id'] for r in rows]:
        raise ValueError('invalid expression versions')
    return deepcopy(packet)


def current(source, packet):
    try:
        if 'expression_style_v1' not in source.learning_capabilities().get('capabilities', []):
            return False
        scope = {k: packet[k] for k in ('scope', 'persona', 'member')}
        if not packet.get('items'):
            return True
        row = packet['items'][0]
        frozen = {k: row[k] for k in ('source_id', 'revision', 'selection')}
        latest = validate(source.get_expression_style_context(**scope, frozen=frozen), scope)
        return latest['items'] == packet['items']
    except Exception:
        return False


def prepare(prompt, runtime, item):
    capture = bridge.capture_for(runtime, item)
    if capture is None or getattr(item, 'private_direct', False):
        return prompt
    host, source = capture.host(), capture.source()
    try:
        if host is None or source is None or bridge.provider(host) is not source or 'expression_style_v1' not in source.learning_capabilities().get('capabilities', []):
            return prompt
        payload = json.loads(prompt)
        if payload.get('response_plan', {}).get('action') not in {'reply', 'acknowledge', 'close', 'clarify'}:
            return prompt
        if not getattr(capture, 'expression_prepared', False):
            capture.expression_prepared = True
            packet = validate(source.get_expression_style_context(**capture.scope,
                scene=labels(payload.get('turn_signals') or payload.get('interaction_understanding') or {}), action=payload['response_plan']['action'], query=capture.query), capture.scope)
            trace = {k: capture.scope[k] for k in ('scope', 'persona', 'member')}
            trace['turn_id'] = capture.base['turn_id']
            capture.expression_packet = {**packet, 'trace': trace, 'corrections': corrections(capture.query)}
            report(source, {**trace, 'stage': 'selected', 'refs': [{k:r[k] for k in ('source_id', 'revision', 'category')} for r in packet['items']]})
        return bridge.augment_reply(prompt, runtime, item)
    except Exception:
        return prompt


def budget_examples(profiles, scenes, styles):
    """Two total examples across all channels, without mutating frozen input/guards."""
    profiles, scenes, styles = deepcopy(profiles), deepcopy(scenes), deepcopy(styles)
    budget = 2
    seen = set()
    def distinct(values, key, limit):
        selected = []
        for v in values:
            signature = str(v.get(key, '')).strip()
            if signature and signature not in seen and len(selected) < limit:
                seen.add(signature)
                selected.append(v)
        return selected
    if styles:
        limit = 1 if scenes else 2
        for row in styles:
            examples = distinct(row['value'].get('examples', []), 'text', min(limit, budget))
            row['value']['examples'] = examples
            budget -= len(examples)
    for row in scenes:
        row['value']['examples'] = distinct(row['value'].get('examples', []), 'response', budget)
        budget -= len(row['value']['examples'])
    kept = []
    for row in profiles:
        if row.get('category') == 'expression_example':
            signature = str(row.get('value', {}).get('expression', '')).strip()
            if budget <= 0 or signature in seen:
                continue
            seen.add(signature)
            budget -= 1
        kept.append(row)
    return kept, scenes, styles
