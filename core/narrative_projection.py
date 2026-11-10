"""Bound model input while keeping the complete evidence in the journal."""
import copy
import json
from .semantic_excerpt import excerpt
from .character_material import energy_context, select_commitments
from .character_view import historical_item, speaker_snapshot
from .narrative import select_evidence_slots
from . import member_boundary


_SCOPE_KEYS = ("platform", "umo", "persona_id", "instance_id")
_AFFECTION_SOURCE = "affection:narrative:v1:"
_CURRENT_INTERACTION_GUIDANCE = (
    "按本轮对象、方向和实际言行判断：若匹配的当前关系线索显示稳定亲近，且本人本轮直接向安安表达喜欢、关心、撒娇或确认关系，"
    "这是有内容的交流邀请，应优先接话并回应其在意之处；发言面向他人则不抢话。普通善意也可以自然回应亲近、分享在意；"
    "明确针对安安本人的辱骂或恶意挑衅可以坚定回应具体言行，不升级为人身羞辱。"
    "引用他人攻击安安并维护她，不等于当前发言者攻击安安；反馈和合理批评也不等于攻击。"
    "拒绝某个身体动作只说明本轮不愿做该动作，不连带否定关系或过去亲近；不因旧拒绝预先回避，若此刻愿意可自然接受靠近。意图或归属不清时先澄清。"
    "历史中的bot反应只是当时记录，不是本轮口吻规范。"
)


def _same_scope(item_scope, bundle_scope):
    if not isinstance(item_scope, dict) or not isinstance(bundle_scope, dict):
        return False
    if not item_scope.get("umo") or item_scope.get("umo") != bundle_scope.get("umo"):
        return False
    for key in _SCOPE_KEYS:
        expected = bundle_scope.get(key)
        if expected and item_scope.get(key) != expected:
            return False
    return True


def _unknown_relationship_cue(speaker_id, reason):
    return {
        "status": "unknown",
        "unknown": True,
        "speaker_id": (speaker_id or "unknown")[:128],
        "text": reason,
    }


def _current_relationship_cue(managed, items):
    speaker_id = str(managed.get("speaker_id") or "")[:128]
    scope = managed.get("scope")
    candidates = [row for row in items
                  if isinstance(row, dict)
                  and row.get("provider") == "self_learning"
                  and isinstance(row.get("source_id"), str)
                  and row["source_id"].startswith(_AFFECTION_SOURCE)
                  and str(row.get("subject_id") or "") == speaker_id
                  and speaker_id
                  and isinstance(scope, dict)
                  and row.get("source_umo") == scope.get("umo")
                  and _same_scope(row.get("scope"), scope)
                  and isinstance(row.get("unknown"), bool)]
    if not candidates:
        return _unknown_relationship_cue(
            speaker_id, "没有可验证的当前成员关系线索；不推断好感档位，也不借用其他成员的线索。")
    if len(candidates) > 1:
        times = [row.get("occurred_at") for row in candidates]
        if not all(isinstance(value, (int, float)) for value in times):
            return _unknown_relationship_cue(
                speaker_id, "当前成员存在多条无法排序的关系线索；不推断好感档位。")
        latest_time = max(times)
        latest = [row for row in candidates if row["occurred_at"] == latest_time]
        if len(latest) > 1:
            signatures = {(row.get("unknown"), row.get("fact_kind"),
                           excerpt(row.get("text", ""), 500)) for row in latest}
            if len(signatures) > 1:
                return _unknown_relationship_cue(
                    speaker_id, "同一时间存在互相冲突的关系线索；不推断好感档位。")
        candidates = latest
    row = candidates[-1]
    return {
        "status": "subjective_current_tendency",
        "unknown": row["unknown"],
        "provider": row["provider"],
        "source_id": row["source_id"][:240],
        "subject_id": str(row["subject_id"])[:128],
        "source_umo": str(row["source_umo"])[:180],
        "scope": {key: str(row["scope"][key])[:180] for key in _SCOPE_KEYS if key in row["scope"]},
        "fact_kind": row.get("fact_kind"),
        "occurred_at": row.get("occurred_at"),
        "text": excerpt(row.get("text", ""), 500),
        "interpretation": "表示安安与当前成员关系的亲近/治愈阶段；高阶段应体现为更安心、接受关心和靠近，不每轮重置防备。unknown原值只标记这是角色主观倾向，不代表对方真实感受或身体同意，也不使匹配阶段失效。",
    }


def _current_interaction_basis(managed):
    speaker_id = str(managed.get("speaker_id") or "")[:128]
    scope = managed.get("scope")
    raw_events = managed.get("events", [])
    events = raw_events if isinstance(raw_events, list) else [raw_events]
    matched = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "message.received":
            continue
        if str(event.get("actor_id") or "") != speaker_id or not speaker_id:
            continue
        if not isinstance(scope, dict) or not _same_scope(event.get("scope"), scope):
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        directed = data.get("directed_to_bot")
        matched.append({
            "event_id": event["event_id"][:128] if isinstance(event.get("event_id"), str) else "unknown",
            "directed_to_bot": directed if isinstance(directed, bool) else "unknown",
        })
    return {
        "status": "matched" if matched else "unknown",
        "speaker_id": speaker_id or "unknown",
        "scope_umo": str(scope["umo"])[:180] if isinstance(scope, dict) and scope.get("umo") else "unknown",
        "current_events": matched[-4:],
        "guidance": _CURRENT_INTERACTION_GUIDANCE,
    }


def reply_material(managed, budget=8000):
    snapshot = speaker_snapshot(managed.get("snapshot", {}), managed.get("speaker_id", ""), managed.get("captured_at", 0))
    state = {k: snapshot[k] for k in ("revision", "scope_revision", "activity", "availability",
             "subjective_state", "summary", "valid_from", "valid_until", "stale", "energy") if k in snapshot}
    for k in ("activity", "subjective_state", "summary"):
        if k in state:
            state[k] = excerpt(state[k], 300)
    state['energy'] = energy_context(snapshot)
    if snapshot.get("humanlike"):
        state["humanlike"] = snapshot["humanlike"]
    state["commitments"] = select_commitments(snapshot.get('commitments', []), managed.get('speaker_id', ''), managed.get('captured_at', 0), limit=6)
    out = {"snapshot": state, "source_scope": managed["scope"], "speaker_id": managed.get("speaker_id"),
           "context_items": [], "world_items": [], "purpose": managed.get("purpose", "reply"),
           "evidence_detail": "available_in_local_turn_journal"}
    availability = managed.get("availability")
    if isinstance(availability, dict):
        statuses = {"ready", "partial", "empty", "timeout", "unavailable", "invalid", "pending", "deferred"}
        status = availability.get("livingmemory_evidence_status")
        status = status if status in statuses else "unknown"
        relationship = availability.get("self_learning")
        out["memory_retrieval"] = {
            "long_term_status": status,
            "relationship_status": "available" if relationship is True else "unavailable" if relationship is False else "unknown",
        }
        recall_status = availability.get("livingmemory_recall_status")
        if recall_status in statuses:
            out["memory_retrieval"]["recall_status"] = recall_status
        if status != "ready" or relationship is not True:
            out["memory_retrieval"]["guidance"] = (
                "这些状态只说明本轮资料取用情况；检索超时、失败、部分返回或没有匹配，"
                "不表示过去没互动、承诺已取消或关系归零。先依据当前可验证资料回应；"
                "具体往事缺证据时可说这次暂时没找到对应记录并询问一个线索，"
                "不据此断言以前没聊过，也不编造回忆。关系资料暂不可用时不推断陌生或亲近程度下降。")
    evidence_view = managed.get("evidence_view")
    raw_evidence_slots = {}
    if isinstance(evidence_view, dict) and evidence_view.get("schema_version") == 1:
        raw_evidence_slots = evidence_view.get("slots") if isinstance(evidence_view.get("slots"), dict) else {}
        out["evidence_revision"] = evidence_view.get("correction_revision")
        diagnostics = evidence_view.get("diagnostics")
        if isinstance(diagnostics, dict):
            out["evidence_diagnostics"] = {key: diagnostics.get(key) for key in
                ("materials_total", "materials_cached", "materials_queued", "materials_pending",
                 "materials_omitted_unclassified", "materials_missing_snapshot_identity",
                 "source_refs_rejected", "projection_version") if key in diagnostics}
    source_items = managed.get("context_items", [])
    if not isinstance(source_items, list):
        source_items = []
    source_items = [row for row in source_items if not (
        isinstance(row, dict) and row.get("provenance_kind") == "evidence_view_atom")]
    # Current, structurally attributed relationship evidence is a separate
    # priority cue; retained history remains evidence rather than a tone order.
    out["current_relationship_cue"] = _current_relationship_cue(managed, source_items)
    out["current_interaction_basis"] = _current_interaction_basis(managed)
    expression=managed.get('expression_context',{})
    if not isinstance(expression, dict):
        expression = {}
    from .rest_behavior import rest_reply_context
    rest_context = rest_reply_context(snapshot, expression)
    if rest_context:
        out['rest_behavior'] = rest_context
    if expression.get('guidance'):
        out['expression_context']={k:expression[k] for k in ('fact_kind','recent_replies','ellipsis_prefixes','guidance') if k in expression}
        out['expression_context']['guidance'] = excerpt(out['expression_context'].get('guidance', ''), 500)
        recent_replies = out['expression_context'].get('recent_replies', [])
        if not isinstance(recent_replies, (list, tuple)):
            recent_replies = []
        prefixes = out['expression_context'].get('ellipsis_prefixes', [])
        if not isinstance(prefixes, (list, tuple)):
            prefixes = []
        out['expression_context']['recent_replies'] = [excerpt(value, 240) for value in recent_replies[:4]]
        out['expression_context']['ellipsis_prefixes'] = [excerpt(value, 80) for value in prefixes[:12]]
    if snapshot.get("recent_interaction"):
        state["recent_interaction"] = snapshot["recent_interaction"]
    out["system_receipts"] = managed.get("system_receipts", [])
    slot_budget = max(0, int(budget) - len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) - 320)
    out["evidence_slots"], slot_diagnostics = select_evidence_slots(raw_evidence_slots, slot_budget)
    out["evidence_slot_diagnostics"] = slot_diagnostics
    # Retain the small set of current world facts before spending the remaining
    # budget on longer historical context; current relationship cues already
    # have priority above both.
    for row in managed.get("character", {}).get("world_items", []):
        clean = {k: excerpt(v, 600) if k in {"text", "content"} else str(v)[:240] for k,v in row.items() if k in {"text", "content", "source_id", "fact_kind"}}
        out["world_items"].append(clean)
        if len(json.dumps(out, ensure_ascii=False)) > budget:
            out["world_items"].pop()
    items = [historical_item(row) for row in source_items]
    items.sort(key=lambda r: (0 if r.get("subject_id") == managed.get("speaker_id") and r.get("subject_id") else 1))
    for row in items:
        clean = {k: (v[:500] if isinstance(v, str) else v) for k,v in row.items()
                 if k in {"text", "source_id", "provider", "fact_kind", "unknown", "provenance_kind", "subject_id", "occurred_at"}
                 and isinstance(v, (str, int, float, bool, type(None)))}
        clean["text"] = excerpt(row.get("text", ""), 500)
        clean["evidence_count"] = len(row.get("evidence", []))
        out["context_items"].append(clean)
        if len(json.dumps(out, ensure_ascii=False)) > budget:
            out["context_items"].pop()
    # Keep complete current evidence and boundaries. Drop optional prose first;
    # if the required atoms alone exceed budget, return them intact and report it.
    while len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) > budget and out["context_items"]:
        out["context_items"].pop()
    while len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) > budget and out["world_items"]:
        out["world_items"].pop()
    if len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) > budget:
        out.pop("expression_context", None)
    if len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) > budget and state.get("summary"):
        state["summary"] = ""
    if len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) > budget:
        required = sum(len(out["evidence_slots"].get(name, [])) for name in
                       ("current_evidence", "commitments_and_boundaries"))
        if required:
            out["evidence_slot_diagnostics"]["required_slots_over_budget"] = True
            out["evidence_slot_diagnostics"]["required_atoms"] = required
    return out


def filter_reply_material(boundary_host, event, material, managed):
    """Filter every member-derived text section through the public boundary API.

    AutonomousBlocking filters only its documented frame collections. This
    flattens dynamic world items, evidence atoms and optional snapshot prose
    into temporary context frames, then restores only frames returned by the
    service. Static, explicitly typed Worldbook settings stay in world_items.
    Source IDs and actor attribution are used only during this filter pass.
    """
    if not isinstance(material, dict) or not isinstance(managed, dict):
        return {}, member_boundary.resolve_state(boundary_host, event)
    result = copy.deepcopy(material)
    scope = managed.get("scope") if isinstance(managed.get("scope"), dict) else {}
    speaker_id = str(managed.get("speaker_id") or "").strip()
    frames = []
    routes = {}
    sequence = 0

    def strings(value):
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return sorted({str(item).strip() for item in values
                       if isinstance(item, (str, int)) and str(item).strip()})

    def same_source_scope(row):
        owner = row.get("scope") if isinstance(row.get("scope"), dict) else {}
        return bool(owner) and all(owner.get(key) == scope.get(key)
                                   for key in _SCOPE_KEYS)

    def add_frame(route, text, source_ids=(), *, subject_id="", complete=False):
        nonlocal sequence
        token = str(sequence)
        sequence += 1
        source_ids = strings(source_ids)
        subject_id = str(subject_id or "").strip()
        if subject_id:
            source_ids = sorted(set(source_ids) | {subject_id})
        frame = {"_cd_boundary_token": token, "text": str(text or ""),
                 "provenance_complete": bool(complete and source_ids)}
        if subject_id:
            frame["subject_id"] = subject_id
        if source_ids:
            frame["source_uids"] = source_ids
            if len(source_ids) == 1:
                frame["author_uid"] = source_ids[0]
        routes[token] = route
        frames.append(frame)
        return token

    # Existing social/profile items have an explicit target and exact scope.
    raw_context_items = managed.get("context_items", [])
    raw_context_items = raw_context_items if isinstance(raw_context_items, list) else []
    raw_by_source = {}
    for row in raw_context_items:
        if isinstance(row, dict) and row.get("source_id"):
            raw_by_source.setdefault(str(row["source_id"]), []).append(row)
    context_routes = []
    for index, row in enumerate(result.get("context_items", [])):
        if not isinstance(row, dict):
            continue
        candidates = raw_by_source.get(str(row.get("source_id")), [])
        candidates = [source for source in candidates
                      if source.get("provider") == row.get("provider")
                      and source.get("subject_id") == row.get("subject_id")]
        source = candidates[0] if len(candidates) == 1 else {}
        target = str(source.get("subject_id") or row.get("subject_id") or "").strip()
        refs = source.get("source_refs", source.get("evidence_refs", []))
        ref_sources = [ref.get("author_id", ref.get("actor_id")) for ref in refs
                       if isinstance(ref, dict)] if isinstance(refs, list) else []
        for key in ("source_uids", "authors"):
            ref_sources.extend(strings(source.get(key, [])))
        ref_sources.append(source.get("author_uid"))
        provider = str(source.get("provider") or row.get("provider") or "")
        api_targeted = (provider in {"self_learning", "portrayal"} and target
                        and same_source_scope(source)
                        and source.get("source_umo", scope.get("umo")) == scope.get("umo"))
        explicit_complete = source.get("provenance_complete")
        complete = bool(target and (explicit_complete is True if explicit_complete is not None
                                    else api_targeted))
        token = add_frame(("context", index), row.get("text", ""), ref_sources,
                          subject_id=target, complete=complete)
        context_routes.append((index, token))

    # Worldbook's typed, scope-bound world settings are static. All other
    # selected world rows are dynamic and must carry member provenance or be
    # rejected when any shield is active.
    raw_world_items = managed.get("character", {}).get("world_items", [])
    raw_world_items = raw_world_items if isinstance(raw_world_items, list) else []
    raw_world_by_source = {}
    for row in raw_world_items:
        if isinstance(row, dict) and row.get("source_id"):
            raw_world_by_source.setdefault(str(row["source_id"]), []).append(row)
    static_world_tokens = set()
    world_routes = []
    for index, row in enumerate(result.get("world_items", [])):
        if not isinstance(row, dict):
            continue
        candidates = raw_world_by_source.get(str(row.get("source_id")), [])
        source = candidates[0] if len(candidates) == 1 else {}
        source_id = str(source.get("source_id") or "")
        is_static = (source_id.startswith("worldbook:")
                     and source.get("fact_kind") == "world_setting"
                     and source.get("confidence") == "established_setting"
                     and source.get("unknown") is False
                     and source.get("provenance_complete") is not False
                     and same_source_scope(source)
                     and not source.get("subject_id"))
        if is_static:
            static_world_tokens.add(index)
            continue
        subject = str(source.get("subject_id") or row.get("subject_id") or "").strip()
        explicit_complete = source.get("provenance_complete")
        source_scope = source.get("scope")
        scope_bound = source_scope is None or (isinstance(source_scope, dict) and same_source_scope(source))
        complete = bool(subject and source_id and scope_bound
                        and explicit_complete is not False)
        token = add_frame(("world", index), row.get("text", ""),
                          source.get("source_uids", source.get("authors", [])),
                          subject_id=subject, complete=complete)
        world_routes.append((index, token))

    # Evidence slots are filtered against the canonical LivingMemory atom,
    # before safe_evidence_atom drops event author metadata for model display.
    raw_view = managed.get("evidence_view") if isinstance(managed.get("evidence_view"), dict) else {}
    raw_atoms = raw_view.get("atoms", [])
    raw_atoms = raw_atoms if isinstance(raw_atoms, list) else []
    raw_atoms_by_id = {str(atom.get("atom_id")): atom for atom in raw_atoms
                       if isinstance(atom, dict) and atom.get("atom_id")}
    slot_routes = []
    slots = result.get("evidence_slots") if isinstance(result.get("evidence_slots"), dict) else {}
    for slot_name, rows in slots.items():
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            raw = raw_atoms_by_id.get(str(row.get("atom_id")), {})
            refs = raw.get("source_refs", [])
            refs = refs if isinstance(refs, list) else []
            actor_ids = [ref.get("author_id", ref.get("actor_id")) for ref in refs
                         if isinstance(ref, dict)]
            direct_subjects = strings(raw.get("subject_ids", []))
            if raw.get("subject_id"):
                direct_subjects = sorted(set(direct_subjects) | {str(raw["subject_id"]).strip()})
            resolved_refs = [str(value).strip() for value in actor_ids
                             if isinstance(value, (str, int)) and str(value).strip()]
            source_ids = sorted(set(direct_subjects) | set(resolved_refs))
            all_refs_attributed = bool(refs) and all(
                isinstance(ref, dict)
                and bool(str(ref.get("author_id", ref.get("actor_id", "")) or "").strip())
                for ref in refs
            )
            # A single canonical subject tied to a single exact event is also
            # attributable; host-history refs without authors stay unknown.
            if (not all_refs_attributed and len(refs) == 1 and len(direct_subjects) == 1
                    and isinstance(refs[0], dict) and refs[0].get("source_event_id")):
                all_refs_attributed = True
            if (all_refs_attributed and not direct_subjects and refs
                    and all(ref.get("author_kind") == "bot" for ref in refs if isinstance(ref, dict))):
                # A bot-authored row without a member subject may still be a
                # reply to a shielded member. Keep it only when a user source is
                # explicitly present in the same atom's provenance.
                all_refs_attributed = False
            complete = bool(source_ids and all_refs_attributed)
            token = add_frame(("slot", slot_name, index), row.get("claim", ""),
                              source_ids, subject_id=(direct_subjects[0] if len(direct_subjects) == 1 else ""),
                              complete=complete)
            slot_routes.append((slot_name, index, token))

    # Snapshot text and per-member commitments are dynamic. Missing attribution
    # is represented explicitly so the real filter drops it under any shield.
    snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    snapshot_routes = []
    humanlike = snapshot.get("humanlike", {})
    if isinstance(humanlike, dict):
        for kind, rows in humanlike.items():
            if not isinstance(rows, list):
                continue
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                subjects = strings(row.get("participant_ids", []))
                subject = str(row.get("subject_id", ""))
                if subject:
                    subjects = [subject]
                token = add_frame(
                    ("humanlike", kind, index),
                    row.get("text", row.get("nickname", "")),
                    subjects,
                    subject_id=subject,
                    complete=bool(subjects),
                )
                snapshot_routes.append(("humanlike", kind, index, token))
    for field in ("activity", "subjective_state", "summary"):
        value = snapshot.get(field)
        if isinstance(value, str) and value:
            snapshot_routes.append((field, add_frame(("snapshot_text", field), value)))
    commitments = snapshot.get("commitments", [])
    if isinstance(commitments, list):
        for index, row in enumerate(commitments):
            if not isinstance(row, dict):
                continue
            participants = strings(row.get("participant_ids", []))
            token = add_frame(("commitment", index), row.get("description", ""), participants,
                              subject_id=(participants[0] if len(participants) == 1 else ""),
                              complete=bool(participants))
            snapshot_routes.append(("commitments", index, token))
    recent = snapshot.get("recent_interaction")
    if isinstance(recent, dict):
        subjects = strings(recent.get("subject_ids", []))
        token = add_frame(("recent_interaction",), recent.get("summary", recent.get("feeling", "")),
                          subjects, subject_id=(subjects[0] if len(subjects) == 1 else ""),
                          complete=bool(subjects and recent.get("source_ids")))
        snapshot_routes.append(("recent_interaction", token))

    # These fields are already speaker-bound by their producing API. Mark the
    # target explicitly for the common filter; never infer it from text.
    for field in ("current_relationship_cue", "current_interaction_basis"):
        row = result.get(field)
        if isinstance(row, dict):
            target = str(row.get("subject_id") or row.get("speaker_id") or speaker_id).strip()
            token = add_frame((field,), row.get("text", row.get("guidance", "")), [target],
                              subject_id=target, complete=bool(target))
            snapshot_routes.append((field, token))
    for index, row in enumerate(result.get("system_receipts", [])):
        if isinstance(row, dict):
            token = add_frame(("system_receipt", index), row.get("text", ""), [speaker_id],
                              subject_id=speaker_id, complete=bool(speaker_id))
            snapshot_routes.append(("system_receipts", index, token))
    expression = result.get("expression_context")
    if isinstance(expression, dict):
        text_parts = [*expression.get("recent_replies", []), *expression.get("ellipsis_prefixes", [])]
        guidance = expression.get("guidance")
        if isinstance(guidance, str):
            text_parts.append(guidance)
        token = add_frame(("expression_context",), "\n".join(value for value in text_parts if isinstance(value, str)))
        snapshot_routes.append(("expression_context", token))

    filtered, state = member_boundary.filter_material(
        boundary_host, event, {"context_items": frames}, purpose="role_input")
    if not state.available or state.disabled:
        return result, state
    if not isinstance(filtered, dict) or not isinstance(filtered.get("context_items"), list):
        return {}, state
    kept = {row.get("_cd_boundary_token") for row in filtered["context_items"]
            if isinstance(row, dict) and isinstance(row.get("_cd_boundary_token"), str)}

    result["context_items"] = [row for index, row in enumerate(result.get("context_items", []))
                               if any(route_index == index and token in kept
                                      for route_index, token in context_routes)]
    result["world_items"] = [row for index, row in enumerate(result.get("world_items", []))
                             if index in static_world_tokens or any(
                                 route_index == index and token in kept for route_index, token in world_routes)]
    for slot_name, rows in list(slots.items()):
        slots[slot_name] = [row for index, row in enumerate(rows) if any(
            route_slot == slot_name and route_index == index and token in kept
            for route_slot, route_index, token in slot_routes)]
    if isinstance(result.get("snapshot"), dict):
        for route in snapshot_routes:
            if route[0] == "humanlike":
                _, kind, index, token = route
                if token not in kept:
                    result["snapshot"]["humanlike"][kind][index] = None
            elif len(route) == 2 and route[0] in {"activity", "subjective_state", "summary"}:
                field, token = route
                if token not in kept:
                    result["snapshot"].pop(field, None)
            elif route[0] == "commitments":
                _, index, token = route
                values = result["snapshot"].get("commitments", [])
                if isinstance(values, list) and index < len(values) and token not in kept:
                    values[index] = None
            elif route[0] == "recent_interaction" and route[1] not in kept:
                result["snapshot"].pop("recent_interaction", None)
            elif route[0] in {"current_relationship_cue", "current_interaction_basis", "expression_context"}:
                if route[1] not in kept:
                    result.pop(route[0], None)
            elif route[0] == "system_receipts":
                _, index, token = route
                values = result.get("system_receipts", [])
                if isinstance(values, list) and index < len(values) and token not in kept:
                    values[index] = None
        for kind, rows in result["snapshot"].get("humanlike", {}).items():
            result["snapshot"]["humanlike"][kind] = [row for row in rows if row is not None]
        for field in ("commitments",):
            values = result["snapshot"].get(field)
            if isinstance(values, list):
                result["snapshot"][field] = [row for row in values if row is not None]
    if isinstance(result.get("system_receipts"), list):
        result["system_receipts"] = [row for row in result["system_receipts"] if row is not None]
    return result, state
