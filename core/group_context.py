"""Bounded platform facts, separate from member speech and reply triggers."""
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math

EVENT_WINDOW_SECONDS = 600.0


@dataclass(frozen=True)
class GroupContextEvent:
    event_id: str
    kind: str
    member_id: str
    member_name: str
    operator_id: str
    subtype: str
    occurred_at: float
    observed_at: float
    received_at: float

    def payload(self, cutoff):
        return {"event_id": self.event_id, "kind": self.kind, "source": "platform_notice",
                "member_id": self.member_id, "member_name": self.member_name,
                "operator_id": self.operator_id, "subtype": self.subtype,
                "occurred_at": self.occurred_at, "age_seconds": max(0.0, cutoff - self.observed_at)}


def member_notice(event, parsed, *, now, wall_time):
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if not isinstance(raw, Mapping) or raw.get("post_type") != "notice":
        return None
    kinds = {"group_increase": "member_joined", "group_decrease": "member_left"}
    kind = kinds.get(raw.get("notice_type"))
    if not kind or str(raw.get("group_id", "")) != str(parsed.group_id):
        return None
    uid = str(raw.get("user_id", ""))
    if not uid.isdecimal() or uid == "0" or len(uid) > 32 or uid != str(parsed.sender_id):
        return None
    stamp = raw.get("time", wall_time)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        return None
    age = wall_time - stamp
    if age > EVENT_WINDOW_SECONDS or age < -60:
        return None
    operator = str(raw.get("operator_id", ""))
    operator = operator if operator.isdecimal() and operator != "0" and len(operator) <= 32 else ""
    subtype = str(raw.get("sub_type", ""))
    subtype = subtype if subtype in {"approve", "invite", "leave", "kick", "kick_me"} else "unknown"
    identity = [str(parsed.group_id), kind, uid, operator, subtype,
                raw.get("time", str(parsed.message_id))]
    event_id = "notice:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
    name = str(parsed.sender_name or "")[:80]
    return GroupContextEvent(event_id, kind, uid, name if name != uid else "", operator,
                             subtype, float(stamp), now - max(0.0, age), now)


def remember_notice(runtime, notice):
    if any(row.event_id == notice.event_id for row in runtime.group_events):
        return False
    runtime.group_events.append(notice)
    return True


def select_group_events(runtime, cutoff, *, member_ids=()):
    if getattr(runtime, "scope_kind", "group") != "group":
        return ()
    eligible = sorted((row for row in getattr(runtime, "group_events", ())
                       if row.received_at <= cutoff and 0 <= cutoff - row.observed_at <= EVENT_WINDOW_SECONDS),
                      key=lambda row: (row.observed_at, row.received_at))
    preferred = set(member_ids)
    rows = sorted(eligible, key=lambda row: (row.member_id in preferred, row.observed_at), reverse=True)[:4]
    rows.sort(key=lambda row: (row.observed_at, row.received_at))
    dag = getattr(runtime, "dag", None)
    nodes = dag.get_recent_nodes(limit=80) if dag is not None else ()
    names = {node.user_id: str(node.metadata.get("sender_name", ""))[:80] for node in nodes
             if 0 <= cutoff - node.timestamp <= EVENT_WINDOW_SECONDS and node.metadata.get("sender_name")}
    return tuple(replace(row, member_name=names.get(row.member_id, row.member_name)) for row in rows)


def export_group_events(runtime, now):
    return [asdict(row) for row in getattr(runtime, "group_events", ())
            if 0 <= now - row.observed_at <= EVENT_WINDOW_SECONDS]


def restore_group_events(runtime, rows, *, shift, now):
    if runtime.scope_kind != "group" or not isinstance(rows, list):
        return
    for row in rows[-32:]:
        try:
            if not isinstance(row, dict) or row.get("kind") not in {"member_joined", "member_left"}:
                continue
            if not all(isinstance(row.get(key), str) for key in
                       ("event_id", "member_id", "member_name", "operator_id", "subtype")):
                continue
            if not row['event_id'].startswith('notice:') or len(row['event_id']) != 31:
                continue
            if not row['member_id'].isdecimal() or row['member_id'] == '0' or len(row['member_id']) > 32:
                continue
            if row['operator_id'] and (not row['operator_id'].isdecimal() or len(row['operator_id']) > 32):
                continue
            if row['subtype'] not in {'approve', 'invite', 'leave', 'kick', 'kick_me', 'unknown'}:
                continue
            if any(type(row.get(key)) not in (float, int) or not math.isfinite(row[key])
                   for key in ('observed_at', 'occurred_at', 'received_at')):
                continue
            observed = row['observed_at'] + shift
            if not 0 <= now - observed <= EVENT_WINDOW_SECONDS:
                continue
            notice = GroupContextEvent(row['event_id'], row['kind'], row['member_id'], row['member_name'][:80],
                                       row['operator_id'], row['subtype'], row['occurred_at'], observed,
                                       row['received_at'] + shift)
            remember_notice(runtime, notice)
        except (KeyError, TypeError, ValueError):
            continue
