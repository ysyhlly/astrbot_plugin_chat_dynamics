"""Join approved member observations, existing evidence and decided reply rhythm.

This module never recalls memory, approves learning, changes admission or writes a
persona. It only references evidence that survived the existing material filters.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time

from . import member_boundary
from .integrations import dynamics_horizons, dynamics_learning

GUIDANCE = (
    """conversation_adaptation把当前成员资料、有来源的共同对话线索和本轮回复节奏放在一起。
先接住本轮具体内容，再酌情使用相关资料；资料不存在或暂不可用时保持未知，不为了表现熟悉而编造经历。
member_profile只描述对方，不能改写你的性格、身份、价值观或自身喜恶；以原角色卡和已有稳定设定为准。表达样例用于理解语境，不照抄成固定口头禅，也不机械模仿错字或标点。
shared_context只索引已经提供的证据：成员自述仍是自述，群里说过不等于专门对你说过，计划/意图不等于执行完成；旧Bot回复不是本轮口吻模板。具体共同经历缺乏证据时不说“我们曾经”。
遵守既定response_plan与rhythm；优先回应新补充和纠正，不再询问已回答的问题。轻回应自然收住，认真倾诉留空间，闲聊无需每轮总结或反问。当前明确反馈优先于旧习惯，停止和成员边界仍是最终约束。
不向群友展示学习、审批、证据字段或内部判断过程；只输出角色实际会发的回复。
"""
    + dynamics_horizons.HORIZONS_REPLY_GUIDANCE
)
SCENE_GUIDANCE = """scene_patterns是已审查的同群接话方法，不是角色的新性格。先确认当前语境满足conditions且不触及avoid，
再借鉴method接住当前具体内容；examples是去身份的加工示例，不是原话、你的经历或可直接复制的台词。
最多借鉴一个方法，不强行玩梗。认真求助、当前纠正和明确边界优先于模式。"""

PROFILE_CATEGORIES = {"style", "typing_habit", "preference", "expression_example"}
SCOPE_KEYS = ("platform", "umo", "persona_id", "instance_id")


def split_prompt(prompt):
    """Remove the structured learning and horizons envelopes before constructing model messages."""
    try:
        payload = json.loads(prompt)
    except (ValueError, TypeError):
        return prompt, {}, None
    if not isinstance(payload, dict):
        return prompt, {}, None
    packet = payload.pop("dynamics_learning_reply", None)
    horizons = payload.pop("_horizons_v1", None)
    if packet is not None or horizons is not None:
        payload.pop("reviewed_learning_context", None)
        prompt = json.dumps(payload, ensure_ascii=False)
    if isinstance(horizons, dict) and horizons:
        payload = dict(payload, _horizons_v1=horizons)
    return prompt, payload, packet


def private_packet(host, owned_event, persona, previous=None):
    """Consume a scope resolved by this exact owned request's learning hook."""
    handoff = getattr(owned_event, "_dynamics_learning_private_scope_v1", None)
    source = dynamics_learning.provider(host) if host is not None else None
    if (
        not isinstance(handoff, dict)
        or handoff.get("owner") is not source
        or handoff.get("event") is not owned_event
        or source is None
    ):
        return previous
    scope = handoff.get("scope", {})
    umo = str(getattr(owned_event, "unified_msg_origin", ""))
    if not isinstance(scope, dict) or scope.get("umo") != umo or ":GroupMessage:" in umo:
        return previous
    old = previous if isinstance(previous, dict) else {}
    authorization = {
        "scope": umo,
        "persona": hashlib.sha256((str(persona.persona_id) + "\0" + str(persona.prompt)).encode()).hexdigest(),
        "member": str(owned_event.get_sender_id()),
        "baseline": old.get("baseline", ""),
        "host_version": old.get("host_version", dynamics_learning.HOST_VERSION),
    }
    selected = dynamics_learning.read(
        source, authorization, str(getattr(owned_event, "_cd_original_user_text_v1", "")), scope
    )
    projection = selected.get("reply_projection")
    return (
        {**projection, **authorization, "versions": selected["versions"]} if isinstance(projection, dict) else previous
    )


def _same_scope(left, right):
    return (
        isinstance(left, dict)
        and isinstance(right, dict)
        and all(left.get(k) == right.get(k) and right.get(k) for k in SCOPE_KEYS)
    )


def _words(text):
    return set(re.findall("[a-z0-9_]{2,}", text.lower())) | {
        text[i : i + 2] for i in range(len(text) - 1) if all("\u4e00" <= c <= "\u9fff" for c in text[i : i + 2])
    }


def _shared_context(material, managed, payload, speaker):
    result = {
        "memory_refs": [],
        "boundary_refs": [],
        "dialogue_ref": None,
        "usage": "仅在本轮相关时引用下列位置的原证据；自述不是外部事实，未核实计划不是共同经历。",
    }
    conversation = payload.get("conversation", {})
    dialogue = conversation.get("dialogue", {}) if isinstance(conversation, dict) else {}
    if (
        isinstance(dialogue, dict)
        and dialogue.get("user_id") == speaker
        and dialogue.get("last_bot_message_id")
        and isinstance(dialogue.get("source_message_ids"), (list, tuple))
        and dialogue["last_bot_message_id"] in dialogue["source_message_ids"]
    ):
        result["dialogue_ref"] = {
            "path": "conversation.dialogue",
            "source_message_ids": list(dialogue["source_message_ids"])[:12],
            "attribution": "当前成员与Bot已送达回复的连续对话，不用作固定口吻示范",
        }
    view = managed.get("evidence_view", {}) if isinstance(managed, dict) else {}
    if not isinstance(view, dict) or view.get("available") is not True or view.get("schema_version") != 1:
        return result
    scope = managed.get("scope", {})
    if not all(scope.get(k) for k in SCOPE_KEYS):
        return result
    from .narrative import _narrative_scope_key

    expected = _narrative_scope_key({k: scope[k] for k in SCOPE_KEYS})
    if view.get("scope_key") != expected:
        return result
    canonical = {a.get("atom_id"): a for a in view.get("atoms", []) if isinstance(a, dict)}
    query = _words(str(conversation.get("text", "")))
    ranked = []
    seen = set()
    for slot, rows in material.get("evidence_slots", {}).items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            atom = canonical.get(row.get("atom_id"))
            if (
                not atom
                or atom.get("claim") != row.get("claim")
                or atom.get("scope", {}).get("scope_key") != expected
                or atom.get("status") in {"withdrawn", "superseded", "disputed", "invalid", "rejected"}
                or row.get("atom_id") in seen
            ):
                continue
            refs = atom.get("source_refs", [])
            authors = {str(r.get("author_id", r.get("actor_id", ""))) for r in refs if isinstance(r, dict)}
            subjects = {str(uid) for uid in atom.get("subject_ids", []) if isinstance(uid, (str, int))}
            if speaker not in authors or speaker not in subjects:
                continue
            # The canonical memory service must have resolved an actual event.
            if not any(isinstance(root, str) and root.startswith("ev1:") for root in atom.get("root_evidence_ids", [])):
                continue
            if (
                atom.get("valid_until") is not None
                and isinstance(atom["valid_until"], (int, float))
                and atom["valid_until"] <= time.time()
            ):
                continue
            seen.add(row["atom_id"])
            pointer = {
                "atom_id": row["atom_id"],
                "slot": slot,
                "kind": row.get("kind"),
                "status": row.get("status"),
                "attribution": "该成员在此会话中的有来源表达；是否直接对Bot说、内容是否外部事实仍按原证据判断",
            }
            if slot == "commitments_and_boundaries":
                result["boundary_refs"].append(pointer)
            else:
                overlap = len(query & _words(str(row.get("claim", ""))))
                stamp = atom.get("valid_from") if isinstance(atom.get("valid_from"), (int, float)) else 0
                ranked.append((overlap, stamp, pointer))
    result["memory_refs"] = [r[2] for r in sorted(ranked, key=lambda r: (r[0], r[1]), reverse=True)[:3]]
    plan = payload.get("response_plan", {})
    attention = payload.get("attention_plan", {})
    if (
        isinstance(plan, dict)
        and plan.get("action") in {"acknowledge", "close"}
        or isinstance(attention, dict)
        and attention.get("mode") == "light"
    ):
        result["memory_refs"] = []  # A light acknowledgement does not need a recap.
    result["boundary_refs"] = result["boundary_refs"][:4]
    return result


def _rhythm(payload, speaker):
    plan = payload.get("response_plan", {})
    attention = payload.get("attention_plan", {})
    constraints = payload.get("delivery_constraints", {})
    conversation = payload.get("conversation", {})
    dialogue = conversation.get("dialogue", {}) if isinstance(conversation, dict) else {}
    if not isinstance(dialogue, dict) or dialogue.get("user_id") != speaker:
        dialogue = {}

    def value(data, key):
        return str(data.get(key, ""))[:80] if isinstance(data, dict) else ""

    return {
        "action": value(plan, "action"),
        "length": value(plan, "length"),
        "mode": value(attention, "mode"),
        "rhythm_state": value(constraints, "rhythm_state"),
        "rhythm_action": value(constraints, "rhythm_action"),
        "continuation_kind": value(dialogue, "continuation_kind"),
        "next_step": value(dialogue, "next_step"),
        "pending_question": dialogue.get("question_pending") is True,
        "closed": dialogue.get("closed") is True,
        "user_updates": len(dialogue.get("user_updates", []))
        if isinstance(dialogue.get("user_updates"), (list, tuple))
        else 0,
        "fragment_count": len(conversation.get("messages", []))
        if isinstance(conversation.get("messages"), list)
        else 0,
        "authority": "沿用已决定的对象、时机、动作和长度；这些资料不增加接话或主动联系权限。",
    }


def projection_current(packet, latest, required=True):
    projection = latest.get("reply_projection")
    if not isinstance(projection, dict):
        return not required
    if any(packet.get(k) != projection.get(k) for k in ("scope", "persona", "member")):
        return False
    if isinstance(packet.get("character_scope"), dict) and not _same_scope(
        packet["character_scope"], projection.get("character_scope")
    ):
        return False

    def stable(row):
        return json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)

    current = {
        stable(r) for r in projection.get("items", []) if isinstance(r, dict) and r.get("category") != "social_snapshot"
    }
    return all(
        stable(r) in current
        for r in packet.get("items", [])
        if isinstance(r, dict) and r.get("category") != "social_snapshot"
    )


def filter_learning_items(host, event, items, scope, speaker, versions):
    """Shared production and admin-preview boundary filtering; no model or sends."""
    learned = []
    frames = []
    rows = {}
    for index, row in enumerate(items[:30]):
        if (
            not isinstance(row, dict)
            or row.get("member") not in ("", speaker)
            or not isinstance(row.get("source_id"), str)
        ):
            continue
        if row.get("category") != "social_snapshot" and row["source_id"] not in versions:
            continue
        token = "learning:" + str(index)
        rows[token] = row
        frame = {
            "_cd_boundary_token": token,
            "subject_id": row.get("member", ""),
            "source_uids": row.get("source_uids", []),
            "provenance_complete": row.get("provenance_complete") is True,
            "text": json.dumps(row, ensure_ascii=False),
        }
        if len(frame["source_uids"]) == 1:
            frame["author_uid"] = frame["source_uids"][0]
        frames.append(frame)

    def filtered():
        output, state = member_boundary.filter_material(
            host, event, {"context_items": frames}, purpose="reply", scope=scope
        )
        if not state.available and (member_boundary.fail_closed(state) or state.service is not None):
            raise member_boundary.BoundaryCancelled("reply_learning_filter_unavailable")
        return (
            {
                r.get("_cd_boundary_token"): r.get("text", "")
                for r in output.get("context_items", [])
                if isinstance(r, dict) and r.get("_cd_boundary_token") in rows
            }
            if isinstance(output, dict)
            else {}
        )

    kept = filtered()
    for token, text in kept.items():
        try:
            row = json.loads(text)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(row, dict)
            and row.get("source_id") == rows[token]["source_id"]
            and row.get("member") == rows[token].get("member")
        ):
            learned.append(row)
    return learned, kept, filtered


def compose(material, managed, payload, packet, host, event, persona):
    """Build a single provider-facing layer, plus a cached pre-call validity guard."""
    managed = managed if isinstance(managed, dict) else {}
    horizons_proj = payload.get("_horizons_v1") if isinstance(payload, dict) else None
    if not (isinstance(horizons_proj, dict) and horizons_proj):
        horizons_proj = managed.get("horizons_reply_projection")
    horizons_source = (
        dynamics_horizons.provider(host)
        if (host is not None and isinstance(horizons_proj, dict) and horizons_proj)
        else None
    )
    conversation = payload.get("conversation", {}) if isinstance(payload, dict) else {}
    speaker = str(conversation.get("author", "")) if isinstance(conversation, dict) else ""
    scope = managed.get("scope", {})
    if not scope and isinstance(packet, dict) and isinstance(packet.get("character_scope"), dict):
        scope = packet["character_scope"]
    if not speaker or managed and str(managed.get("speaker_id", "")) != speaker:
        if isinstance(horizons_proj, dict) and horizons_proj and (host is None or horizons_source is not None):
            enriched = copy.deepcopy(material)
            enriched["horizons"] = copy.deepcopy(horizons_proj)
            return enriched, (lambda: True) if host is None else (
                lambda: dynamics_horizons.provider(host) is horizons_source
            )
        return material, lambda: True
    sender = getattr(event, "get_sender_id", None)
    if callable(sender) and str(sender()) != speaker:
        return material, lambda: True
    result = copy.deepcopy(material)
    result.pop("learning_context", None)
    if isinstance(horizons_proj, dict) and horizons_proj and (host is None or horizons_source is not None):
        result["horizons"] = copy.deepcopy(horizons_proj)
    else:
        result.pop("horizons", None)
    learned = []

    def guard():
        return True

    projection_status = "unavailable"
    scene_rows = []
    scene_trace = None
    style_rows = []
    style_enabled = False
    current_expression_request = []
    if isinstance(packet, dict) and type(packet.get("schema_version")) is int and packet.get("schema_version") == 1:
        character = managed.get("character", {})
        prompt = (
            character["core_prompt"] + "\n" + str(character.get("group_rules", ""))
            if isinstance(character, dict) and isinstance(character.get("core_prompt"), str)
            else str(getattr(persona, "prompt", ""))
        )
        expected = hashlib.sha256((str(getattr(persona, "persona_id", "")) + "\0" + prompt).encode()).hexdigest()
        valid_identity = (
            packet.get("scope") == getattr(event, "unified_msg_origin", None)
            and packet.get("member") == speaker
            and packet.get("persona") == expected
            and (not scope or _same_scope(packet.get("character_scope"), scope))
        )
        source = dynamics_learning.provider(host) if host is not None else None
        authorization = {k: packet.get(k) for k in ("scope", "persona", "member", "baseline", "host_version")}
        versions = packet.get("versions", [])

        def source_current():
            if source is None or dynamics_learning.provider(host) is not source:
                return False
            latest = dynamics_learning.read(source, authorization, str(conversation.get("text", "")), scope)
            latest_scope = latest.get("reply_projection", {}).get("character_scope")
            required = "reply_projection_v1" in getattr(source, "learning_capabilities", lambda: {})().get(
                "capabilities", []
            )
            return (
                isinstance(versions, list)
                and all(v in latest["versions"] for v in versions)
                and projection_current(packet, latest, required)
                and (not scope or latest_scope is None or _same_scope(latest_scope, scope))
            )

        if valid_identity and source_current() and isinstance(packet.get("items"), list):
            learned, kept, filtered = filter_learning_items(host, event, packet["items"], scope, speaker, versions)
            if versions or learned:

                def guard():
                    return source_current() and filtered() == kept

            projection_status = "available" if learned else "empty_or_filtered"
            scene_packet = packet.get("scene_selection")
            if isinstance(scene_packet, dict):
                from .integrations.scene_learning import current as scene_current
                from .integrations.scene_learning import validate as validate_scene

                try:
                    validate_scene(scene_packet, authorization)
                    if scene_current(source, scene_packet):
                        scene_rows, scene_kept, scene_filtered = filter_learning_items(
                            host, event, scene_packet["items"], scope, speaker, scene_packet["versions"]
                        )
                        base_guard = guard

                        def guard():
                            return (
                                base_guard()
                                and dynamics_learning.provider(host) is source
                                and scene_current(source, scene_packet)
                                and scene_filtered() == scene_kept
                            )

                        scene_trace = scene_packet.get("trace")
                except (ValueError, TypeError, KeyError):
                    scene_rows = []
            style_packet = packet.get("expression_selection")
            if isinstance(style_packet, dict):
                from .integrations.expression_style import (
                    corrections as style_corrections,
                )
                from .integrations.expression_style import (
                    current as style_current,
                )
                from .integrations.expression_style import (
                    validate as validate_style,
                )

                try:
                    validate_style(style_packet, authorization)
                    if style_current(source, style_packet):
                        style_enabled = True
                        style_rows, style_kept, style_filtered = filter_learning_items(
                            host, event, style_packet["items"], scope, speaker, style_packet["versions"]
                        )
                        expression_base_guard = guard
                        if style_packet["items"]:

                            def guard():
                                return (
                                    expression_base_guard()
                                    and dynamics_learning.provider(host) is source
                                    and style_current(source, style_packet)
                                    and style_filtered() == style_kept
                                )

                        current_expression_request = style_corrections(str(conversation.get("text", "")))
                        if style_rows:
                            scene_trace = style_packet.get("trace")
                except (ValueError, TypeError, KeyError):
                    style_rows = []
    profiles = [row for row in learned if row.get("category") in PROFILE_CATEGORIES]
    other = [row for row in learned if row.get("category") not in PROFILE_CATEGORIES]
    if style_enabled:
        from .integrations.expression_style import budget_examples

        profiles, scene_rows, style_rows = budget_examples(profiles, scene_rows, style_rows)
    result["member_profile"] = {
        "member_id": speaker,
        "status": "ready" if profiles else "no_approved_profile_in_context",
        "source_status": projection_status,
        "items": profiles,
        "usage": "仅已批准且有效的成员资料；用来理解和调整交流方式，不改写Bot自身性格，不机械模仿。",
    }
    result.setdefault("source_scope", scope)
    result.setdefault("speaker_id", speaker)
    if other:
        result["learning_context"] = other
    if scene_rows:
        result["scene_patterns"] = scene_rows
        result["scene_trace"] = scene_trace
        result["scene_usage"] = SCENE_GUIDANCE
    if style_rows:
        from .integrations.expression_style import GUIDANCE as EXPRESSION_GUIDANCE

        result["expression_style"] = style_rows
        result["expression_usage"] = EXPRESSION_GUIDANCE
        if any(r.get("category") == "preference" for r in profiles):
            result["expression_usage"] += (
                "当前成员已有的明确交流偏好优先于群体习惯；不采用与其偏好冲突的语气词、玩笑或写法。"
            )
        result["scene_trace"] = scene_trace
    if current_expression_request:
        result["current_expression_request"] = current_expression_request
    corrections = result.get("snapshot", {}).get("humanlike", {}).get("corrections", [])
    result["conversation_adaptation"] = {
        "schema_version": 1,
        "member_id": speaker,
        "personality_locked": True,
        "member_profile_path": "member_profile",
        "shared_context": _shared_context(result, managed, payload, speaker),
        "rhythm": _rhythm(payload, speaker),
        "current_corrections_path": "snapshot.humanlike.corrections" if corrections else None,
        "priority": [
            "成员边界与既定角色约束",
            "本轮明确要求和纠正",
            "本轮问题与既定节奏",
            "相关的已审查成员资料与有来源的过往",
        ],
        "usage": "先回应眼前的人和事，资料相关时自然使用；不为了展示熟悉而逐条复述，不把群友喜恶变成自己的喜恶。",
    }
    offered = packet.get("items", []) if isinstance(packet, dict) else []
    used = {row["source_id"] for row in profiles}
    result["_reply_usage_omitted"] = [
        {"source_id": row.get("source_id", ""), "reason": "boundary_or_identity_filter"}
        for row in offered
        if isinstance(row, dict) and row.get("category") in PROFILE_CATEGORIES and row.get("source_id") not in used
    ]
    if "horizons" in result and host is not None:
        prior_guard = guard

        def guard():
            return prior_guard() and dynamics_horizons.provider(host) is horizons_source

    return result, guard


def usage(material, omitted=()):
    layer = material.get("conversation_adaptation", {})
    if not isinstance(layer, dict) or layer.get("schema_version") != 1:
        return None
    return {
        "profiles": [
            {k: row.get(k) for k in ("source_id", "category", "revision", "source_umo")}
            for row in material.get("member_profile", {}).get("items", [])
        ],
        "learning": [
            {
                **{k: row.get(k) for k in ("source_id", "category", "revision", "source_umo")},
                **(
                    {"example_ids": [v["id"] for v in row["value"].get("examples", [])]}
                    if row.get("category") == "expression_style"
                    else {}
                ),
            }
            for row in [
                *material.get("learning_context", []),
                *material.get("scene_patterns", []),
                *material.get("expression_style", []),
            ]
            if row.get("category") != "social_snapshot"
        ],
        "scene_trace": material.get("scene_trace"),
        "memory_refs": [r["atom_id"] for r in layer.get("shared_context", {}).get("memory_refs", [])],
        "dialogue": bool(layer.get("shared_context", {}).get("dialogue_ref")),
        "rhythm": layer.get("rhythm", {}),
        "personality_locked": layer.get("personality_locked") is True,
        "omitted": list(omitted),
    }


def counts(material):
    layer = material.get("conversation_adaptation", {})
    if not isinstance(layer, dict) or layer.get("schema_version") != 1:
        return None
    shared = layer.get("shared_context", {})
    return {
        "profiles": len(material.get("member_profile", {}).get("items", [])),
        "memory_refs": len(shared.get("memory_refs", [])),
        "dialogue": int(bool(shared.get("dialogue_ref"))),
        "rhythm": int(bool(layer.get("rhythm"))),
        "personality_locked": layer.get("personality_locked") is True,
    }


def measure(contexts, provenance):
    """Count only exact composed material that survived to provider kwargs."""
    result = {"reply_adaptation_stage": "unavailable", "reply_adaptation_counts": None}
    entries = provenance.get("adaptation_entries", {}) if isinstance(provenance, dict) else {}
    if not entries or not isinstance(contexts, list):
        return result
    from .character_view import _LM_MATERIAL_PREFIX, _LM_PART_COUNTERS, _provider_text_parts

    parts = []
    if not _provider_text_parts(contexts, parts, {k: 0 for k in _LM_PART_COUNTERS}):
        return result
    decoder = json.JSONDecoder()
    for text in parts:
        offset = 0
        while True:
            start = text.find(_LM_MATERIAL_PREFIX, offset)
            if start < 0:
                break
            start += len(_LM_MATERIAL_PREFIX)
            try:
                _, length = decoder.raw_decode(text[start:])
            except ValueError:
                offset = start
                continue
            raw = text[start : start + length]
            digest = hashlib.sha256(raw.encode()).hexdigest()
            if digest in entries:
                result = {
                    "reply_adaptation_stage": "provider_kwargs_after_boundaries",
                    "reply_adaptation_counts": entries[digest],
                }
                report = provenance.get("usage_entries", {}).get(digest)
                if isinstance(report, dict):
                    result["reply_usage"] = {**report, "material_hash": digest}
                return result
            offset = start + length
    return {"reply_adaptation_stage": "not_present", "reply_adaptation_counts": None}


def log_measurement(diagnostic):
    value = diagnostic.get("reply_adaptation_counts") if isinstance(diagnostic, dict) else None
    if not isinstance(value, dict) or diagnostic.get("reply_adaptation_stage") != "provider_kwargs_after_boundaries":
        return
    from astrbot.api import logger

    logger.info(
        f"[ChatDynamics] Reply adaptation profiles={value['profiles']} memory_refs={value['memory_refs']} "
        f"dialogue={value['dialogue']} rhythm={value['rhythm']} personality_locked={value['personality_locked']}"
    )


class ReplyGuardProvider:
    """The unmanaged fallback still checks approval and boundary revisions."""

    def __init__(self, provider, guard, provenance, reporter=None):
        self.provider, self.guard, self.provenance = provider, guard, provenance
        self.reporter = reporter

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def check(self, kwargs):
        if not self.guard():
            raise member_boundary.BoundaryCancelled("reply_learning_invalidated")
        diagnostic = measure(kwargs.get("contexts"), self.provenance)
        log_measurement(diagnostic)
        if self.reporter is not None:
            try:
                self.reporter(diagnostic)
            except Exception:
                pass

    async def text_chat(self, *args, **kwargs):
        self.check(kwargs)
        result = await self.provider.text_chat(*args, **kwargs)
        if not self.guard():
            raise member_boundary.BoundaryCancelled("reply_learning_invalidated")
        return result

    async def text_chat_stream(self, *args, **kwargs):
        self.check(kwargs)
        stream = self.provider.text_chat_stream(*args, **kwargs)
        try:
            async for response in stream:
                if not self.guard():
                    raise member_boundary.BoundaryCancelled("reply_learning_invalidated")
                yield response
        finally:
            close = getattr(stream, "aclose", None)
            if close:
                await close()


def report_unmanaged_usage(host, event, diagnostic):
    if (
        not isinstance(diagnostic, dict)
        or diagnostic.get("reply_adaptation_stage") != "provider_kwargs_after_boundaries"
    ):
        return
    usage = diagnostic.get("reply_usage")
    if not isinstance(usage, dict):
        return
    scope = getattr(event, "unified_msg_origin", "")
    member = str(event.get_sender_id())
    trace = usage.get("scene_trace")
    if trace is not None:
        if not isinstance(trace, dict) or trace.get("scope") != scope or trace.get("member") != member:
            return
        turn_id = trace.get("turn_id")
    else:
        if not (usage.get("profiles") or usage.get("learning")):
            return
        extras = getattr(event, "_extras", None)
        turn_id = extras.get("_cd_pipeline_turn_id_v1") if isinstance(extras, dict) else None
    if not scope or not member or not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 256:
        return
    source = dynamics_learning.provider(host) if host is not None else None
    report = getattr(source, "report_reply_usage", None)
    if callable(report):
        report(
            {
                "schema_version": 1,
                **usage,
                "stage": "provider_kwargs_after_boundaries",
                "scope": scope,
                "member": member,
                "turn_id": turn_id,
            }
        )


async def preview_learning_material(host, umo, persona, member, query):
    from types import SimpleNamespace

    source = dynamics_learning.provider(host)
    if source is None:
        raise ValueError("群间学习未就绪")
    result = {
        "available": False,
        "scope": umo,
        "member": member,
        "persona": persona,
        "candidates": [],
        "offered": [],
        "items": [],
        "excluded": [],
        "model_called": False,
        "message_sent": False,
        "character_verified": False,
        "note": "按当前角色、生产数据契约及成员边界验证的学习材料快照；真实回复会在调用模型前再次检查版本。",
    }
    authorization = dict(
        scope=umo,
        persona=persona,
        member=member,
        baseline=host._turn_config_identity(),
        host_version=dynamics_learning.HOST_VERSION,
    )
    try:
        raw = source.get_learning_context(**authorization, query=query, trace=True)
        selected = dynamics_learning.validate_selection(raw, authorization)
    except (ValueError, TypeError, KeyError):
        result["reason"] = "学习材料未通过正式回复的数据契约校验"
        return result
    packet = selected.get("reply_projection", {})
    scope = packet.get("character_scope")
    offered = packet.get("items", [])
    result.update(candidates=raw.get("candidates", []), offered=offered)
    runtimes = {id(r): r for r in getattr(host, "_sessions", {}).values() if getattr(r, "umo", "") == umo}
    runtime = next(iter(runtimes.values())) if len(runtimes) == 1 else None
    if not runtime or not runtime.bot_id:
        result["reason"] = "缺少该群实际运行身份，请等待一条真实消息后重试"
        return result
    if not isinstance(scope, dict) or scope.get("umo") != umo or not all(scope.get(k) for k in SCOPE_KEYS):
        result["reason"] = "当前角色身份快照不完整"
        return result
    try:
        platform = host.context.get_platform_inst(umo.rsplit(":", 2)[0])
        if platform is None:
            raise ValueError("platform unavailable")
        event = SimpleNamespace(
            unified_msg_origin=umo,
            get_platform_id=lambda: umo.rsplit(":", 2)[0],
            get_self_id=lambda: str(runtime.bot_id),
            get_sender_id=lambda: member,
            get_session_id=lambda: umo,
            get_platform_name=lambda: platform.meta().name,
        )
        async with asyncio.timeout(8):
            current_persona = await host.persona_engine.bridge.snapshot(event, record_media=False)
            world = host.integrations.narrative_provider("worldbook")
            character = await world.get_character_context(
                {
                    "platform": scope["platform"],
                    "umo": umo,
                    "persona_id": current_persona.persona_id,
                    "instance_id": current_persona.persona_id,
                },
                None,
            )
        if (
            not isinstance(character, dict)
            or not character.get("available")
            or character.get("disabled")
            or not isinstance(character.get("core_prompt"), str)
        ):
            raise ValueError("character unavailable")
    except Exception:
        result["reason"] = "无法确认当前角色，未将旧快照标记为可注入"
        return result
    expected = hashlib.sha256(
        (
            str(current_persona.persona_id)
            + "\0"
            + character["core_prompt"]
            + "\n"
            + str(character.get("group_rules", ""))
        ).encode()
    ).hexdigest()
    if expected != persona or not _same_scope(character.get("scope"), scope):
        result["reason"] = "当前角色或绑定已变化，请等待真实回合更新学习快照"
        return result
    result["character_verified"] = True
    latest = dynamics_learning.read(source, authorization, query, scope)
    if (
        dynamics_learning.provider(host) is not source
        or not projection_current(packet, latest)
        or not all(v in latest["versions"] for v in selected["versions"])
    ):
        result["reason"] = "读取期间资料版本已变化，请重新预览"
        return result
    policy, _, state = member_boundary.peek(host, event, member, scope=scope)
    if not state.available and (state.required or state.service is not None):
        result["reason"] = "成员边界服务或身份不可用，未确认任何可注入内容"
        return result
    if member_boundary.blocked(policy, purpose="reply"):
        result.update(
            available=True,
            reason="当前成员边界阻止回复",
            excluded=[{"source_id": r["source_id"], "reason": "成员边界禁止回复"} for r in offered],
        )
        return result
    try:
        learned, _, _ = filter_learning_items(host, event, offered, scope, member, selected.get("versions", []))
    except member_boundary.BoundaryCancelled:
        result["reason"] = "成员边界过滤未完成，未确认任何可注入内容"
        return result
    kept = {r["source_id"] for r in learned}
    result.update(
        available=True,
        items=learned,
        boundary="checked" if state.available and not state.disabled else "not_enabled",
        excluded=[
            {"source_id": r["source_id"], "reason": "成员边界或来源完整性过滤"}
            for r in offered
            if r["source_id"] not in kept
        ],
    )
    return result
