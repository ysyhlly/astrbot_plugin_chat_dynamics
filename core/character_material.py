"""Shared evidence selection for Jev and visible expression."""
import math
from .semantic_excerpt import excerpt


def energy_context(snapshot):
    raw = snapshot.get('energy') or {}
    stale = bool(snapshot.get('stale'))
    level = raw.get('level', 'unknown') if not stale else 'unknown'
    if level not in {'unknown','normal','recovering','recovered'}:
        level = 'unknown'
    result = {'level':level,'stale':stale,'fact_kind':'subjective'}
    for key in ('confirmed_activity_seconds','previous_rest_seconds'):
        value = raw.get(key)
        if not stale and type(value) in (int,float) and math.isfinite(value) and value >= 0:
            result[key] = round(min(value, 86400))
    return result


def select_commitments(rows, speaker_id='', now=0, limit=4):
    """Do not discard overdue promises or let terminal history consume slots."""
    statuses = {'committed':0,'conditional':1,'proposed':2}
    valid = [r for r in rows if isinstance(r,dict) and r.get('status') in statuses]
    def ranking(row):
        participants = row.get('participant_ids') or []
        related = bool(speaker_id and (speaker_id == row.get('subject_id') or speaker_id in participants))
        due = row.get('due_at')
        due = float(due) if type(due) in (int,float) and math.isfinite(due) else float('inf')
        return (statuses[row['status']], 0 if related else 1, due, str(row.get('commitment_id','')))
    selected=[]
    for row in sorted(valid,key=ranking)[:limit]:
        item={k:v for k,v in row.items() if k in {'commitment_id','status','due_at','reminder_id','participant_ids'}}
        item['description']=excerpt(row.get('description',''),220)
        due=row.get('due_at')
        item['overdue']=bool(type(due) in (int,float) and math.isfinite(due) and due < now)
        selected.append(item)
    return selected
