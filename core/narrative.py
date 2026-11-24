"""Coordinate public character APIs, immutable contexts and real delivery facts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from astrbot.api import logger
import math
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo
from contextvars import ContextVar

from .narrative_store import NarrativeJournal
from .semantic_excerpt import excerpt
from .character_material import energy_context, select_commitments
from .character_view import historical_item, speaker_snapshot
from .environment_context import activity_text,environment_context
from .scene_context import scene_context
from .world_visual_context import world_visual_context
from .private_original_input import is_private_message_event, private_message_recipient
from . import member_boundary
from .integrations import dynamics_horizons
from .platform_bridge import as_send_result
from .pipeline_telemetry import PipelineTelemetry
from .shared_reads import SharedReads
from .exact_calls import ExactMemo, CallTimings
from .decision_compaction import COMPACT_DECISION_GUIDANCE
from .social_opportunity import (
    decision_allows_social_observation,
    private_social_candidate,
    public_social_candidate,
    social_boundary_allows,
)

DECISION_GUIDANCE = COMPACT_DECISION_GUIDANCE

_READ_SCOPE = ContextVar('cd_narrative_read_scope', default=None)
_MEMO_METHODS = frozenset({'get_evidence_view', 'get_profiles', 'get_social_snapshot', 'get_social_context', 'get_snapshot'})


@dataclass(frozen=True)
class _RpcFailure:
    reason: str
    budget: float = 0.0


@dataclass(frozen=True)
class _PrepareTrace:
    turn_id: str
    session_id: str
    phase: str
    started: float

    def unavailable(self, stage, reason):
        logger.info(
            "Narrative prepare unavailable turn_id=%s session_id=%s phase=%s stage=%s reason=%s elapsed_ms=%.1f",
            self.turn_id, self.session_id, self.phase, stage, reason, (time.monotonic() - self.started) * 1000,
        )


@dataclass(frozen=True)
class _ReadScope:
    key: str
    expires_at: float
    trace: _PrepareTrace


MANAGED_KEY = "_narrative_managed_v1"
RECIPIENTS = ("dynamic_life_state", "livingmemory", "self_learning")
MEMORY_RECALL_TIMEOUT_SECONDS = 8.0
# 命名 RPC 的默认预算。调用方显式传 timeout 时以显式值为准（8s 与
# MEMORY_RECALL_TIMEOUT_SECONDS 本次未动）；只有省略 timeout 的调用点才使用
# plugin 配置的 narrative_rpc_timeout_seconds（schema 默认 5）。
# 取 5 而非 10：evidence-view 的两处调用在回复路径上串行 await，10s 会把单次回复
# 尾延迟上限抬到 10s；5s 是「2s 恒定失败」的 2.5 倍余量且有界。上线后若仍超时，
# 由运维调配置即可，无需改代码。
DEFAULT_RPC_TIMEOUT_SECONDS = 5.0
# 证据视图（get_evidence_view）在回复路径上是串行 await，且是 livingmemory 侧真实
# 读库+组装的重活。生产 instrumented 实测：中位约 4.5~5.5s，长尾 6.0/6.2/6.4/7.0/7.9s，
# 8s 预算下 60 分钟内仍有 2 次被截断（outlier=full_budget）。
# 因此默认给 10s 覆盖实测长尾，并改为配置项 narrative_evidence_timeout_seconds
# （取值 1-120），运维可按日志里的 elapsed_ms/outlier 分布自行调档；本常量仅作兜底。
DEFAULT_EVIDENCE_VIEW_TIMEOUT_SECONDS = 10.0
# 同 (provider, method) 的不可用 WARNING 限流窗口，避免慢 provider 刷屏。
RPC_WARN_INTERVAL_SECONDS = 300.0
# 单次调用耗时占预算达到该比例即标记 outlier，用于上线后按实测耗时定档。
RPC_SLOW_RATIO = 0.5
_HISTORY_QUOTE_REQUEST = re.compile(
    r"(?:原话|原句|原文|逐字|照原样(?:说|复述|引用)?|原样(?:引用|复述)|准确引用|引用原文|"
    r"(?:你|我|他|她|当时|上次|之前)(?:具体)?(?:是)?怎么说的|当时(?:的)?(?:完整)?(?:原话|原句)|"
    r"(?:quote|repeat).{0,20}(?:verbatim|exact|original)|(?:exact|original) wording|"
    r"what\s+(?:exactly\s+)?(?:did\s+you|did\s+i|was)\s+(?:say|said))", re.IGNORECASE)

_LM_RECALL_ROUTE_NAMES = frozenset({
    "scope_auth", "history_legacy", "history_managed", "narrative_events",
    "narrative_sources", "candidate_recheck", "evidence_view", "ready_baseline",
    "semantic_legacy", "semantic_managed",
    "revision_peek", "scope_memory_policy", "scope_session", "scope_plugin", "scope_config", "baseline_recheck",
})
_LM_RECALL_ROUTE_STATES = frozenset({"timeout", "error", "busy", "closed", "unavailable"})


def safe_livingmemory_recall_diagnostics(diagnostics):
    """Allowlist body-free runtime counters from LivingMemory's recall_context."""
    if not isinstance(diagnostics, dict):
        return {}
    result = {}
    for field in ("baseline_preserved", "local_reply_fast_path"):
        if type(diagnostics.get(field)) is bool:
            result[field] = diagnostics[field]
    for key, maximum in (("elapsed_ms", 120000), ("inflight_count", 1024)):
        value = diagnostics.get(key)
        if type(value) is int and 0 <= value <= maximum:
            result[key] = value
    for key, states in (("route_elapsed_ms", None), ("route_failures", _LM_RECALL_ROUTE_STATES)):
        values = diagnostics.get(key)
        if not isinstance(values, dict):
            continue
        if states is None:
            kept = {}
            for route, value in values.items():
                if route in _LM_RECALL_ROUTE_NAMES and type(value) is int and 0 <= value <= 120000:
                    kept[route] = value
            if len(kept) >= 20:
                        break
        else:
            kept = {}
            for route, value in values.items():
                if route in _LM_RECALL_ROUTE_NAMES and isinstance(value, str) and value in states:
                    kept[route] = value
            if len(kept) >= 20:
                        break
        if kept:
            result[key] = kept
    return result


def _bounded_projection_stage_counts(raw_view, canonical_view, projected_items):
    """Return bounded, body-free counts; malformed or absent inputs stay unknown."""
    def atom_count(view):
        if not isinstance(view, dict) or not isinstance(view.get("atoms"), list):
            return None
        return min(len(view["atoms"]), 100000)

    if not isinstance(projected_items, list) or any(not isinstance(row, dict) for row in projected_items):
        projected_count = None
    else:
        projected_count = min(sum(row.get("provider") == "livingmemory" for row in projected_items), 100000)
    return {
        "raw_recall_view_atoms": atom_count(raw_view),
        "canonical_merged_atoms": atom_count(canonical_view),
        "projected_lm_context_items": projected_count,
    }


def history_request_purpose(query, caller_purpose=None):
    """Choose quote mode only from an explicit caller route or the real user query."""
    if caller_purpose in {"reply", "history_quote"}:
        return caller_purpose
    return "history_quote" if isinstance(query, str) and _HISTORY_QUOTE_REQUEST.search(query) else "reply"


def decision_materials(items, limit=6):
    """Keep historical evidence and social context represented in a small budget."""
    items = [historical_item(row) for row in items]
    memory = [row for row in items if row.get("provider") == "livingmemory"]
    historical = [row for row in memory if str(row.get("provenance_kind", "")).startswith("historical")]
    buckets = [historical + [row for row in memory if row not in historical],
               [row for row in items if row.get("provider") == "self_learning"],
               [row for row in items if row.get("provider") == "portrayal"],
               [row for row in items if row.get("provider") not in
                {"livingmemory", "self_learning", "portrayal"}]]
    selected = []
    while len(selected) < limit and any(buckets):
        for bucket in buckets:
            if bucket and len(selected) < limit:
                row = bucket.pop(0)
                selected.append({key: value for key, value in row.items() if key in
                                 {"text", "source_id", "provider", "fact_kind", "unknown", "provenance_kind", "subject_id", "source_umo"}})
    for row in selected:
        row["text"] = excerpt(row.get("text", ""), 220)
    return selected


def same_scope(left, right):
    return all(left.get(key) == right.get(key) for key in ("platform", "umo", "persona_id", "instance_id"))


def effective_core_version(character):
    """Hash only stable persona instructions, excluding dynamic Worldbook matches."""
    if not isinstance(character, dict):
        return ""
    payload = {
        "schema": "effective_core_v1",
        "card_id": character.get("card_id"),
        "core_prompt": character.get("core_prompt", ""),
        "group_rules": character.get("group_rules", ""),
    }
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def safe_evidence_atom(atom):
    """Keep claim semantics and source fingerprints, not raw source records."""
    if not isinstance(atom, dict):
        return None
    result = {key: atom[key] for key in ("atom_id", "kind", "status", "claim", "conditions",
              "polarity", "valid_from", "valid_until", "confidence", "source_status",
              "delivery_status", "execution_status") if key in atom}
    refs = atom.get("source_refs")
    if isinstance(refs, list):
        result["source_refs"] = [{key: ref[key] for key in
            ("material_id", "source_event_id", "event_type", "text_hash", "exact_span",
             "source_status", "author_kind", "delivery_status") if key in ref}
            for ref in refs if isinstance(ref, dict)][:8]
    roots = atom.get("root_evidence_ids")
    if isinstance(roots, list):
        result["root_evidence_ids"] = [str(value)[:240] for value in roots[:8]]
    return result


def select_evidence_slots(raw_slots, budget_chars):
    """Fit whole evidence atoms; never truncate claims, negation, conditions or deadlines."""
    raw_slots = raw_slots if isinstance(raw_slots, dict) else {}
    names = ("current_evidence", "commitments_and_boundaries", "recent", "historical_claims")
    source = {name: [safe_evidence_atom(row) for row in raw_slots.get(name, [])
                     if isinstance(row, dict)]
              if isinstance(raw_slots.get(name, []), list) else [] for name in names}
    selected = {name: [] for name in names}
    required = ("current_evidence", "commitments_and_boundaries")
    for name in required:
        selected[name] = source[name]
    required_chars = len(json.dumps({name: selected[name] for name in names}, ensure_ascii=False,
                                    separators=(",", ":")))
    over_budget = required_chars > max(0, int(budget_chars))
    omitted = {name: 0 for name in names}
    for name in required:
        omitted[name] = 0
    if not over_budget:
        # Keep recent material ahead of older claims; each whole atom must fit.
        for name in ("recent", "historical_claims"):
            for atom in source[name]:
                candidate = {key: list(value) for key, value in selected.items()}
                candidate[name].append(atom)
                size = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
                if size <= budget_chars:
                    selected[name].append(atom)
                else:
                    omitted[name] += 1
    else:
        for name in ("recent", "historical_claims"):
            omitted[name] = len(source[name])
    counts = {name: {"available": len(source[name]), "included": len(selected[name]),
                     "omitted": omitted[name]} for name in names}
    return selected, {"budget_chars": max(0, int(budget_chars)),
                      "required_slots_over_budget": over_budget, "slots": counts}


def _host_history_visible_text(role, content):
    """Extract only host-visible text from the known serialized history protocol.

    The original row remains untouched for identity matching. Think, media,
    tool and unknown parts are never converted into quoteable source text.
    """
    if role not in {"user", "assistant"}:
        return None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    visible = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and set(part) == {"type", "text"} and isinstance(part.get("text"), str):
            visible.append(part["text"])
        # Known reasoning fields are deliberately ignored. All other part
        # protocols (media, tool, and future extensions) remain non-evidence.
    text = "".join(visible)
    return text if text else None


def host_history_snapshot(history, conversation_id="", conversation_created_at=None):
    """Return conservative visible-text host-history refs and a content fingerprint.

    The fingerprint covers the exact serialized host snapshot. References carry
    only role/text and the host list position; they deliberately do not invent
    authors, event IDs, timestamps, or delivery status.
    """
    if not isinstance(history, str) or not history:
        return {"conversation_id": str(conversation_id or ""), "snapshot_fingerprint": "", "rows": [], "source_refs": []}
    try:
        rows = json.loads(history)
    except (TypeError, ValueError):
        return {"conversation_id": str(conversation_id or ""), "snapshot_fingerprint": "", "rows": [], "source_refs": []}
    if not isinstance(rows, list):
        return {"conversation_id": str(conversation_id or ""), "snapshot_fingerprint": "", "rows": [], "source_refs": []}
    normalized = []
    refs = []
    ref_diagnostics = {"text_ref_extracted": 0, "string_ref_extracted": 0,
                       "text_part_ref_extracted": 0, "mixed_part_ref_extracted": 0,
                       "source_ref_rows_skipped": 0}
    prefix = hashlib.sha256(b"chat-dynamics-host-history-prefix-v1\0")
    epoch = ""
    if isinstance(conversation_created_at, int) and not isinstance(conversation_created_at, bool) and conversation_created_at > 0:
        epoch_payload = json.dumps([str(conversation_id or ""), conversation_created_at],
                                  ensure_ascii=False, separators=(",", ":"))
        epoch = hashlib.sha256(epoch_payload.encode("utf-8")).hexdigest()
    for position, row in enumerate(rows):
        prefix_anchor = prefix.hexdigest()
        if not isinstance(row, dict):
            normalized.append(None)
            encoded = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            prefix.update(position.to_bytes(8, "big") + b"\0" + encoded.encode("utf-8") + b"\0")
            continue
        role, content = row.get("role"), row.get("content")
        normalized.append(copy.deepcopy(row))
        encoded = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        prefix.update(position.to_bytes(8, "big") + b"\0" + encoded.encode("utf-8") + b"\0")
        # Keep the source position and prefix anchor tied to the complete host
        # row, while citing only its visible text fields. Extra message fields
        # can indicate a tool exchange or an unrecognized host protocol.
        visible_text = _host_history_visible_text(role, content)
        if (visible_text and set(row).issubset({"role", "content"})):
            refs.append({"source_kind": "host_history", "position": position,
                         "role": role, "text": visible_text,
                         "source_prefix_anchor": prefix_anchor,
                         **({"history_epoch": epoch} if epoch else {})})
            ref_diagnostics["text_ref_extracted"] += 1
            if isinstance(content, str):
                ref_diagnostics["string_ref_extracted"] += 1
            else:
                ref_diagnostics["text_part_ref_extracted"] += 1
                if any(not isinstance(part, dict) or part.get("type") != "text" for part in content):
                    ref_diagnostics["mixed_part_ref_extracted"] += 1
        else:
            ref_diagnostics["source_ref_rows_skipped"] += 1
    return {"conversation_id": str(conversation_id or ""),
            "history_epoch": epoch,
            "snapshot_fingerprint": hashlib.sha256(history.encode("utf-8")).hexdigest(),
            "rows": normalized, "source_refs": refs, "diagnostics": ref_diagnostics}


def _narrative_scope_key(scope):
    """Mirror LivingMemory's public v1 scope key for response validation."""
    keys = ("platform", "umo", "persona_id", "instance_id")
    if not isinstance(scope, dict):
        return ""
    values = {key: scope.get(key, "") for key in keys}
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        return ""
    values = {key: value.strip() for key, value in values.items()}
    encoded = json.dumps(values, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "narrative:v1:" + hashlib.sha256(encoded).hexdigest()


def _validated_evidence_view(view, scope, purpose, card_version):
    """Validate the complete LivingMemory v1 view before using any of its atoms."""
    expected_scope_key = _narrative_scope_key(scope)
    if (not expected_scope_key or not isinstance(view, dict)
            or type(view.get("schema_version")) is not int or view.get("schema_version") != 1
            or view.get("available") is not True or view.get("purpose") != purpose
            or view.get("card_version") != card_version
            or view.get("scope_key") != expected_scope_key
            or type(view.get("correction_revision")) is not int
            or view["correction_revision"] < 0
            or not isinstance(view.get("atoms"), list)
            or not isinstance(view.get("slots"), dict)
            or not isinstance(view.get("history_projection"), dict)
            or not isinstance(view.get("revocations"), list)):
        return None
    return copy.deepcopy(view)


def _unavailable_evidence_status(view):
    """Keep a typed failure envelope diagnostic-only, without accepting its data."""
    if (not isinstance(view, dict) or view.get("available") is not False
            or view.get("atoms") != [] or view.get("history_projection") != {}
            or not isinstance(view.get("slots"), dict)
            or any(view["slots"].values())):
        return None
    status = view.get("evidence_status")
    return status if status in {"timeout", "unavailable", "pending", "deferred"} else None


def _merge_evidence_views(host_view, recall_view):
    """Merge views at one correction revision, preserving typed source identity."""
    if not isinstance(host_view, dict):
        return None
    if not isinstance(recall_view, dict):
        return copy.deepcopy(host_view)
    if (host_view.get("scope_key") != recall_view.get("scope_key")
            or host_view.get("purpose") != recall_view.get("purpose")
            or host_view.get("card_version") != recall_view.get("card_version")
            or host_view.get("correction_revision") != recall_view.get("correction_revision")
            or host_view.get("revocations") != recall_view.get("revocations")):
        return None

    def atoms_by_identity(view):
        result = {}
        conflicts = set()
        revoked_ids = set()
        for revocation in view.get("revocations", []):
            if not isinstance(revocation, dict):
                continue
            target = revocation.get("target_id")
            if isinstance(target, str) and target:
                revoked_ids.add(target)
            affected = revocation.get("affected_ids", [])
            if isinstance(affected, list):
                revoked_ids.update(value for value in affected if isinstance(value, str) and value)
            replacement = revocation.get("replacement_atom_id")
            if isinstance(replacement, str) and replacement:
                revoked_ids.discard(replacement)
        for atom in view.get("atoms", []):
            if (not isinstance(atom, dict) or not isinstance(atom.get("atom_id"), str)
                    or not atom["atom_id"] or not isinstance(atom.get("claim"), str)
                    or not atom["claim"].strip() or not isinstance(atom.get("source_refs"), list)
                    or not atom["source_refs"]):
                continue
            if atom["atom_id"] in conflicts:
                continue
            atom_scope = atom.get("scope")
            if not isinstance(atom_scope, dict) or atom_scope.get("scope_key") != view.get("scope_key"):
                continue
            refs = [ref for ref in atom["source_refs"]
                    if isinstance(ref, dict) and isinstance(ref.get("material_id"), str)
                    and ref.get("material_id")]
            if not refs:
                continue
            root_evidence = atom.get("root_evidence_ids", [])
            root_material = atom.get("root_material_ids", [])
            root_evidence = root_evidence if isinstance(root_evidence, list) else []
            root_material = root_material if isinstance(root_material, list) else []
            identity_ids = {atom["atom_id"], *(value for value in root_evidence if isinstance(value, str)),
                            *(value for value in root_material if isinstance(value, str))}
            identity_ids.update(ref_id for ref in refs for ref_id in
                                (ref.get("material_id"), ref.get("source_event_id"))
                                if isinstance(ref_id, str) and ref_id)
            if identity_ids & revoked_ids:
                continue
            candidate = copy.deepcopy(atom)
            candidate["source_refs"] = refs
            candidate["root_evidence_ids"] = sorted({value for value in root_evidence
                                                      if isinstance(value, str) and value})
            candidate["root_material_ids"] = sorted({value for value in root_material
                                                      if isinstance(value, str) and value})
            prior = result.get(candidate["atom_id"])
            if prior is None:
                result[candidate["atom_id"]] = candidate
                continue
            if prior.get("claim") != candidate.get("claim") or prior.get("kind") != candidate.get("kind"):
                result.pop(candidate["atom_id"], None)
                conflicts.add(candidate["atom_id"])
                continue
            prior["root_evidence_ids"] = sorted(set(prior.get("root_evidence_ids", [])) |
                                                 set(candidate.get("root_evidence_ids", [])))
            prior["root_material_ids"] = sorted(set(prior.get("root_material_ids", [])) |
                                                 set(candidate.get("root_material_ids", [])))
            by_ref = {json.dumps(ref, sort_keys=True, ensure_ascii=False): ref
                      for ref in [*prior.get("source_refs", []), *candidate["source_refs"]]}
            prior["source_refs"] = list(by_ref.values())[:20]
        return result, conflicts

    host_atoms, host_conflicts = atoms_by_identity(host_view)
    recall_atoms, recall_conflicts = atoms_by_identity(recall_view)
    conflicts = host_conflicts | recall_conflicts
    merged_atoms = dict(host_atoms)
    for atom_id, atom in recall_atoms.items():
        if atom_id in conflicts:
            continue
        if atom_id not in merged_atoms:
            merged_atoms[atom_id] = atom
        else:
            prior = merged_atoms[atom_id]
            if prior.get("claim") == atom.get("claim") and prior.get("kind") == atom.get("kind"):
                prior["root_evidence_ids"] = sorted(set(prior.get("root_evidence_ids", [])) |
                                                     set(atom.get("root_evidence_ids", [])))
                prior["root_material_ids"] = sorted(set(prior.get("root_material_ids", [])) |
                                                     set(atom.get("root_material_ids", [])))
                by_ref = {json.dumps(ref, sort_keys=True, ensure_ascii=False): ref
                          for ref in [*prior.get("source_refs", []), *atom.get("source_refs", [])]}
                prior["source_refs"] = list(by_ref.values())[:20]
            else:
                merged_atoms.pop(atom_id, None)
                conflicts.add(atom_id)
    for atom_id in conflicts:
        merged_atoms.pop(atom_id, None)

    merged = copy.deepcopy(host_view)
    merged["atoms"] = list(merged_atoms.values())
    slots = {}
    for view in (host_view, recall_view):
        for name, rows in view.get("slots", {}).items():
            if not isinstance(name, str) or not isinstance(rows, list):
                continue
            target = slots.setdefault(name, {})
            for row in rows:
                if not isinstance(row, dict) or row.get("atom_id") not in merged_atoms:
                    continue
                target[row["atom_id"]] = copy.deepcopy(merged_atoms[row["atom_id"]])
    merged["slots"] = {name: list(rows.values())[:40] for name, rows in slots.items()}
    # Only the host call is bound to this conversation snapshot. Recall-view
    # projections may describe other source positions and cannot be aligned by
    # text similarity or a reused row index.
    merged["history_projection"] = copy.deepcopy(host_view.get("history_projection", {}))
    merged["diagnostics"] = {
        "source_text_logged": False,
        "raw_source_returned": False,
        "projection_version": "evidence-view-v1",
        "purpose": host_view.get("purpose"),
        "merged_view_count": 2,
    }
    return merged
REPLY_GUIDANCE = """根据已确定角色、当前状态和来源明确的上下文，生成角色实际会发出的消息。
先回应本轮具体问题、动作或情绪，再自然体现人设。当前明确更正优先于同一任务的旧条件；背景里的旧问题不是本轮要回答的问题。不要为了展示记忆而搬出无关经历或日记。
分清当前发言者、回应对象、被引用者和新人。group_events 是平台事实，不是成员台词；说欢迎的人不一定是新人。缺失信息确实妨碍作答时，只澄清关键一点，不猜身份、关系、动机或对方没说过的要求。
遵守 response_plan 的对象、动作和长度。可以确认、澄清、拒绝或有限帮助，不必自动扩展为完整答题服务；需要回答时先给有用的核心内容，不以角色描写代替答案。
普通关心、问候、感谢和善意玩笑先按善意理解。对当前关系中亲近的人，可以自然表达安心、愿意或关心；短句也要回应具体内容。亲近不等于同意每个动作，拒绝时只说明这次边界，不否定整段关系。
保留核心人格与自主性，但不靠固定的省略号、反问、抱怨、自称或拒绝证明人设。慢热不等于冷淡，嘴硬不等于讥笑善意；旧回复的句式不是本轮模板。
把吃饭、起床、休息、走动等日常留在背景，让有效状态自然影响语气、长短和能否继续聊。直接接当前话题，不在无关聊天前加第一人称动作引子，也不靠硬比喻制造关联。只有对方问起、约好要反馈的进展确实发生、当前状态实际限制回应，或日常细节确实能推进当前话题时，才简短提及。未知或过期状态不证明困倦，不把一时疲惫写成长期疏远，也不临时编造活动解释新消息。
humanlike.preferences是自己的稳定偏好，可自然影响选题和看法，不需要每次展示或迎合对方而反转。
当前发言明确问起你的实际生活状态或正在做什么时，可按需调用可用的 get_dynamics_world_state。只回答已确认的相关状态，查不到时保留未知。旧活动、喜好和计划不能证明你此刻在做什么。
humanlike.moods只针对当前成员，按strength保留轻微余韵并逐渐淡化；当前话题和新反馈优先。认真话题可以收起玩笑，安静不等于冷淡，不能把对别人或先前话题的不满迁移过来。
humanlike.corrections是当前成员已明确提出的纠正，text说明后续应怎样调整；只在其topic适用时执行，空topic表示对该成员的一般表达偏好。当前明确反馈优先于旧理解、旧建议和旧回复习惯。先修正理解再继续实际话题，不只道歉，不每轮复述“我记住了”，不让旧记忆把被纠正的说法重新带回来。不把成员主张当作外部事实，也不赋予工具权限或改变核心人格。
humanlike.interests记录持续兴趣的当前进展和next_step。相关话题再出现时从已有进展接下去，可补充新理解，不重复从头介绍；paused不催，done不冒充仍待完成。discussion只是讨论过，execution才有实际行动回执；next_step尚未发生，不据此主动插话或编造体验。
humanlike.moods的cause_quote和topic说明余波从何而来；换题、认真求助或收到纠正时重新判断，不能用旧情绪抵消新反馈。
humanlike.nicknames是你对当前成员的专属称呼记录，不是对方已认可的证明。active可在合适时偶尔使用；没有记录时也允许依据真实共同经历或对方名字自然起一个友善、不过度亲昵的昵称，不要求每人都有。当前成员说不喜欢就立即停用，rejected时用其正常显示名，不自行另取替代昵称；只有本人明确重新要求才恢复。不要给别人套用同一记录，不用昵称代替身份判断。
表达轻重随内容：一句吐槽可以只接一句，认真倾诉就具体回应；不把每次闲聊写成解释、建议或总结。对“哈哈、嗯嗯、好、知道了、晚安”等已经结束的话，若仍需回应就简短收住，不硬加新问题、话题或小打算；存在尚未回答的具体问题时先回答。
续聊时先接住对方新增的回答、解释和纠正。conversation.dialogue 的 pending_question、user_updates 和 next_step 指明上一轮问了什么、对方已补充什么；已经回答的问题不要再问。适度补充一个相关看法、具体细节或有意义的问题，让话题有进展，不每轮都反问。短暂的“哈哈、嗯嗯”不等于话题终止；对方继续给出实质内容，就自然接上，不要求重新叫名字或@。原来的温和收尾不等于拒绝后续交流，明确停止或互动边界仍须遵守。
事实、主观猜测、计划、开始执行和完成结果要分开。承诺保留原条件和截止时间，未经来源或回执证实，不宣称已开始或已完成，也不编造拖延原因。
system_receipts 是系统执行事实：生成失败不等于角色手滑、误发、厌烦或故意不理人。用户问起才简短说明未能生成回复，原因未知就保持未知，不把技术失败写进角色经历。
谈及旧话，区分可核对原话与当前态度；不替过去补编动机，不责怪对方提问。可以更正当前意思、承认影响，同时保留合理边界。
只输出要发送的角色消息，不输出判定过程、上下文字段、心理分析或无关的解释。拒绝当应声虫：不做“确实/有道理/太真实了/我也觉得”式空泛附和或同义复述；非严肃求助与非沉重倾诉场景下，优先贴合人设整活（接梗升级、反差吐槽、脑洞延伸）或亮出自己鲜明的见解与偏好（允许温和反驳或换角度切入），公开讨论中若除了附和没新角度则直接 silent。避免重复和习惯性追问；遵守有效 silence/shield 边界。真实平台 @、引用和私聊也允许按人设与语境主动沉默；需要回应时，忙碌可简短说明限制。最终输出遵守宿主提供的回应方式协议。"""


REPLY_GUIDANCE += "\n" + dynamics_horizons.HORIZONS_REPLY_GUIDANCE
DECISION_GUIDANCE += "\n环境天气只帮助理解角色当前处境，不是新消息、接话义务或主动联系授权；不要因为天气字段存在而主动插话。"
DECISION_GUIDANCE += "\n" + dynamics_horizons.HORIZONS_DECISION_GUIDANCE


@dataclass(frozen=True)
class TurnContextBundle:
    """Store serialized material so downstream consumers cannot mutate a turn."""

    turn_id: str
    payload: str

    def _wire_view(self):
        cached = getattr(self, "_wire_cache", None)
        if cached is None:
            cached = json.loads(self.payload)
            object.__setattr__(self, "_wire_cache", cached)
        return cached

    def wire(self):
        return copy.deepcopy(self._wire_view())

    @property
    def bundle_id(self):
        cached = getattr(self, "_bundle_id_cache", None)
        if cached is None:
            cached = hashlib.sha256(self.payload.encode()).hexdigest()
            object.__setattr__(self, "_bundle_id_cache", cached)
        return cached

    @property
    def scope(self):
        return copy.deepcopy(self._wire_view()["scope"])

    @property
    def snapshot(self):
        return copy.deepcopy(self._wire_view()["snapshot"])

    def decision_context(self, budget=2400):
        data = self._wire_view()
        snapshot = speaker_snapshot(data["snapshot"], data.get("speaker_id", ""), data.get("captured_at", 0))
        evidence = data.get("evidence_view")
        raw_slots = evidence.get("slots", {}) if isinstance(evidence, dict) and evidence.get("available") is True else {}
        # Evidence atoms already occupy their typed slots; do not duplicate the
        # same claims in context_items and let them consume the budget twice.
        raw_items = [row for row in data.get("context_items", [])
                     if not (isinstance(row, dict) and row.get("provenance_kind") == "evidence_view_atom")]
        items = decision_materials(raw_items)
        base = {"revision": snapshot.get("revision"), "scope_revision": snapshot.get("scope_revision"),
                "availability": snapshot.get("availability", "unknown"),
                "activity": excerpt(activity_text(snapshot), 220),
                "energy": energy_context(snapshot), "stale": bool(snapshot.get("stale")),
                "valid_until": snapshot.get("valid_until"),
                "subjective_state": excerpt(snapshot.get("subjective_state", ""), 160),
                "summary": excerpt(snapshot.get("summary", ""), 220),
                "commitments": select_commitments(snapshot.get("commitments", []), data.get("speaker_id", ""), data.get("captured_at", 0))}
        if snapshot.get("humanlike"):
            base["humanlike"] = snapshot["humanlike"]
        environment=environment_context(snapshot,data.get('captured_at',0),forecast_limit=2)
        if environment is not None:
            base["environment"] = environment
        scene=scene_context(snapshot)
        if scene is not None:
            scene.pop("receipts", None)
            scene.pop("visible_items", None)
            base['scene']=scene
        visual=world_visual_context(snapshot,data.get('captured_at',0),compact=True)
        if visual is not None:
            base["world_visual"] = visual
        horizons_slot = data.get("horizons_decision_slot")
        if isinstance(horizons_slot, dict) and horizons_slot:
            base["horizons"] = copy.deepcopy(horizons_slot)

        def _dump_len(obj):
            return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

        base_empty_len = _dump_len({**base, "context_items": []})
        item_lens = [_dump_len(row) for row in items]
        items_overhead = sum(item_lens) + max(0, len(item_lens) - 1)
        while True:
            used = base_empty_len + items_overhead
            slot_budget = max(0, int(budget) - used)
            slots, diagnostics = select_evidence_slots(raw_slots, slot_budget)
            result = {**base, "context_items": list(items), "evidence_slots": slots, "evidence_slot_diagnostics": diagnostics}
            if _dump_len(result) <= budget or not items:
                break
            items.pop()
            popped = item_lens.pop()
            items_overhead -= popped + (1 if item_lens else 0)
        # Snapshot summaries and external media hints are optional; current life
        # commitments and all current/boundary atoms are never truncated to satisfy the soft budget.
        if _dump_len(result) > budget and base.get("summary"):
            base["summary"] = ""
            skeleton = {**base, "context_items": items}
            slot_budget = max(0, int(budget) - _dump_len(skeleton))
            slots, diagnostics = select_evidence_slots(raw_slots, slot_budget)
            result = {**skeleton, "evidence_slots": slots, "evidence_slot_diagnostics": diagnostics}
        if _dump_len(result) > budget and "horizons" in base:
            if isinstance(base["horizons"], dict) and "open_question" in base["horizons"]:
                base["horizons"].pop("open_question", None)
                skeleton = {**base, "context_items": items}
                slot_budget = max(0, int(budget) - _dump_len(skeleton))
                slots, diagnostics = select_evidence_slots(raw_slots, slot_budget)
                result = {**skeleton, "evidence_slots": slots, "evidence_slot_diagnostics": diagnostics}
            if _dump_len(result) > budget:
                base.pop("horizons", None)
                skeleton = {**base, "context_items": items}
                slot_budget = max(0, int(budget) - _dump_len(skeleton))
                slots, diagnostics = select_evidence_slots(raw_slots, slot_budget)
                result = {**skeleton, "evidence_slots": slots, "evidence_slot_diagnostics": diagnostics}
        return result


class NarrativeCoordinator:
    """Own managed contexts and sends while life remains the state authority."""

    _MAX_BUNDLES = 256
    _MAX_SYNCED_FAILED_INTENTS = 512

    def __init__(self, plugin, path):
        self.plugin = plugin
        self.journal = NarrativeJournal(path)
        from .journal_io import JournalIO, ReceiptOutbox
        self._io = JournalIO(logger)
        self._receipt_outbox = ReceiptOutbox(self.journal.path.parent / 'pending-receipts')
        self._receipt_tasks = set()
        self._unspooled_receipts = {}
        self.task = None
        self._dispatch_lock = asyncio.Lock()
        self._intent_lock = asyncio.Lock()
        self._bundles = OrderedDict()
        self._synced_failed_intents = set()
        self._shared_reads = SharedReads()
        self._read_memo = ExactMemo(ttl=10, capacity=64)
        self._rpc_timings = CallTimings()
        self._revision_reads = {}
        self._revision_joins = 0
        self._revision_checks = set()
        self._social_opportunity_at = {}
        # (provider, method) -> [上次 WARNING 的 monotonic 时刻, 该窗口内被压制的条数]
        self._rpc_warn_at: dict[tuple[str, str], list] = {}

    def _cache_bundle(self, umo: str, bundle: TurnContextBundle) -> None:
        self._bundles[umo] = bundle
        if isinstance(self._bundles, OrderedDict):
            self._bundles.move_to_end(umo)
            while len(self._bundles) > self._MAX_BUNDLES:
                self._bundles.popitem(last=False)

    def enabled(self, umo):
        cfg = self.plugin._runtime_config
        if not getattr(cfg, "narrative_enabled", False):
            return False
        if private_message_recipient(umo):
            return bool(getattr(cfg, "narrative_private_chat_enabled", False) and self._takeover_enabled(umo))
        parts = str(umo or "").split(":", 2)
        return bool(
            len(parts) == 3 and all(parts) and parts[1].lower() == "groupmessage"
            and (getattr(cfg, "narrative_all_groups", False) or umo in getattr(cfg, "narrative_sessions", ()))
        )

    def _takeover_enabled(self, umo):
        p = self.plugin
        if private_message_recipient(umo):
            cfg = p._runtime_config
            return bool(getattr(cfg, "enabled", True) and getattr(cfg, "private_chat_enabled", False))
        parts = str(umo or "").split(":", 2)
        return bool(len(parts) == 3 and all(parts) and parts[1].lower() == "groupmessage"
                    and p.is_group_takeover_enabled(parts[2]))

    def horizon_outreach_allowed(self, umo):
        if not self._takeover_enabled(umo):
            return False
        current = dynamics_horizons.provider(self.plugin)
        fn = getattr(current,'horizons_outreach_allowed',None)
        return bool(callable(fn) and fn(umo))

    def intent_outreach_allowed(self, intent):
        umo=(intent.get('scope') or {}).get('umo','')
        if self._intent_provider(intent.get('intent_id'),intent)=='dynamics_horizons':
            return self.horizon_outreach_allowed(umo)
        return self.outreach_allowed(umo)

    def outreach_allowed(self, umo):
        cfg = self.plugin._runtime_config
        allowed = getattr(cfg, "narrative_outreach_sessions", ())
        return bool(getattr(cfg, "narrative_outreach_enabled", False)
                    and self.enabled(umo) and (not allowed or umo in allowed)
                    and self._takeover_enabled(umo))

    def _rpc_timeout(self, timeout):
        """Resolve the wait budget for one named RPC call.

        An explicit caller value always wins; otherwise the plugin setting is
        used, falling back to DEFAULT_RPC_TIMEOUT_SECONDS when it is unusable.
        """
        if timeout is not None:
            try:
                explicit = float(timeout)
            except (TypeError, ValueError):
                explicit = 0.0
            if explicit > 0:
                return explicit
        config = getattr(self.plugin, "_runtime_config", None)
        configured = getattr(config, "narrative_rpc_timeout_seconds", None)
        try:
            value = float(configured)
        except (TypeError, ValueError):
            value = DEFAULT_RPC_TIMEOUT_SECONDS
        if not math.isfinite(value) or value <= 0:
            value = DEFAULT_RPC_TIMEOUT_SECONDS
        # A typo in config must not be able to stall a reply indefinitely.
        return min(value, 60.0)

    def _evidence_timeout(self) -> float:
        """解析证据视图（livingmemory.get_evidence_view）的单次等待预算。

        与 _rpc_timeout 分开是因为这条调用的耗时分布明显更重（实测中位 4.5~5.5s、
        长尾 7.9s），需要独立可调的档位；原先写死常量时运维无法在不改代码的情况下调档。

        Returns:
            预算秒数、已钳制到 [1, 120]；配置缺失/非法时用默认值。
        """
        config = getattr(self.plugin, "_runtime_config", None)
        configured = getattr(config, "narrative_evidence_timeout_seconds", None)
        try:
            value = float(configured)
        except (TypeError, ValueError):
            value = DEFAULT_EVIDENCE_VIEW_TIMEOUT_SECONDS
        if not math.isfinite(value) or value <= 0:
            value = DEFAULT_EVIDENCE_VIEW_TIMEOUT_SECONDS
        return min(max(value, 1.0), 120.0)

    @staticmethod
    def _rpc_outlier(elapsed_ms: float, budget: float) -> str:
        """Classify a finished call against its budget, for post-deploy tuning."""
        if not budget or budget <= 0:
            return "no"
        ratio = (elapsed_ms / 1000.0) / budget
        if ratio >= 0.95:
            return "full_budget"
        if ratio >= RPC_SLOW_RATIO:
            return "slow"
        return "no"

    def _log_rpc_unavailable(self, provider, method, exc, elapsed_ms=0.0, budget=0.0) -> None:
        """WARN about an unavailable capability, at most once per throttle window.

        elapsed_ms / outlier 用于上线后按实测耗时决定预算是否合适；窗口内被压制的
        次数会累计到下一次实际输出的 WARNING 上，因此不会丢失频次信息。
        """
        key = (str(provider), str(method))
        now = time.monotonic()
        entry = self._rpc_warn_at.get(key)
        if entry is not None and now - entry[0] < RPC_WARN_INTERVAL_SECONDS:
            entry[1] += 1
            return
        suppressed = int(entry[1]) if entry is not None else 0
        self._rpc_warn_at[key] = [now, 0]
        logger.warning(
            "Narrative capability unavailable provider=%s method=%s type=%s "
            "elapsed_ms=%.1f budget_s=%.1f outlier=%s suppressed_since_last=%d "
            "(identical warnings throttled for %.0fs)",
            provider, method, type(exc).__name__, elapsed_ms, budget,
            self._rpc_outlier(elapsed_ms, budget), suppressed, RPC_WARN_INTERVAL_SECONDS,
        )

    async def call(self, provider, method, *args, timeout=None, on_unavailable=None):
        """Resolve one capability, with an optional failure reason for the caller."""
        scope = _READ_SCOPE.get()
        started = time.monotonic() if scope is not None else None
        result = await self._coalesced_call(provider, method, *args, timeout=timeout)
        if not isinstance(result, _RpcFailure):
            return result
        # A shared task carries the creator's context. Report each waiter's
        # phase and turn here after the result has been delivered to it.
        if scope is not None:
            trace = scope.trace
            logger.info(
                "Narrative prepare RPC unavailable turn_id=%s session_id=%s phase=%s provider=%s method=%s "
                "reason=%s elapsed_ms=%.1f budget_s=%.3g",
                trace.turn_id, trace.session_id, trace.phase, provider, method, result.reason,
                (time.monotonic() - started) * 1000, result.budget,
            )
        if on_unavailable is not None:
            on_unavailable(result.reason)
        return None

    async def _coalesced_call(self, provider, method, *args, timeout=None):
        if method in {"recall_context", "get_evidence_view", "get_social_context", "get_social_snapshot", "get_profiles", "get_snapshot"}:
            instance = self.plugin.integrations.narrative_provider(provider)
            revision = await self._read_revision(instance, provider, args[0] if args else None)
            if revision is None:
                # No published version means no safe coalescing. In particular,
                # a relation correction must never inherit an older in-flight read.
                return await self._call(provider, method, *args, timeout=timeout)
            try:
                serialized = json.dumps(args, sort_keys=True, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                return await self._call(provider, method, *args, timeout=timeout)
            key = (id(instance), provider, method, revision, hashlib.sha256(serialized.encode()).hexdigest(), timeout)
            scope = _READ_SCOPE.get()
            if method in _MEMO_METHODS and scope is not None and time.monotonic() < scope.expires_at:
                async def load():
                    result = await self._call(provider, method, *args, timeout=timeout)
                    after = await self._read_revision(instance, provider, args[0])
                    valid = (after == revision and self.plugin.integrations.narrative_provider(provider) is instance
                             and isinstance(result, dict) and result.get('schema_version') == 1
                             and result.get('available', True) is not False and not result.get('disabled'))
                    if valid and provider == 'livingmemory' and method == 'get_evidence_view':
                        diagnostics = result.get('diagnostics', {})
                        if isinstance(diagnostics, dict) and any(diagnostics.get(k, 0) for k in
                                ('materials_pending', 'materials_admission_pending', 'materials_queued', 'materials_deferred')):
                            valid = False
                    return result, valid
                result, _ = await self._read_memo.read((scope.key, key), load, cacheable=lambda row: row[1])
                if not isinstance(result, _RpcFailure) and self.plugin.integrations.narrative_provider(provider) is not instance:
                    return _RpcFailure('scope_changed')
                return result
            return await self._shared_reads.read(key, lambda: self._call(provider, method, *args, timeout=timeout))
        return await self._call(provider, method, *args, timeout=timeout)

    def call_diagnostics(self):
        return {'window': 'since_reload', 'rpc': self._rpc_timings.snapshot(), 'memo': self._read_memo.snapshot(),
                'shared_read_joins': self._shared_reads.joins, 'revision_read_joins': self._revision_joins}

    async def _read_revision(self, instance, provider, scope):
        reader = getattr(instance, "peek_evidence_revision" if provider == "livingmemory" else "peek_context_revision", None)
        if not callable(reader) or not isinstance(scope, dict):
            return None
        try:
            read_key = (id(instance), provider, hashlib.sha256(json.dumps(scope, sort_keys=True, allow_nan=False).encode()).hexdigest())
        except (TypeError, ValueError):
            return None

        async def read():
            if inspect.iscoroutinefunction(reader):
                return await reader(scope)
            return await asyncio.to_thread(reader, scope)

        task = self._revision_reads.get(read_key)
        if task is None or task.done():
            if len(self._revision_checks) >= 2:
                return None
            create = getattr(self.plugin, "_create_background_task", None)
            task = create(read()) if callable(create) else asyncio.create_task(read())
            self._revision_checks.add(task)
            self._revision_reads[read_key] = task
        else:
            self._revision_joins += 1

        def completed(done):
            self._revision_checks.discard(done)
            if self._revision_reads.get(read_key) is done:
                self._revision_reads.pop(read_key, None)
            if not done.cancelled():
                done.exception()

        if not getattr(task, '_cd_revision_callback', False):
            task.add_done_callback(completed)
            task._cd_revision_callback = True
        try:
            value = await asyncio.wait_for(asyncio.shield(task), .03)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if not isinstance(value, dict) or value.get("available") is not True:
            return None
        revision = value.get("correction_revision") if provider == "livingmemory" else value.get("generation")
        if type(revision) is not int or revision < 0:
            return None
        return (str(value.get("scope_key", "")), revision)

    async def _call(self, provider, method, *args, timeout=None):
        """Use only live public methods, dropping results from unloaded instances.

        Args:
            provider: Named companion capability.
            method: Public method name.
            args: JSON-compatible interface arguments.
            timeout: Maximum wait in seconds; None uses the configured default.

        Returns:
            A versioned object, or an internal failure reason for each waiter.
        """
        registry = self.plugin.integrations
        instance = registry.narrative_provider(provider)
        fn = getattr(instance, method, None)
        if not callable(fn):
            return _RpcFailure('missing')
        budget = self._rpc_timeout(timeout)
        started = time.monotonic()
        read_scope = _READ_SCOPE.get()
        phase = read_scope.trace.phase if read_scope and started < read_scope.expires_at else 'other'
        try:
            if inspect.iscoroutinefunction(fn):
                result = await asyncio.wait_for(fn(*args), budget)
            else:
                # 同步 provider 的旧行为是在 wait_for 之前就在事件循环上跑完，
                # timeout 对它形同虚设；放进线程后超时才真正生效。
                result = await asyncio.wait_for(asyncio.to_thread(fn, *args), budget)
                if inspect.isawaitable(result):
                    # 同步包装器返回协程时保持旧行为：继续等待。
                    result = await asyncio.wait_for(result, budget)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            outlier = self._rpc_outlier(elapsed_ms, budget)
            if outlier == "no":
                # 正常快调用：生产 log_level=INFO 时不输出，零成本。
                logger.debug(
                    "Narrative capability call provider=%s method=%s elapsed_ms=%.1f "
                    "budget_s=%.1f outlier=%s",
                    provider, method, elapsed_ms, budget, outlier,
                )
            else:
                # 慢但成功（耗时 >= 预算的 50%）：生产 log_level=INFO 下必须可见，
                # 否则「5s 是否普遍不够」这个结论拿不到尾部样本。只对慢调用打
                # INFO，日志量仍然可控。
                logger.info(
                    "Narrative capability slow provider=%s method=%s elapsed_ms=%.1f "
                    "budget_s=%.1f outlier=%s",
                    provider, method, elapsed_ms, budget, outlier,
                )
            if registry.narrative_provider(provider) is not instance:
                return _RpcFailure('scope_changed', budget)
            if isinstance(result, (dict, list)):
                return result
            return _RpcFailure('missing' if result is None else 'invalid', budget)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            self._log_rpc_unavailable(provider, method, exc, elapsed_ms, budget)
            return _RpcFailure('timeout' if isinstance(exc, asyncio.TimeoutError) else 'unavailable', budget)
        finally:
            self._rpc_timings.observe(f'{phase}:{provider}.{method}', time.monotonic() - started)

    async def evidence_current_async(self, managed):
        """Off-loop variant of evidence_current() for async call sites.

        evidence_current() 本身保持同步：它还被 native_memory_bridge 的同步闭包
        与 persona_engine.valid() 调用。但 LivingMemory 的 revision 读取会取
        evidence sidecar 的 RLock，而后台写入线程可能正持锁做 SQLite I/O，
        因此在内联调用时事件循环可能被拖住。异步调用点应改走本包装器。
        """
        return await asyncio.to_thread(self.evidence_current, managed)

    def evidence_current(self, managed):
        """Synchronously validate a frozen evidence view using the public RAM API."""
        if not isinstance(managed, dict):
            return True
        view = managed.get("evidence_view")
        if not isinstance(view, dict):
            return True
        scope = managed.get("scope")
        if not isinstance(scope, dict):
            return False
        scope4 = {key: scope.get(key) for key in ("platform", "umo", "persona_id", "instance_id")}
        if not all(isinstance(value, str) and value for value in scope4.values()):
            return False
        registry = getattr(self.plugin, "integrations", None)
        service = registry.narrative_provider("livingmemory") if registry is not None else None
        reader = getattr(service, "peek_evidence_revision", None)
        if not callable(reader):
            return False
        try:
            current = reader(scope4)
        except Exception:
            return False
        if (view.get("available") is not True or not isinstance(current, dict)
                or type(current.get("schema_version")) is not int or current.get("schema_version") != 1
                or current.get("available") is not True):
            return False
        expected_scope_key = _narrative_scope_key(scope4)
        return (type(view.get("schema_version")) is int and view.get("schema_version") == 1 and
                view.get("scope_key") == expected_scope_key and
                view.get("purpose") in {"reply", "history_quote"} and
                isinstance(view.get("card_version"), str) and
                isinstance(view.get("atoms"), list) and isinstance(view.get("slots"), dict) and
                isinstance(view.get("history_projection"), dict) and isinstance(view.get("revocations"), list) and
                type(view.get("correction_revision")) is int and view.get("correction_revision") >= 0 and
                type(current.get("correction_revision")) is int and
                current.get("scope_key") == expected_scope_key and
                current.get("correction_revision") == view.get("correction_revision"))

    async def _owned_private_memory_scope(self, event, req, request_purpose):
        """Return only a fresh, owner-bound scope receipt for CD's private clone."""
        def unavailable(reason):
            return {"schema_version": 1, "status": "bypass", "reason": reason,
                    "purpose": request_purpose}

        raw_umo = getattr(event, "unified_msg_origin", None)
        if not isinstance(raw_umo, str) or not raw_umo.strip():
            return unavailable("scope_unavailable")
        umo = raw_umo.strip()
        parts = umo.rsplit(":", 2)
        if len(parts) != 3 or any(not part.strip() for part in parts):
            return unavailable("scope_unavailable")
        expected_platform = parts[0]

        bridge = getattr(getattr(self.plugin, "persona_engine", None), "bridge", None)
        persona_current = getattr(req, "_cd_private_persona_current_v1", None)
        owner = getattr(persona_current, "_cd_private_persona_owner_v1", None)

        async def current_accepted_persona():
            current_owner = getattr(persona_current, "_cd_private_persona_owner_v1", None)
            if (not callable(persona_current)
                    or getattr(req, "_cd_private_persona_current_v1", None) is not persona_current
                    or not isinstance(current_owner, tuple) or len(current_owner) != 2
                    or current_owner[0] is not req or current_owner[1] is not event):
                return False
            try:
                result = persona_current()
                if not inspect.isawaitable(result):
                    return False
                return await asyncio.wait_for(result, timeout=1.0) is True
            except asyncio.CancelledError:
                raise
            except Exception:
                return False

        if (bridge is None or not isinstance(owner, tuple) or len(owner) != 2
                or owner[0] is not req or owner[1] is not event
                or not await current_accepted_persona()):
            return unavailable("persona_snapshot_unavailable")
        try:
            persona = await bridge.snapshot(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable("persona_snapshot_unavailable")
        persona_id = getattr(persona, "persona_id", None)
        if not isinstance(persona_id, str) or not persona_id.strip():
            return unavailable("persona_snapshot_unavailable")
        if not await current_accepted_persona():
            return unavailable("persona_changed")

        try:
            character = await self.call("worldbook", "get_character_context", {
                "platform": expected_platform,
                "umo": umo,
                "persona_id": persona_id,
                "instance_id": persona_id,
            }, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable("character_context_unavailable")
        if (not isinstance(character, dict) or character.get("available", True) is not True
                or character.get("disabled") is True):
            return unavailable("character_context_unavailable")
        character_persona = character.get("persona_id")
        if not isinstance(character_persona, str) or not character_persona.strip():
            return unavailable("scope_identity_incomplete")
        worldbook_scope = character.get("scope")
        if not isinstance(worldbook_scope, dict):
            return unavailable("scope_identity_incomplete")
        scope = {key: worldbook_scope.get(key)
                 for key in ("platform", "umo", "persona_id", "instance_id")}
        if not all(isinstance(value, str) and value.strip() for value in scope.values()):
            return unavailable("scope_identity_incomplete")
        if (scope["umo"] != umo or scope["platform"] != expected_platform
                or scope["persona_id"] != character_persona):
            return unavailable("scope_identity_mismatch")
        card_version = effective_core_version(character)
        if not isinstance(card_version, str) or not card_version.strip():
            return unavailable("card_version_unavailable")

        # The request-local cap still refers to AgentBridge's accepted snapshot;
        # also compare the locally captured snapshot across the Worldbook await.
        if not await current_accepted_persona():
            return unavailable("persona_changed")
        is_current = getattr(bridge, "current", None)
        if not callable(is_current):
            return unavailable("persona_snapshot_unavailable")
        try:
            result = is_current(event, persona)
            if not inspect.isawaitable(result) or await asyncio.wait_for(result, timeout=1.0) is not True:
                return unavailable("persona_changed")
        except asyncio.CancelledError:
            raise
        except Exception:
            return unavailable("persona_changed")

        return {"schema_version": 1, "status": "skipped", "reason": "cd_owned_request",
                "scope": scope, "card_version": card_version,
                "purpose": request_purpose}

    async def prepare_memory_request(self, event, req, materials=None, purpose=None):
        """Public bridge for a companion's native request hook.

        The callback is intentionally conservative: only a complete host-history
        snapshot matching ``req.contexts`` can be projected. Current prompt,
        event identity, and protocol/tool rounds are left to their owners.
        """
        extras = getattr(event, "_extras", {}) if event is not None else {}
        request_purpose = history_request_purpose(getattr(req, "prompt", ""), purpose)
        owned_private = (
            getattr(event, "_chat_dynamics_owned_request", False) is True
            and getattr(event, "_cd_owned_original_input_v1", False) is True
            and is_private_message_event(event)
        )
        if owned_private:
            # Owned private turns require a fresh request-local persona guard and
            # a real Worldbook scope. They never reuse a stale projection receipt.
            return await self._owned_private_memory_scope(event, req, request_purpose)
        if isinstance(extras, dict) and extras.get(MANAGED_KEY):
            return {"schema_version": 1, "status": "skipped", "reason": "cd_owned_request",
                    "purpose": request_purpose}
        existing = getattr(req, "_cd_memory_feedback_v1", None)
        if (isinstance(existing, dict) and materials is None
                and existing.get("purpose") == request_purpose):
            return dict(existing)
        if event is None or req is None:
            return {"schema_version": 1, "status": "bypass", "reason": "request_identity_unavailable",
                    "purpose": request_purpose}
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        bridge = getattr(getattr(self.plugin, "persona_engine", None), "bridge", None)
        if not umo or bridge is None:
            return {"schema_version": 1, "status": "bypass", "reason": "scope_unavailable",
                    "purpose": request_purpose}
        try:
            persona = await bridge.snapshot(event)
        except Exception:
            return {"schema_version": 1, "status": "bypass", "reason": "persona_snapshot_unavailable",
                    "purpose": request_purpose}
        scope = {"platform": umo.split(":", 2)[0], "umo": umo,
                 "persona_id": persona.persona_id, "instance_id": persona.persona_id}
        character = await self.call("worldbook", "get_character_context", scope, event)
        if not isinstance(character, dict) or not character.get("available", True) or character.get("disabled"):
            return {"schema_version": 1, "status": "bypass", "reason": "character_context_unavailable",
                    "purpose": request_purpose}
        scope = character.get("scope") or scope
        scope4 = {key: scope.get(key) for key in ("platform", "umo", "persona_id", "instance_id")}
        if not all(isinstance(value, str) and value for value in scope4.values()):
            return {"schema_version": 1, "status": "bypass", "reason": "scope_identity_incomplete",
                    "purpose": request_purpose}
        manager = getattr(self.plugin.context, "conversation_manager", None)
        cid = str(persona.conversation_id or "")
        if not manager or not cid:
            return {"schema_version": 1, "status": "bypass", "reason": "conversation_identity_unavailable",
                    "scope": scope4, "purpose": request_purpose}
        try:
            conversation = await manager.get_conversation(umo, cid)
            snapshot = host_history_snapshot(getattr(conversation, "history", None), cid,
                                             getattr(conversation, "created_at", None))
        except Exception:
            snapshot = host_history_snapshot(None, cid)
        if not snapshot.get("snapshot_fingerprint"):
            return {"schema_version": 1, "status": "bypass", "reason": "history_snapshot_unavailable",
                    "scope": scope4, "conversation_id": cid, "purpose": request_purpose}
        card_version = effective_core_version(character)
        # 不再硬编码 2 秒（生产实测该预算持续 TimeoutError），也不跟随较小的默认预算：
        # 证据视图实测 3.3~4.8s，与 recall_context 同量级，故用同一档 8s。
        view = await self.call("livingmemory", "get_evidence_view", scope4, snapshot["source_refs"],
                               request_purpose, card_version, cid,
                               snapshot["snapshot_fingerprint"], materials,
                               timeout=self._evidence_timeout())
        view = _validated_evidence_view(view, scope4, request_purpose, card_version)
        if view is None:
            return {"schema_version": 1, "status": "bypass", "reason": "evidence_view_unavailable",
                    "scope": scope4, "conversation_id": cid,
                    "snapshot_fingerprint": snapshot["snapshot_fingerprint"], "purpose": request_purpose}
        contexts = getattr(req, "contexts", None)
        from .character_view import project_host_history_contexts
        _, diagnostic = project_host_history_contexts(contexts, snapshot["rows"], view)
        # This method only prepares immutable evidence. The reversible builder
        # alias installs provider-only projections after the real runner reset;
        # mutating req.contexts would contaminate the host's saved history.
        status = "prepared" if diagnostic.get("evidence_projection") == "active" else "bypass"
        reason = "" if status == "prepared" else diagnostic.get("reason", "projection_unavailable")
        diagnostic = {**diagnostic, "reason": reason, "native_history_covered": False}
        receipt = {"schema_version": 1, "status": status,
                   "reason": reason,
                   "provider": "astrbot_plugin_chat_dynamics",
                   "scope": scope4, "card_version": card_version,
                   "purpose": request_purpose, "conversation_id": cid,
                   "snapshot_fingerprint": snapshot["snapshot_fingerprint"],
                   "diagnostics": diagnostic, "evidence_view": view}
        try:
            setattr(req, "_cd_memory_feedback_v1", dict(receipt))
            setattr(req, "_cd_memory_feedback_snapshot_v1", {
                "conversation_id": cid, "snapshot_fingerprint": snapshot["snapshot_fingerprint"],
                "rows": snapshot["rows"]})
        except Exception:
            pass
        return receipt

    async def prepare(self, runtime, item, persona, *, lightweight=False):
        started = time.monotonic()
        enabled = self.enabled(runtime.umo)
        if lightweight and not enabled:
            return None
        turn = item.context
        trace = _PrepareTrace(
            PipelineTelemetry.turn_id(turn),
            hashlib.sha256(turn.session_key.encode()).hexdigest()[:32],
            'decision' if lightweight else 'reply',
            started,
        )
        if not enabled:
            trace.unavailable('enabled', 'unmanaged')
            return None
        identity = (getattr(runtime, 'session_key', turn.session_key), getattr(runtime, 'epoch', turn.epoch), turn.author, turn.revision,
                    tuple((m.message_id, m.text) for m in turn.messages),
                    persona.fingerprint, sorted(getattr(runtime, 'topic_stop_revisions', {}).items()),
                    getattr(self.plugin, '_turn_config_identity', lambda: '')())
        key = hashlib.sha256(repr(identity).encode()).hexdigest()
        token = _READ_SCOPE.set(_ReadScope(key, time.monotonic() + 10, trace))
        try:
            bundle = await self._prepare(runtime, item, persona, trace, lightweight=lightweight)
        finally:
            _READ_SCOPE.reset(token)
        if bundle is not None:
            self.start()
        return bundle

    async def _prepare(self, runtime, item, persona, trace, *, lightweight=False):
        """Resolve the character before acquiring one state and background bundle.

        Args:
            runtime: Owned group runtime.
            item: Attributed incoming turn.
            persona: Current host persona guard.
            trace: Correlation fields for this prepare invocation.

        Returns:
            An immutable bundle or None for the legacy request path.
        """
        umo = runtime.umo
        if not self.enabled(umo):
            trace.unavailable('enabled', 'unmanaged')
            return None
        event = item.events[-1]
        scope = {"platform": umo.split(":", 2)[0], "umo": umo,
                 "persona_id": persona.persona_id, "instance_id": persona.persona_id}
        character = await self.call(
            "worldbook", "get_character_context", scope, event,
            on_unavailable=lambda reason: trace.unavailable('character', reason),
        )
        if character is None:
            return None
        if character and not isinstance(character, dict):
            trace.unavailable('character', 'invalid')
            raise AttributeError('character context must be a mapping')
        if not character or character.get("schema_version") != 1:
            trace.unavailable('character', 'invalid')
            return None
        if character.get("disabled"):
            trace.unavailable('character', 'unmanaged')
            return None
        if not character.get("available", True):
            reason = character.get('reason')
            if reason == 'no_character':
                reason = 'missing'
            elif reason == 'scope_mismatch':
                reason = 'scope_changed'
            else:
                reason = 'unavailable'
            trace.unavailable('character', reason)
            return None
        try:
            scope = character.get("scope") or dict(
                scope, persona_id=character["persona_id"], instance_id=character.get("instance_id", character["persona_id"])
            )
            if scope.get("umo") != umo:
                trace.unavailable('character', 'scope_changed')
                return None
            scope = dict(scope)
        except (AttributeError, KeyError, TypeError, ValueError):
            trace.unavailable('character', 'invalid')
            raise
        character_name = character.get("name")
        if isinstance(character_name, str) and character_name.strip():
            # Historical persona display names come from the authoritative card.
            # They do not alter the four-field scope identity or open other groups.
            scope["persona_aliases"] = [character_name.strip()[:100]]
        snapshot = await self.call(
            "dynamic_life_state", "get_snapshot", scope,
            on_unavailable=lambda reason: trace.unavailable('snapshot', reason),
        )
        if snapshot is None:
            return None
        if snapshot and not isinstance(snapshot, dict):
            trace.unavailable('snapshot', 'invalid')
            raise AttributeError('lifecycle snapshot must be a mapping')
        if not snapshot or snapshot.get("schema_version") != 1:
            trace.unavailable('snapshot', 'invalid')
            return None
        if not snapshot.get("managed"):
            trace.unavailable('snapshot', 'unmanaged')
            return None
        participants = list(dict.fromkeys([item.context.author, *[m.author for m in item.context.messages]]))
        care_target = getattr(event, '_narrative_care_target_v1', '')
        if care_target and care_target not in participants:
            participants.append(care_target)
        boundary_state = member_boundary.resolve_state(self.plugin, event)
        if not boundary_state.available and (member_boundary.fail_closed(boundary_state)
                                             or boundary_state.service is not None):
            trace.unavailable('boundary_state', 'boundary_unavailable')
            return None
        if boundary_state.available and not boundary_state.disabled:
            visible = []
            for participant in participants:
                policy, _, state = member_boundary.peek(self.plugin, event, participant)
                if not state.available and (member_boundary.fail_closed(state) or state.service is not None):
                    trace.unavailable('boundary_policy', 'boundary_unavailable')
                    return None
                if str((policy or {}).get("mode") or "").lower() != "shield":
                    visible.append(participant)
            participants = visible
        scope4 = {key: scope.get(key) for key in ("platform", "umo", "persona_id", "instance_id")}
        card_version = effective_core_version(character)
        purpose = history_request_purpose(item.context.text)
        recall_query = item.context.text
        if item.context.wake_kind == "poke":
            recall_query = json.dumps(
                {
                    "event": "当前群成员戳了机器人，检索该来源者的相关互动背景，不执行旧请求",
                    "initiator_id": item.context.author,
                    "recent_context": [
                        {"message_id": m.message_id, "author_id": m.author, "text": excerpt(m.text, 280)}
                        for m in item.context.background
                        if any(p.relation in {"poke_sender", "poke_reply"} for p in m.context_evidence)
                    ][-4:],
                },
                ensure_ascii=False,
            )
        calls = [
            ("livingmemory", "recall_context", (scope4, recall_query, purpose, card_version)),
            ("self_learning", "get_social_context", (scope4, participants, purpose, card_version, None)),
            ("portrayal", "get_profiles", (scope, participants)),
        ]

        async def load_host_evidence():
            """Read the independent host snapshot concurrently with recall.

            Returns:
                The captured history snapshot and its validated-source view.
            """
            history_snapshot = {
                "conversation_id": str(persona.conversation_id or ""),
                "snapshot_fingerprint": "",
                "rows": [],
                "source_refs": [],
            }
            if history_snapshot["conversation_id"]:
                try:
                    manager = getattr(self.plugin.context, "conversation_manager", None)
                    conversation = (
                        await manager.get_conversation(umo, history_snapshot["conversation_id"])
                        if manager is not None
                        else None
                    )
                    history_snapshot = host_history_snapshot(
                        getattr(conversation, "history", None),
                        history_snapshot["conversation_id"],
                        getattr(conversation, "created_at", None),
                    )
                except Exception as exc:
                    logger.info("Narrative history snapshot unavailable type=%s", type(exc).__name__)
            host_view = None
            if (
                history_snapshot.get("conversation_id")
                and history_snapshot.get("snapshot_fingerprint")
                and all(isinstance(value, str) and value for value in scope4.values())
                and card_version
            ):
                host_view = await self.call(
                    "livingmemory",
                    "get_evidence_view",
                    scope4,
                    history_snapshot["source_refs"],
                    purpose,
                    card_version,
                    history_snapshot["conversation_id"],
                    history_snapshot["snapshot_fingerprint"],
                    timeout=self._evidence_timeout(),
                )
            return history_snapshot, host_view

        # Host history and recall are independent. Keep revision validation
        # below so overlapping reads can never mix correction snapshots.
        results = ([None, await self.call("self_learning", "get_social_snapshot", scope4, participants,
                                        purpose, card_version, timeout=1), None, ({"conversation_id": str(persona.conversation_id or ""),
                    "snapshot_fingerprint": "", "rows": [], "source_refs": []}, None)] if lightweight else list(await asyncio.gather(
            *(
                self.call(
                    name,
                    method,
                    *args,
                    timeout=MEMORY_RECALL_TIMEOUT_SECONDS if name == "livingmemory" else None,
                )
                for name, method, args in calls
            ),
            load_host_evidence(),
        )))
        history_snapshot, host_view = results.pop()
        context_items = []
        completed = {"worldbook": True, "dynamic_life_state": True}
        availability = {"worldbook": True, "dynamic_life_state": True}
        raw_recall_view = None
        livingmemory_result = None
        for (name, _, _), result in zip(calls, results):
            completed[name] = not lightweight or (name == "self_learning" and isinstance(result, dict))
            availability[name] = bool(result and result.get("available", True))
            if name == "livingmemory":
                livingmemory_result = result if isinstance(result, dict) else None
                raw_recall_view = ((livingmemory_result or {}).get("evidence_view")
                                   if livingmemory_result is not None else None)
                status = livingmemory_result.get("evidence_status") if livingmemory_result else None
                if status in {"ready", "empty", "pending", "partial", "deferred", "invalid", "unavailable", "timeout"}:
                    availability["livingmemory_evidence_status"] = status
                    availability["livingmemory_recall_status"] = status
                diagnostics = livingmemory_result.get("diagnostics") if livingmemory_result else None
                if isinstance(diagnostics, dict):
                    safe_counts = {key: value for key, value in diagnostics.items()
                                   if key in {"accepted", "resolved", "deferred", "rejected", "timeout"}
                                   and type(value) is int and 0 <= value <= 100000}
                    if safe_counts:
                        availability["livingmemory_evidence_counts"] = safe_counts
                    recall_diagnostics = safe_livingmemory_recall_diagnostics(diagnostics)
                    if recall_diagnostics:
                        availability["livingmemory_recall_diagnostics"] = recall_diagnostics
            elif name == "self_learning":
                diagnostics = result.get("diagnostics") if isinstance(result, dict) else None
                if isinstance(diagnostics, dict):
                    restore_status = diagnostics.get("relationship_restore_status")
                    if restore_status in {"pending", "running", "complete", "partial", "failed", "unavailable"}:
                        availability["self_learning_relationship_restore_status"] = restore_status
                    if result.get("partial") is True:
                        availability["self_learning_partial"] = True
                    safe_counts = {key: value for key, value in diagnostics.items()
                                   if key in {"relationship_restore_pending", "relationship_restore_restored",
                                              "relationship_restore_failed"}
                                   and type(value) is int and 0 <= value <= 100000}
                    if safe_counts:
                        availability["self_learning_diagnostics"] = safe_counts
            rows = (result or {}).get("items", [])
            if name == "self_learning":
                from .social_materials import select_social_items
                from .integrations.dynamics_learning import filter_legacy_social
                rows = filter_legacy_social(self.plugin,scope4,participants,rows)
                rows = select_social_items(rows, item.context.text, item.context.author)
            for row in rows[:8]:
                # recall_context.items is retained as a public compatibility
                # response, but managed turns consume only its typed view.
                if name == "livingmemory":
                    continue
                if not isinstance(row, dict) or not row.get("source_id"):
                    continue
                owner = row.get("scope") or {}
                if not same_scope(owner, scope):
                    continue
                context_items.append(dict(row, text=str(row.get("text", "")), provider=name))
        evidence_view = None
        checked_host_view = _validated_evidence_view(host_view, scope4, purpose, card_version)
        recalled_view = (livingmemory_result or {}).get("evidence_view") if livingmemory_result else None
        checked_recall_view = _validated_evidence_view(recalled_view, scope4, purpose, card_version)
        availability["livingmemory_projection_stage_counts"] = _bounded_projection_stage_counts(
            raw_recall_view, None, None
        )
        if host_view is not None and checked_host_view is None:
            host_failure = _unavailable_evidence_status(host_view)
            if host_failure:
                availability["livingmemory_host_view_status"] = host_failure
            else:
                availability["livingmemory_host_view_invalid"] = True
        if livingmemory_result is not None and recalled_view is not None and checked_recall_view is None:
            availability["livingmemory_evidence_status"] = _unavailable_evidence_status(recalled_view) or "invalid"
        stale_view = False
        if checked_host_view and checked_recall_view:
            if checked_host_view["correction_revision"] == checked_recall_view["correction_revision"]:
                evidence_view = _merge_evidence_views(checked_host_view, checked_recall_view)
            else:
                # Never combine claims from different correction snapshots.
                evidence_view = checked_host_view
                stale_view = True
        elif checked_host_view:
            evidence_view = checked_host_view
        elif checked_recall_view:
            evidence_view = copy.deepcopy(checked_recall_view)
            evidence_view["history_projection"] = {}
            availability["livingmemory_evidence_status"] = "partial"
        availability["livingmemory_projection_stage_counts"] = _bounded_projection_stage_counts(
            raw_recall_view, evidence_view, None
        )
        if evidence_view is not None:
            # Host history is projected by the provider wrapper. Do not append
            # a second, weakly mapped legacy summary even for quote requests;
            # exact quotes come from the fixed host-history snapshot itself.
            availability["livingmemory_evidence_view"] = True
            if availability.get("livingmemory_recall_status") in {"timeout", "unavailable", "invalid", "partial"}:
                availability["livingmemory_evidence_status"] = "partial"
            if stale_view:
                availability["livingmemory_evidence_stale"] = True
                availability["livingmemory_evidence_status"] = "partial"
            atoms = evidence_view.get("atoms", [])
            if purpose != "history_quote" and isinstance(atoms, list):
                for atom in atoms:
                    if (not isinstance(atom, dict) or not isinstance(atom.get("claim"), str)
                            or not atom["claim"].strip() or not isinstance(atom.get("atom_id"), str)
                            or not atom["atom_id"]):
                        continue
                    refs = atom.get("source_refs")
                    if not isinstance(refs, list) or not refs:
                        continue
                    context_items.append({"scope": scope4, "source_id": atom["atom_id"][:240],
                                          "provider": "livingmemory", "fact_kind": str(atom.get("kind") or "evidence"),
                                          "text": atom["claim"], "provenance_kind": "evidence_view_atom",
                                          "evidence_refs": refs[:8], "evidence_status": str(atom.get("status") or "unknown")})
        else:
            # Without a source-aware view, legacy recall prose cannot safely be
            # used as evidence in a managed reply.
            availability["livingmemory_evidence_view"] = False
            if livingmemory_result and not availability.get("livingmemory_evidence_status"):
                availability["livingmemory_evidence_status"] = "unavailable"
        prior = item.narrative_bundle.wire() if item.narrative_bundle is not None else {}
        extra_view = _validated_evidence_view(prior.get("evidence_view"), scope4, purpose, card_version)
        extra_usable = False
        if not lightweight and prior.get("preparation_phase") == "decision" and extra_view:
            if evidence_view and extra_view["correction_revision"] == evidence_view["correction_revision"]:
                merged = _merge_evidence_views(evidence_view, extra_view)
                if merged is not None:
                    evidence_view, extra_usable = merged, True
                    availability["supplement_context_status"] = "merged"
                else:
                    availability["supplement_context_status"] = "conflicting"
            elif evidence_view is None and await self.evidence_current_async({"scope": scope4, "evidence_view": extra_view}):
                evidence_view, extra_usable = copy.deepcopy(extra_view), True
                # A targeted recall does not establish a fresh host-history map.
                evidence_view["history_projection"] = {}
                availability.update(livingmemory=True, livingmemory_evidence_view=True,
                                    livingmemory_evidence_status="partial", supplement_context_status="retained")
            else:
                availability["supplement_context_status"] = "stale"
        if extra_usable:
            known = {row.get("source_id") for row in context_items}
            allowed = {atom.get("atom_id") for atom in evidence_view.get("atoms", []) if isinstance(atom, dict)}
            context_items.extend(row for row in prior.get("context_items", [])
                                 if row.get("provider") == "livingmemory" and row.get("source_id") in allowed
                                 and row.get("source_id") not in known)
        filtered, state = member_boundary.filter_material(
            self.plugin, event, {"context_items": context_items, "snapshot": snapshot}, purpose="role_input")
        if not state.available and (member_boundary.fail_closed(state) or state.service is not None):
            trace.unavailable('boundary_material', 'boundary_unavailable')
            return None
        if isinstance(filtered, dict):
            context_items = filtered.get("context_items", context_items)
            if isinstance(filtered.get("snapshot"), dict):
                snapshot = filtered["snapshot"]
        availability["livingmemory_projection_stage_counts"] = _bounded_projection_stage_counts(
            raw_recall_view, evidence_view, context_items
        )
        now = self.plugin.time_service.wall_time()
        payload = {"schema_version": 1, "scope": scope, "character": character,
                   "snapshot": snapshot, "context_items": context_items, "completed": completed,
                   "captured_at": now, "availability": availability, "speaker_id": item.context.author,
                   "effective_core_version": card_version, "purpose": purpose,
                   "host_history_snapshot": history_snapshot,
                   "evidence_view": evidence_view}
        payload["preparation_phase"] = "decision" if lightweight else "reply"
        if lightweight:
            availability["livingmemory_evidence_status"] = "deferred"
        if lightweight:
            payload['expression_context'], payload['system_receipts'] = {}, []
        else:
            payload['expression_context'], payload['system_receipts'] = await asyncio.gather(
                asyncio.to_thread(self.journal.expression_context, scope),
                asyncio.to_thread(self.journal.failure_context, scope, item.context.author, now))
        turn_id = hashlib.sha256((umo + "|" + "|".join(m.message_id for m in item.context.messages)).encode()).hexdigest()[:32]
        envelopes = []
        prior_events = {e["event_id"]: e for e in item.narrative_bundle.wire().get("events", [])} if (
            item.narrative_bundle is not None and item.narrative_bundle.wire().get("preparation_phase") == "decision") else {}
        for message in (() if getattr(event, "_extras", {}).get("_narrative_internal_v1") else item.context.messages):
            occurred = now
            for raw in item.events:
                obj = getattr(raw, "message_obj", None)
                if str(getattr(obj, "message_id", "")) == message.message_id:
                    stamp = getattr(obj, "timestamp", getattr(raw, "created_at", None))
                    if isinstance(stamp, (int, float)) and math.isfinite(stamp) and stamp > 946684800:
                        occurred = min(now, float(stamp))
            stamp = getattr(message, "timestamp", None)
            if occurred == now and isinstance(stamp, (int, float)) and math.isfinite(stamp):
                monotonic = getattr(self.plugin.time_service, "time", lambda: now)()
                occurred = max(0, now - max(0, monotonic - stamp))
            source_text = getattr(message, "source_text", None)
            raw_text = source_text if source_text is not None else message.text
            envelope = {
                "schema_version": 1,
                "event_id": "in:" + hashlib.sha256((umo + "|" + message.message_id).encode()).hexdigest(),
                "type": "message.received",
                "occurred_at": occurred,
                "received_at": now,
                "scope": scope,
                "actor_id": message.author,
                "text": raw_text[:12000],
                "source_ids": [message.message_id],
                "fact_kind": "observation",
                "data": {
                    "quoted_message_id": getattr(message, "reply_to", None),
                    "turn_id": turn_id,
                    "care_activity_id": message.message_id,
                    "text_truncated": len(raw_text) > 12000,
                    "original_length": len(raw_text),
                    "directed_to_bot": bool(
                        item.context.bot_id
                        and (
                            item.context.bot_id in message.mentioned_users
                            or getattr(message.semantics, "quoted_author_id", None) == item.context.bot_id
                        )
                    )
                    or bool(item.context.mandatory_reply and len(item.context.messages) == 1)
                    or bool(is_private_message_event(event) and message.author == item.context.author),
                    "attention_basis": "platform_mention_or_quote",
                    "public_social_candidate": (
                        public_social_candidate(item.context, message)
                        and social_boundary_allows(self.plugin, event, scope, message.author)
                    ),
                    "social_stop_revision": getattr(runtime, 'topic_stop_revisions', {}).get(message.author, 0),
                },
            }
            if envelope["event_id"] in prior_events:
                envelope = prior_events[envelope["event_id"]]
            else:
                incoming_recipients = ["livingmemory", "self_learning"]
                if self.plugin.integrations.narrative_provider("portrayal") is not None:
                    incoming_recipients.append("portrayal")
                await self._io.call('incoming_event', self.journal.enqueue, envelope, tuple(incoming_recipients), now)
            envelopes.append(envelope)
        payload["events"] = envelopes
        if not getattr(event, "_extras", {}).get("_narrative_internal_v1"):
            horizons_source = dynamics_horizons.provider(self.plugin)
            if horizons_source is not None:
                h_selected = dynamics_horizons.read(
                    horizons_source,
                    scope=umo,
                    persona=str(getattr(persona, "persona_id", "") or ""),
                    member=str(getattr(item.context, "author", "") or ""),
                    query=str(getattr(item.context, "text", "") or ""),
                    nearby_text=dynamics_horizons._build_nearby_text(item.context),
                    character_scope=scope4,
                )
                if h_selected.get("matched"):
                    if isinstance(h_selected.get("decision_slot"), dict) and h_selected["decision_slot"]:
                        payload["horizons_decision_slot"] = h_selected["decision_slot"]
                    if isinstance(h_selected.get("reply_projection"), dict) and h_selected["reply_projection"]:
                        payload["horizons_reply_projection"] = h_selected["reply_projection"]
        bundle = TurnContextBundle(turn_id, json.dumps(payload, ensure_ascii=False, allow_nan=False))
        if lightweight:
            self.bind(item.events, bundle, persist=False)
        else:
            await self.bind_async(item.events, bundle)
        self._cache_bundle(umo, bundle)
        return bundle

    async def supplement_context(self, item, query):
        """Add bounded public memory evidence without replaying incoming events.

        Args:
            item: Original turn and its frozen narrative bundle.
            query: Attributed query derived from current, visible messages.

        Returns:
            A replacement bundle and a body-free result status.
        """
        original = item.narrative_bundle
        data = original.wire()
        scope = {k: data["scope"].get(k) for k in ("platform", "umo", "persona_id", "instance_id")}
        result = await self.call(
            "livingmemory",
            "recall_context",
            scope,
            query[:2000],
            data.get("purpose", "reply"),
            data.get("effective_core_version", ""),
            timeout=7,
        )
        raw = (result or {}).get("evidence_view")
        if raw is None or len(json.dumps(raw, ensure_ascii=False)) > 100000:
            return original, "unavailable"
        view = _validated_evidence_view(
            raw, scope, data.get("purpose", "reply"), data.get("effective_core_version", "")
        )
        if view is None:
            return original, "invalid"
        # Canonicalize atom ownership, source references and revocations even
        # when the original request did not have an evidence view yet.
        view = _merge_evidence_views(view, view)
        prior = data.get("evidence_view")
        if prior and (
            prior.get("correction_revision") != view["correction_revision"]
            or prior.get("revocations") != view["revocations"]
        ):
            return original, "stale"
        old_ids = {a.get("atom_id") for a in (prior or {}).get("atoms", []) if isinstance(a, dict)}
        slotted = {
            a.get("atom_id")
            for rows in view["slots"].values()
            if isinstance(rows, list)
            for a in rows
            if isinstance(a, dict)
        }
        selected, entries, budget = [], [], 3000
        for atom in view["atoms"]:
            if (
                not isinstance(atom, dict)
                or not atom.get("atom_id")
                or atom["atom_id"] in old_ids
                or atom["atom_id"] not in slotted
                or not isinstance(atom.get("claim"), str)
                or not atom.get("source_refs")
            ):
                continue
            valid_from = atom.get("valid_from")
            if type(valid_from) in (int, float) and valid_from > data.get("captured_at", 0):
                continue
            size = len(json.dumps(atom, ensure_ascii=False))
            if size > budget:
                continue
            budget -= size
            selected.append(atom)
            entries.append(
                {
                    "scope": scope,
                    "source_id": atom["atom_id"],
                    "provider": "livingmemory",
                    "fact_kind": str(atom.get("kind") or "evidence"),
                    "text": atom["claim"],
                    "provenance_kind": "evidence_view_atom",
                    "evidence_refs": atom["source_refs"][:8],
                    "evidence_status": str(atom.get("status") or "unknown"),
                }
            )
            if len(selected) >= 6:
                break
        filtered, boundary = member_boundary.filter_material(
            self.plugin, item.events[-1], {"context_items": entries}, purpose="role_input"
        )
        if not boundary.available and (member_boundary.fail_closed(boundary) or boundary.service is not None):
            raise member_boundary.BoundaryCancelled("context_completion_boundary_unavailable")
        entries = filtered.get("context_items", []) if isinstance(filtered, dict) else []
        allowed = {(r.get("source_id"), r.get("text")) for r in entries if isinstance(r, dict)}
        selected = [a for a in selected if (a["atom_id"], a["claim"]) in allowed]
        if not selected:
            return original, "empty"
        view["atoms"] = selected
        selected_ids = {a["atom_id"] for a in selected}
        by_id = {a["atom_id"]: a for a in selected}
        view["slots"] = {
            k: [by_id[a["atom_id"]] for a in rows if isinstance(a, dict) and a.get("atom_id") in selected_ids]
            for k, rows in view["slots"].items()
            if isinstance(rows, list)
        }
        view["history_projection"] = {}
        merged = _merge_evidence_views(prior, view) if prior else view
        if merged is None:
            return original, "stale"
        merged_ids = {a.get("atom_id") for a in merged.get("atoms", []) if isinstance(a, dict)}
        entries = [r for r in entries if r.get("source_id") in merged_ids - old_ids]
        if not entries:
            return original, "empty"
        # The requested evidence should be considered before older optional
        # claims in its own slot; required boundary/current slots keep priority.
        for rows in merged.get("slots", {}).values():
            if isinstance(rows, list):
                rows.sort(key=lambda row: 0 if row.get("atom_id") in selected_ids else 1)
        data["evidence_view"] = merged
        data["context_items"] = [*data.get("context_items", []), *entries]
        data["availability"] = {
            **data.get("availability", {}),
            "livingmemory": True,
            "livingmemory_evidence_view": True,
            "livingmemory_evidence_status": "partial",
        }
        if not await self.evidence_current_async(data):
            return original, "stale"
        return TurnContextBundle(original.turn_id, json.dumps(data, ensure_ascii=False, allow_nan=False)), "added"

    def _persist_bundle(self, bundle):
        data = bundle.wire()
        persisted = json.loads(bundle.payload)
        history = persisted.get("host_history_snapshot")
        if isinstance(history, dict):
            persisted["host_history_snapshot"] = {
                "conversation_id": history.get("conversation_id", ""),
                "snapshot_fingerprint": history.get("snapshot_fingerprint", ""),
                "history_epoch": history.get("history_epoch", ""),
                "source_rows_count": len(history.get("rows", [])) if isinstance(history.get("rows"), list) else 0,
                "referenceable_rows_count": len(history.get("source_refs", [])) if isinstance(history.get("source_refs"), list) else 0,
            }
        evidence = persisted.get("evidence_view")
        if isinstance(evidence, dict):
            persisted["evidence_view"] = {key: evidence[key] for key in
                ("schema_version", "available", "scope_key", "correction_revision",
                 "purpose", "card_version", "diagnostics") if key in evidence}
        persisted_payload = json.dumps(persisted, ensure_ascii=False, allow_nan=False)
        self.journal.save_bundle(bundle.bundle_id, bundle.turn_id, data["scope"]["umo"],
                                 persisted_payload, data["captured_at"])

    async def bind_async(self, events, bundle):
        """Finish durable evidence off-loop before exposing the managed turn."""
        await self._io.call('save_bundle', self._persist_bundle, bundle)
        self.bind(events, bundle, persist=False)

    def bind(self, events, bundle, *, persist=True):
        if persist:
            self._persist_bundle(bundle)
        data = bundle.wire()
        for event in events:
            extras = getattr(event, "_extras", None)
            if extras is None:
                event._extras = {}
            event._extras[MANAGED_KEY] = dict(data, turn_id=bundle.turn_id, bundle_id=bundle.bundle_id)
            event._extras["_narrative_segment_index"] = 0

    def route_life_events(self, bundle, impact, decision_evidence=None, event=None):
        # Preserve original observations; routing is a separate attributed event.
        relevant = impact in {"activity", "commitment", "relationship"}
        payload = bundle.wire()
        feedback_speaker = payload.get("feedback_speaker_id", "")
        now = self.plugin.time_service.wall_time()
        umo = bundle.scope['umo']
        for source in payload.get("events", []):
            care_observation = self.outreach_allowed(bundle.scope['umo']) and source.get('data', {}).get('directed_to_bot')
            behavior_observation = source.get("data", {}).get("directed_to_bot") is True and any(
                cue in source.get("text", "")
                for cue in (
                    "不是",
                    "误会",
                    "理解错",
                    "说的是",
                    "意思是",
                    "以后",
                    "下次",
                    "别",
                    "不要",
                    "简短",
                    "只想",
                    "只要",
                    "撤销",
                    "收回",
                )
            )
            social_observation = (
                self.outreach_allowed(umo)
                and same_scope(source.get('scope', {}), bundle.scope)
                and source.get('actor_id') == payload.get('speaker_id')
                and source.get('data', {}).get('public_social_candidate') is True
                and decision_allows_social_observation(decision_evidence)
                and source.get('data', {}).get('social_stop_revision', 0) == decision_evidence.get('social_stop_revision', 0)
                and social_boundary_allows(self.plugin, event, bundle.scope, source.get('actor_id'))
                and 0 <= now - source.get('occurred_at', 0) <= 900
                and now - self._social_opportunity_at.get(umo, float('-inf')) >= 600
            )
            if not relevant and not care_observation and not behavior_observation and not social_observation:
                continue
            routed = dict(source, event_id="life:" + source["event_id"],
                          source_ids=[source["event_id"]],
                          data=dict(source.get("data", {}), event_impact=impact))
            if feedback_speaker and source.get("actor_id") == feedback_speaker:
                routed["data"].update(directed_to_bot=True, attention_basis="confirmed_correction_recipient")
            if social_observation:
                routed['data']['social_opportunity'] = True
            self.journal.enqueue(routed, ("dynamic_life_state",), now)
            if social_observation:
                self._note_social_opportunity(umo, now)

    def _note_social_opportunity(self, umo, now):
        self._social_opportunity_at[umo] = now
        if len(self._social_opportunity_at) > 128:
            oldest = min(self._social_opportunity_at, key=self._social_opportunity_at.get)
            del self._social_opportunity_at[oldest]

    async def route_life_events_async(self, bundle, impact, decision_evidence=None, event=None):
        await self._io.call('route_life_events', self.route_life_events, bundle, impact,
                            copy.deepcopy(decision_evidence), event)

    async def advance_important(self, item, bundle, impact):
        """Wait at most eight seconds for a validated important-event revision."""
        if impact not in {"activity", "commitment", "relationship"}:
            return bundle
        pending = [dict(e, event_id="life:" + e["event_id"], source_ids=[e["event_id"]],
                        data=dict(e.get("data", {}), event_impact=impact))
                   for e in bundle.wire().get("events", [])]
        feedback_speaker = bundle.wire().get("feedback_speaker_id", "")
        for routed in pending:
            if feedback_speaker and routed.get("actor_id") == feedback_speaker:
                routed["data"].update(directed_to_bot=True, attention_basis="confirmed_correction_recipient")
        result = await self.call("dynamic_life_state", "advance", pending, bundle.snapshot.get("revision"), timeout=8)
        snapshot = (result or {}).get("snapshot")
        if not snapshot or not snapshot.get("managed"):
            return bundle
        payload = bundle.wire()
        payload["snapshot"] = snapshot
        revised = TurnContextBundle(bundle.turn_id, json.dumps(payload, ensure_ascii=False, allow_nan=False))
        await self.bind_async(item.events, revised)
        self._cache_bundle(bundle.scope["umo"], revised)
        return revised

    async def current(self, bundle, event=None):
        data = bundle.wire()
        if data.get("evidence_view") is not None and not await self.evidence_current_async(data):
            return False
        if not self.enabled(bundle.scope["umo"]):
            return False
        character = await self.call("worldbook", "get_character_context", bundle.scope, event)
        if not (character and not character.get("disabled") and character.get("available", True) and
                character.get("fingerprint") == bundle.wire()["character"].get("fingerprint")):
            return False
        current = await self.call("dynamic_life_state", "get_snapshot", bundle.scope)
        return bool(current and current.get("managed") and not current.get("paused"))

    def begin_send(self, event, text):
        data = getattr(event, "_extras", {}).get(MANAGED_KEY)
        if not data:
            return None
        segment = event._extras.get("_narrative_segment_index", 0)
        event._extras["_narrative_segment_index"] = segment + 1
        return self.journal.begin(data["scope"], data["turn_id"], segment,
                                  self.plugin.time_service.wall_time(), data.get("intent_id"))

    async def begin_send_async(self, event, text):
        data = getattr(event, '_extras', {}).get(MANAGED_KEY)
        if not data:
            return None
        segment = event._extras.get('_narrative_segment_index', 0)
        event._extras['_narrative_segment_index'] = segment + 1
        task = self._io.submit('begin_send', self.journal.begin, dict(data['scope']), data['turn_id'],
                               segment, self.plugin.time_service.wall_time(), data.get('intent_id'))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # This operation is before the platform side effect. A late committed
            # attempt is cancelled, never sent, even if the caller has gone away.
            def settle(future):
                if not future.cancelled() and future.exception() is None:
                    self._io.submit('cancel_unsent', self.journal.finish, future.result(),
                                    'cancelled', self.plugin.time_service.wall_time())
            task.add_done_callback(settle)
            raise

    async def record_history_diagnostic_async(self, event, projection):
        from types import SimpleNamespace
        extras = getattr(event, '_extras', {})
        source = extras.get(MANAGED_KEY, {})
        frozen_managed = {key: copy.deepcopy(source.get(key)) for key in ('scope', 'turn_id', 'bundle_id')}
        frozen_managed['character'] = {'fingerprint': source.get('character', {}).get('fingerprint')}
        frozen = SimpleNamespace(_extras={MANAGED_KEY: frozen_managed,
            '_narrative_plan_diagnostic_v1': copy.deepcopy(extras.get('_narrative_plan_diagnostic_v1', {}))})
        self._io.submit('history_diagnostic', self.record_history_diagnostic, frozen, copy.deepcopy(projection))

    def record_history_diagnostic(self, event, projection):
        """Persist one idempotent provider-history receipt without prompt content."""
        extras = getattr(event, "_extras", {})
        managed = extras.get(MANAGED_KEY, {})
        if not managed.get("bundle_id") or not managed.get("turn_id"):
            return False
        plan = extras.get("_narrative_plan_diagnostic_v1", {})
        self.journal.record_turn_diagnostic(
            scope=managed.get("scope", {}), turn_id=managed["turn_id"], bundle_id=managed["bundle_id"],
            character_fingerprint=managed.get("character", {}).get("fingerprint"),
            projection=projection, plan=plan,
            material_count=projection.get("material_count", 0),
            material_char_count=projection.get("material_char_count", 0),
            now=self.plugin.time_service.wall_time())
        return True

    def attach_reply_evidence(self, item, decision, answers):
        """Prepare evidence; it becomes actionable only in a successful receipt."""
        evidence = []
        target = next((m for m in item.context.messages if decision.target_message_ids and m.message_id == decision.target_message_ids[0]), None)
        managed = getattr(item.events[-1], '_extras', {}).get(MANAGED_KEY, {})
        recipient = answers.get('recipient', {}) if isinstance(answers, dict) else {}
        if target and decision.action != 'ignore' and not managed.get('internal'):
            sem = target.semantics
            mentions = set(target.mentioned_users) | set(getattr(sem, 'mentioned_user_ids', ()))
            explicit_other = bool(mentions - {item.context.bot_id})
            ambiguous = bool(getattr(sem, 'addressee_ambiguous', False) or getattr(sem, 'routing_ambiguous', False))
            source = next((e for e in managed.get('events', []) if target.message_id in e.get('source_ids', [])
                           and e.get('actor_id') == target.author), None)
            basis, confidence, parent = '', 0.0, None
            if source and source.get('data', {}).get('directed_to_bot'):
                basis, confidence = 'platform_addressed', 1.0
            elif source and not explicit_other and not ambiguous:
                if recipient.get('choice') == 'bot' and recipient.get('confidence', 0) >= .72:
                    basis, confidence = 'jev_addressed', recipient['confidence']
                elif getattr(sem, 'bot_is_addressee', False) and getattr(sem, 'bot_addressee_confidence', 0) >= .72:
                    parent = self.journal.delivered_reply(managed['scope'], getattr(sem, 'parent_message_id', ''))
                    if parent and parent.get('data', {}).get('recipient_user_id') == target.author:
                        age = source['occurred_at'] - parent['occurred_at']
                        if 0 <= age <= 600:
                            basis, confidence = 'confirmed_continuation', sem.bot_addressee_confidence
            if basis:
                evidence = [{'source_event_id':source['event_id'], 'actor_id':target.author,
                             'basis':basis, 'confidence':confidence,
                             'parent_receipt_id':parent['event_id'] if parent else None}]
        for event in item.events:
            event._extras['_narrative_reply_evidence_v1'] = evidence
            event._extras['_narrative_recipient_v1'] = target.author if target else ''

    async def attach_reply_evidence_async(self, item, decision, answers):
        await asyncio.to_thread(self.attach_reply_evidence, item, decision, copy.deepcopy(answers))

    def _send_receipt(self, event, attempt, text, result=None):
        if not attempt:
            return
        data = event._extras[MANAGED_KEY]
        status = "success" if result is not None and result.success else (
            "uncertain" if result is None or getattr(result, "delivery_uncertain", False) else "failure")
        now = self.plugin.time_service.wall_time()
        mid = getattr(result, "message_id", None)
        delivered_text = (
            as_send_result(result).delivered_text(text, media_placeholder=False)
            if result is not None and result.success else ""
        )
        receipt = {"schema_version": 1, "event_id": f"receipt:{attempt}", "type": f"delivery.{status}",
                   "occurred_at": now, "received_at": now, "scope": data["scope"],
                   "actor_id": "bot", "text": delivered_text,
                   "source_ids": list(getattr(result, "message_ids", ()) or (mid or attempt,)), "fact_kind": "receipt",
                   "data": {"attempt_id": attempt, "turn_id": data["turn_id"], "message_id": mid,
                            "segment_index": event._extras.get("_narrative_segment_index", 1) - 1,
                            "intent_id": data.get("intent_id"), "bundle_id": data.get("bundle_id"),
                            "revision": data.get("snapshot", {}).get("revision"), "scope_revision": data.get("snapshot", {}).get("scope_revision"),
                            "character_fingerprint": data.get("character", {}).get("fingerprint")}}
        receipt['data']['recipient_user_id'] = event._extras.get('_narrative_recipient_v1', '')
        failure = event._extras.get('_narrative_generation_failure_v1')
        if failure:
            receipt['data']['generation_failure'] = str(failure)[:80]
            receipt['data']['message_origin'] = 'system_fallback'
        receipt['data']['interaction_evidence'] = (event._extras.get('_narrative_reply_evidence_v1', [])
            if status == 'success' and not data.get('intent_id') else [])
        return {'version': 1, 'attempt': attempt, 'status': status, 'now': now,
                'message_id': mid, 'event': copy.deepcopy(receipt),
                'recipients': ['dynamic_life_state'] if data.get('private_outreach') else list(RECIPIENTS)}

    def _persist_receipt(self, packet):
        try:
            self.journal.finish(packet['attempt'], packet['status'], packet['now'],
                                packet['message_id'], packet['event'], tuple(packet['recipients']))
        except Exception:
            self._receipt_outbox.save(packet)
            return False
        self._receipt_outbox.remove(packet['attempt'])
        return True

    def finish_send(self, event, attempt, text, result=None):
        """Synchronous compatibility API; production sends use the async handoff."""
        packet = self._send_receipt(event, attempt, text, result)
        if packet:
            self._persist_receipt(packet)
            self.start()

    async def finish_send_async(self, event, attempt, text, result=None):
        packet = self._send_receipt(event, attempt, text, result)
        if not packet:
            return
        self._unspooled_receipts[attempt] = packet
        task = self._io.submit('finish_send', self._persist_receipt, packet)
        self._receipt_tasks.add(task)
        def settled(future):
            self._receipt_tasks.discard(future)
            if not future.cancelled() and future.exception() is None:
                self._unspooled_receipts.pop(attempt, None)
                if not future.result():
                    logger.warning('Send receipt deferred code=CD_RECEIPT_QUEUED')
                self.start()
        task.add_done_callback(settled)
        # No await after a confirmed send: its caller receives the platform
        # result and records delivery before another cancellation can intervene.

    async def flush_receipts(self):
        packets = await self._io.call('read_receipt_outbox', self._receipt_outbox.pending)
        by_id = {packet['attempt']: packet for packet in packets}
        by_id.update(dict(self._unspooled_receipts))
        for attempt, packet in list(by_id.items())[:32]:
            stored = await self._io.call('retry_receipt', self._persist_receipt, packet)
            if stored:
                self._unspooled_receipts.pop(attempt, None)

    async def close_io(self):
        await self._io.drain()
        try:
            await self.flush_receipts()
        except Exception as exc:
            logger.warning('Receipt recovery pending at shutdown code=CD_RECEIPT_PENDING type=%s', type(exc).__name__)
        await self._io.drain()
        self.journal.close()

    def _tool_receipt_event(self, event, receipt):
        """Archive that a tool returned, without asserting its external effects."""
        data = getattr(event, "_extras", {}).get(MANAGED_KEY)
        if not data:
            return
        now = self.plugin.time_service.wall_time()
        event_id = "tool:" + uuid.uuid4().hex
        envelope = {"schema_version": 1, "event_id": event_id, "type": "tool.receipt",
                    "occurred_at": now, "received_at": now, "scope": data["scope"],
                    "actor_id": "bot", "text": "Tool call " + str(receipt.get("tool", "")) + " " + str(receipt.get("status", "")),
                    "source_ids": [event_id], "fact_kind": "receipt",
                    "data": dict(receipt, turn_id=data["turn_id"], effect_confirmed=False)}
        if receipt.get("effect_confirmed") is True and receipt.get("effect_kind") == "memory.saved" and receipt.get("resource_id"):
            envelope["type"] = "tool.success"
            envelope["data"]["effect_confirmed"] = True
        return envelope, now

    def tool_receipt(self, event, receipt):
        prepared = self._tool_receipt_event(event, receipt)
        if prepared:
            envelope, now = prepared
            self.journal.enqueue(envelope, RECIPIENTS, now)
            self.start()

    async def tool_receipt_async(self, event, receipt):
        prepared = self._tool_receipt_event(event, receipt)
        if prepared:
            envelope, now = prepared
            task = self._io.submit('tool_receipt', self.journal.enqueue, copy.deepcopy(envelope), RECIPIENTS, now)
            task.add_done_callback(lambda future: self.start() if not future.cancelled() and future.exception() is None else None)

    def reminder_effect(self, umo, reminder_id, action, text="", message_id=None):
        """Called only after the owned scheduler commits or confirms delivery."""
        bundle = self._bundles.get(umo)
        scope = bundle.scope if bundle else None
        if action != "created":
            scope = self.journal.reminder_scope(umo, str(reminder_id))
        if scope is None:
            return
        now = self.plugin.time_service.wall_time()
        event_id = "reminder:" + hashlib.sha256((umo + str(reminder_id) + action).encode()).hexdigest()
        event = {"schema_version": 1, "event_id": event_id, "type": "execution.success",
                 "scope": scope, "occurred_at": now, "received_at": now,
                 "actor_id": "bot", "text": str(text)[:12000], "fact_kind": "receipt", "source_ids": [event_id],
                 "data": {"effect_confirmed": True, "effect_kind": "reminder." + action,
                          "reminder_id": str(reminder_id), "message_id": message_id}}
        self.journal.enqueue(event, RECIPIENTS, now)
        if action == "sent":
            delivered = dict(event, event_id=event_id + ":delivery", type="delivery.success")
            self.journal.enqueue(delivered, ("livingmemory", "self_learning"), now)
        self.start()

    def queue_reminder_effect(self, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.reminder_effect(*args, **kwargs)
        return self._io.submit('reminder_effect', self.reminder_effect, *args, **kwargs)

    def start(self):
        if self.plugin._shutting_down:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = getattr(getattr(self, '_io', None), 'loop', None)
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self.start)
            return
        if self.task is None or self.task.done():
            self.task = self.plugin._create_background_task(self.run())

    async def run(self):
        await self._io.call('ensure_indexes', self.journal.ensure_indexes)
        horizons_source=dynamics_horizons.provider(self.plugin)
        horizons_active=bool(horizons_source and horizons_source.horizons_capabilities().get('proactive_enabled'))
        if (not getattr(self.plugin._runtime_config, "narrative_enabled", False)
                and not horizons_active and not await asyncio.to_thread(self.journal.pending, 1)):
            return
        while not self.plugin._shutting_down:
            try:
                await self.flush_receipts()
                await self._io.call(
                    'maintain',
                    self.journal.maintain,
                    self.plugin.time_service.wall_time(),
                    getattr(self.plugin._runtime_config, "narrative_context_retention_days", 7),
                    archive_retention_days=getattr(
                        self.plugin._runtime_config, "narrative_archive_retention_days", 30
                    ),
                    dead_letter_ttl_days=getattr(
                        self.plugin._runtime_config, "narrative_dead_letter_ttl_days", 7
                    ),
                )
                if self.plugin.integrations.narrative_provider("portrayal") is not None:
                    await self._io.call('backfill_portraits', self.journal.backfill_portraits)
                await self.dispatch()
                await self.dispatch_intents()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Narrative dispatch deferred type=%s", type(exc).__name__)
            await asyncio.sleep(30)

    async def dispatch(self):
        async with self._dispatch_lock:
            async def deliver(name):
                deadline = time.monotonic() + 10
                events = await asyncio.to_thread(self.journal.pending_for, name, 100)
                for event in events:
                    if time.monotonic() >= deadline or self.plugin._shutting_down:
                        break
                    method = "observe_events" if name == "self_learning" else "ingest_events"
                    result = await self.call(name, method, [event])
                    if result is not None and not (isinstance(result, dict) and result.get("available") is False):
                        await self._io.call('event_ack', self.journal.ack, event["event_id"], name)
                    else:
                        # Do not overtake an unresolved predecessor for this
                        # recipient; other recipients continue independently.
                        break
            names = await asyncio.to_thread(self.journal.pending_recipients)
            await asyncio.gather(*(deliver(name) for name in names))

    async def submit_intent(self, intent):
        """Accept a scoped motive, never a prewritten platform message."""
        scope = intent.get("scope") or {}
        if not intent.get("intent_id") or not self.intent_outreach_allowed(intent) or intent.get("target_umo") != scope.get("umo"):
            return {"schema_version": 1, "accepted": False}
        if intent.get("text") or intent.get("message"):
            return {"schema_version": 1, "accepted": False}
        accepted = await self._io.call('add_intent', self.journal.add_intent, intent)
        self.start()
        return {"schema_version": 1, "accepted": accepted}

    async def note_member_activity(self, event, parsed):
        umo = getattr(event, 'unified_msg_origin', '')
        if not self.outreach_allowed(umo) or not parsed.sender_id or parsed.sender_id == parsed.self_id:
            return
        recipient = private_message_recipient(umo)
        if recipient and (recipient != parsed.sender_id or not is_private_message_event(event)):
            return
        now = self.plugin.time_service.wall_time()
        stamp = getattr(getattr(event, 'message_obj', None), 'timestamp', None)
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 946684800:
            stamp = now
        await self.call('dynamic_life_state', 'observe_member_activity', umo, parsed.sender_id,
                        parsed.message_id, min(now, stamp), timeout=2)

    async def observe_private_turn(self, runtime, item, persona):
        """Record direct inputs for the life planner using their exact private scope."""
        p, turn = self.plugin, item.context
        umo = runtime.umo
        uid = private_message_recipient(umo)
        event = item.events[-1]
        if (not item.private_direct or item.shadow or p._shutting_down or not self.outreach_allowed(umo)
                or not uid or uid != turn.author or uid == turn.bot_id
                or event.unified_msg_origin != umo or not is_private_message_event(event)):
            return None
        observed_at = p.time_service.wall_time()
        platform = umo.split(":", 2)[0]
        proposed = {"platform": platform, "umo": umo,
                    "persona_id": persona.persona_id, "instance_id": persona.persona_id}
        character = await self.call("worldbook", "get_character_context", proposed, event)
        if not isinstance(character, dict) or character.get("disabled") or not character.get("available", True):
            return None
        scope = character.get("scope") or {}
        if (not isinstance(scope, dict) or scope.get("platform") != platform or scope.get("umo") != umo
                or not scope.get("persona_id") or not scope.get("instance_id")
                or p._shutting_down or not self.outreach_allowed(umo)
                or not social_boundary_allows(p, event, scope, uid)):
            return None

        records = []
        source_at = {
            str(getattr(raw.message_obj, "message_id", "")): getattr(raw.message_obj, "timestamp", None)
            for raw in item.events if getattr(raw, "message_obj", None) is not None
        }
        now = p.time_service.wall_time()
        mono = p.time_service.time()
        opportunity = (runtime.epoch == turn.epoch and runtime.user_revisions.get(uid, 0) == turn.revision
                       and now - self._social_opportunity_at.get(umo, float("-inf")) >= 600)
        for message in turn.messages:
            if message.author != uid or not message.message_id:
                continue
            text = message.source_text if message.source_text is not None else message.text
            stamp = source_at.get(message.message_id)
            if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 946684800:
                stamp = (max(0, now - max(0, mono - message.timestamp))
                         if isinstance(message.timestamp, (int, float)) and math.isfinite(message.timestamp)
                         else observed_at)
            stamp = min(observed_at, stamp)
            data = {"directed_to_bot": True, "attention_basis": "private_session", "care_activity_id": message.message_id}
            if (opportunity and message is turn.messages[-1] and 0 <= now - stamp <= 900
                    and private_social_candidate(turn, message)):
                data["private_social_opportunity"] = True
                self._note_social_opportunity(umo, now)
            records.append({
                "schema_version": 1, "type": "message.received", "scope": scope,
                "event_id": "in:" + hashlib.sha256((umo + "|" + message.message_id).encode()).hexdigest(),
                "occurred_at": stamp, "received_at": now, "actor_id": uid, "text": text[:12000],
                "source_ids": [message.message_id], "fact_kind": "observation", "data": data,
            })
        if not records:
            return None
        for record in records:
            await self._io.call("private_incoming", self.journal.enqueue, record, ("dynamic_life_state",), now)
        self.start()
        return scope

    async def observe_private_delivery(self, pending_scope, delivered, message_id, observed_at):
        """Publish evidence only for a confirmed platform delivery."""
        if not delivered.strip():
            return
        scope = await pending_scope
        if scope is None or self.plugin._shutting_down or not self.outreach_allowed(scope["umo"]):
            return
        umo = scope["umo"]
        uid = private_message_recipient(umo)
        now = self.plugin.time_service.wall_time()
        receipt_id = "private-reply:" + hashlib.sha256((umo + "|" + message_id).encode()).hexdigest()
        receipt = {
            "schema_version": 1, "type": "delivery.success", "scope": scope, "event_id": receipt_id,
            "occurred_at": observed_at, "received_at": now, "actor_id": "bot", "text": delivered[:12000],
            "source_ids": [message_id], "fact_kind": "receipt",
            "data": {"recipient_user_id": uid, "message_id": message_id},
        }
        await self._io.call("private_delivery", self.journal.enqueue, receipt, ("dynamic_life_state",), now)
        self.start()

    @staticmethod
    def _intent_provider(intent_id, intent=None):
        if str(intent_id or "").startswith("horizon:") or (
            isinstance(intent, dict)
            and intent.get("provider") in {"dynamics_horizons", "astrbot_plugin_dynamics_horizons"}
        ):
            return "dynamics_horizons"
        return "dynamic_life_state"

    def _normalize_due_intent(self, raw, provider, now):
        if not isinstance(raw, dict):
            return None
        if provider != "dynamics_horizons":
            return raw
        intent = dict(raw)
        iid = str(intent.get("intent_id") or "").strip()
        if not iid:
            return None
        is_explicit_horizon = (
            iid.startswith("horizon:")
            or intent.get("provider") in {"dynamics_horizons", "astrbot_plugin_dynamics_horizons"}
            or "horizon_kind" in intent
        )
        if not is_explicit_horizon:
            return None
        if not iid.startswith("horizon:"):
            iid = "horizon:" + iid
        intent["intent_id"] = iid
        intent["provider"] = "dynamics_horizons"
        raw_scope = intent.get("scope")
        if isinstance(raw_scope, dict):
            sc = dict(raw_scope)
            umo_val = str(sc.get("umo") or intent.get("target_umo") or intent.get("umo") or "").strip()
            sc.setdefault("umo", umo_val)
            sc.setdefault("platform", umo_val.split(":", 1)[0] if ":" in umo_val else "")
            sc.setdefault("persona_id", str(intent.get("persona_id") or intent.get("persona") or "default"))
            sc.setdefault(
                "instance_id",
                str(intent.get("instance_id") or getattr(self.plugin, "instance_id", "") or sc["persona_id"] or "default"),
            )
            intent["scope"] = sc
        else:
            umo_val = str(raw_scope or intent.get("target_umo") or intent.get("umo") or "").strip()
            persona_val = str(intent.get("persona_id") or intent.get("persona") or "default")
            intent["scope"] = {
                "platform": umo_val.split(":", 1)[0] if ":" in umo_val else "",
                "umo": umo_val,
                "persona_id": persona_val,
                "instance_id": str(intent.get("instance_id") or getattr(self.plugin, "instance_id", "") or persona_val or "default"),
            }
        intent.setdefault("target_umo", intent["scope"].get("umo", ""))
        intent.setdefault("not_before", now)
        intent.setdefault("expires_at", now + 86400)
        intent.setdefault("status", "pending")
        return intent

    @staticmethod
    def _unwrap_authority_intent(authority):
        if not isinstance(authority, dict):
            return None
        if isinstance(authority.get("item"), dict):
            return authority["item"]
        if "intent_id" in authority:
            return authority
        return None

    @staticmethod
    def _claim_succeeded(claimed):
        if not isinstance(claimed, dict):
            return False
        if "changed" in claimed:
            return bool(claimed["changed"])
        return bool(claimed.get("ok") is True or claimed.get("status") == "claimed")

    async def dispatch_intents(self):
        p = self.plugin
        if p.shadow_mode:
            return
        async with self._intent_lock:
            now = p.time_service.wall_time()
            due = await self.call("dynamic_life_state", "list_due_intents", now) if getattr(p._runtime_config,'narrative_outreach_enabled',False) else []
            life_rows = due if isinstance(due, list) else (due or {}).get("items", [])
            due_horizons = await self.call("dynamics_horizons", "list_due_intents", now)
            raw_horizon_rows = due_horizons if isinstance(due_horizons, list) else (due_horizons or {}).get("items", [])
            horizon_rows = [
                norm
                for raw in (raw_horizon_rows if isinstance(raw_horizon_rows, list) else ())
                if (norm := self._normalize_due_intent(raw, "dynamics_horizons", now)) is not None
            ]
            rows = [*(life_rows if isinstance(life_rows, list) else ()), *horizon_rows]
            for intent in rows:
                await self.submit_intent(intent)
            local = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
            if local.hour < 8:
                return
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            by_id = {i["intent_id"]: i for i in rows if isinstance(i, dict) and i.get("intent_id")}
            for intent, status in await asyncio.to_thread(self.journal.intents, active_only=True):
                iid, scope = intent["intent_id"], intent.get("scope", {})
                if iid in self._synced_failed_intents and status == "failed":
                    continue
                auth_provider = self._intent_provider(iid, intent)
                authority = await self.call(auth_provider, "get_intent", iid)
                current_intent = self._unwrap_authority_intent(authority)
                if current_intent is None:
                    continue
                if current_intent.get("status") in {"sent", "failed", "uncertain", "cancelled", "expired"}:
                    if current_intent["status"] != status:
                        await self._io.call('intent_status', self.journal.intent_status, iid, current_intent["status"])
                    if current_intent["status"] == "failed":
                        if len(self._synced_failed_intents) >= self._MAX_SYNCED_FAILED_INTENTS:
                            self._synced_failed_intents.clear()
                        self._synced_failed_intents.add(iid)
                    continue
                if status == 'failed':
                    updated = await self.call(auth_provider, 'update_intent', iid, 'failed')
                    if updated is not None and not (isinstance(updated, dict) and updated.get("available") is False):
                        if len(self._synced_failed_intents) >= self._MAX_SYNCED_FAILED_INTENTS:
                            self._synced_failed_intents.clear()
                        self._synced_failed_intents.add(iid)
                    continue
                if status != "pending":
                    continue
                if current_intent.get("paused") or current_intent.get("outreach_paused"):
                    continue
                if current_intent.get("status") != "pending" or intent.get("expires_at", 0) <= now or not self.intent_outreach_allowed(intent):
                    await self._io.call('intent_status', self.journal.intent_status, iid, "cancelled")
                    continue
                if iid not in by_id:
                    continue
                if intent.get("not_before", now) > now:
                    continue
                if not self._takeover_enabled(scope["umo"]):
                    continue
                count, last = await asyncio.to_thread(self.journal.quota, scope["umo"], day_start)
                if count >= 2 or last is not None and now - last < 7200:
                    continue
                key = p._resolve_session_key(scope["umo"]) or scope["umo"]
                if p.arbiter.is_in_deep_cooling(key, current_time=p.time_service.time()):
                    continue
                if not await self._io.call('intent_retry_ready', self.journal.intent_retry_ready, iid, now):
                    continue
                try:
                    await self.send_intent(intent)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    retry = await self._io.call('intent_failure', self.journal.record_intent_failure,
                                                iid, p.time_service.wall_time(), type(exc).__name__)
                    if retry and retry['exhausted']:
                        await self.call(auth_provider, 'update_intent', iid, 'failed')
                    logger.warning('Narrative intent failed type=%s attempt=%s', type(exc).__name__,
                                   retry['attempts'] if retry else 'already_attempted')
                    # One provider failure must not starve other members/scopes.
                    continue

    async def send_intent(self, intent):
        """Rejudge an intention with a bot-owned event and tools disabled.

        Args:
            intent: A still-pending motive from the life or horizons authority.
        """
        from .turn_decision import MessageSnapshot, TurnContext, reply_prompt
        from .persona_engine import ModelTurn
        from .persona_engine import delivery_fragments
        from .agent_bridge import PersonaSnapshot

        p, scope = self.plugin, intent["scope"]
        private_recipient = private_message_recipient(scope["umo"])
        if private_recipient and scope.get("platform") != scope["umo"].split(":", 2)[0]:
            return
        auth_provider = self._intent_provider(intent.get("intent_id"), intent)
        is_horizon = auth_provider == "dynamics_horizons"
        if not self.intent_outreach_allowed(intent):
            return
        care_target = ''
        if is_horizon:
            authority = await self.call('dynamics_horizons', 'get_intent', intent['intent_id'])
            live_raw = self._unwrap_authority_intent(authority)
            if live_raw is None:
                return
            live = self._normalize_due_intent(live_raw, 'dynamics_horizons', p.time_service.wall_time()) or {}
            if (
                live.get('status', 'pending') != 'pending'
                or live.get('paused')
                or live.get('outreach_paused')
                or live.get('scope', {}).get('umo') != scope.get('umo')
                or (private_recipient and not same_scope(live.get('scope', {}), scope))
            ):
                return
            current_character = await self.call('worldbook','get_character_context',scope)
            actual_scope = (current_character or {}).get('scope') or {}
            if not (current_character or {}).get('available') or any(actual_scope.get(k)!=scope.get(k) for k in ('persona_id','instance_id')):
                return
            if private_recipient and not same_scope(actual_scope, scope):
                return
            current_life = await self.call('dynamic_life_state','get_snapshot',scope)
            if not isinstance(current_life,dict) or current_life.get('paused') or current_life.get('availability')!='available':
                return
            intent = {**intent, **live, 'scope': dict(scope, **{k: v for k, v in live.get('scope', {}).items() if v})}
            raw_target = str(live.get('recipient_user_id') or live.get('member_id') or intent.get('recipient_user_id') or '').strip()
            if raw_target in {'bot', 'system'}:
                return
            care_target = raw_target
        elif intent.get('kind') in {'care_wait', 'social_chat'}:
            authority = await self.call('dynamic_life_state', 'get_intent', intent['intent_id'])
            live = (authority or {}).get('item') or {}
            if (live.get('status') != 'pending' or not live.get('managed') or live.get('paused')
                    or live.get('outreach_paused') or not same_scope(live.get('scope', {}), scope)
                    or live.get('kind') != intent.get('kind') or not live.get('source_event_id')):
                return
            intent = live
            care_target = str(live.get('recipient_user_id') or '')
            if not care_target or care_target in {'bot', 'system'}:
                return
        if private_recipient and care_target != private_recipient:
            return
        event = self.internal_event(scope)
        boundary_state = member_boundary.resolve_state(p, event)
        if not boundary_state.available and (member_boundary.fail_closed(boundary_state)
                                             or boundary_state.service is not None):
            p._metric("unattributed_proactive_dispatch")
            return
        if boundary_state.available and not boundary_state.disabled and not care_target:
            # Intents currently have a group scope but no trusted recipient set.
            # Never borrow the most recent speaker as their author or target.
            p._metric("unattributed_proactive_dispatch")
            return
        if care_target:
            policy, token, state = member_boundary.peek(p, event, care_target)
            if ((not state.available and (member_boundary.fail_closed(state) or state.service is not None))
                    or member_boundary.blocked(policy, purpose='send')):
                return
            captured, state = member_boundary.capture_policy_snapshot(p, event)
            if state.available and not state.disabled and captured is None:
                return
            event._chat_dynamics_boundary_uid = care_target
            event._chat_dynamics_boundary_token = token
            event._chat_dynamics_boundary_policy_snapshot = captured
            event._narrative_care_target_v1 = care_target
            event._extras['_narrative_recipient_v1'] = care_target
        host = await p.persona_engine.bridge.snapshot(event)
        if private_recipient:
            key = scope["umo"]
            runtime = p._get_or_create_runtime(key, group_id="", umo=key, scope_kind="private")
        else:
            key = p._resolve_session_key(scope["umo"]) or scope["umo"]
            group = scope["umo"].split(":", 2)[-1]
            runtime = p._get_or_create_runtime(key, group_id=group, umo=scope["umo"])
        now = p.time_service.time()
        reason = str(intent.get("reason", ""))[:600]
        if is_horizon:
            sanitized_motive = dynamics_horizons._sanitize_value(
                {
                    "kind": str(intent.get("kind") or "horizon_share")[:64],
                    "horizon_kind": str(intent.get("horizon_kind") or "")[:64],
                    "recipient_user_id": care_target,
                    "topic": str(intent.get("topic") or "")[:120],
                    "topics": list(intent.get("topics") or ())[:6] if isinstance(intent.get("topics"), (list, tuple)) else [],
                    "evidence_quote": str(intent.get("evidence_quote") or "")[:200],
                    "summary": str(intent.get("summary") or intent.get("digest") or "")[:280],
                    "reflection": str(intent.get("reflection") or "")[:240],
                    "open_question": str(intent.get("open_question") or intent.get("question") or "")[:200],
                    "reason": reason,
                    "now": p.time_service.wall_time(),
                    "guidance": (
                        "这是角色最近阅读或观看外部见闻后产生的一次主动分享候选，不是成员的新发言。"
                        "结合当前会话的近期上下文判断是否适合自然提起；若对方正忙、冲突、休息或话题突兀就ignore。"
                        "决定分享时用第一人称日常闲聊口吻简短说一句感受或轻抛一个小问题，严禁像摘要机器人一样背诵，"
                        "严禁输出原始 http/https 链接，未经询问不主动报出完整标题或 BV 号。"
                    ),
                },
                str_limit=300,
            )
            reason = json.dumps(sanitized_motive, ensure_ascii=False)
        elif care_target:
            social_chat = intent.get('kind') == 'social_chat'
            topic_source = None
            if social_chat and intent.get('topic_candidate_id'):
                source = intent.get('topic_source')
                if (not isinstance(source, dict)
                        or source.get('candidate_id') != intent['topic_candidate_id']
                        or source.get('basis') not in {'interest', 'preference', 'recent_experience'}
                        or source.get('fact_kind') not in {'fiction', 'subjective'}
                        or not isinstance(source.get('text'), str) or not source['text'].strip()
                        or not isinstance(source.get('source_ids'), list) or not source['source_ids']):
                    return
                topic_source = {k: copy.deepcopy(source[k]) for k in
                                ('candidate_id', 'basis', 'text', 'fact_kind', 'source_ids', 'fingerprint') if k in source}
            guidance = ('这是根据同一成员既有互动和共同话题提出的一次主动聊天候选，不是成员的新发言。'
                        '围绕具体话题给出一个自然的新角度或分享，再轻轻邀请交流；不催问、不假装对方刚刚提问，'
                        '不把互动次数当成亲密关系。对方忙、休息、拒绝或话题不合时宜就ignore。') if social_chat else (
                        '这是有原话依据的一次主动关心候选，不是成员的新发言。结合当前状态决定是否联系；可以ignore。'
                        '仅向指定成员轻轻问候一次，不断言对方出事，不责备未回复，不催促，不制造关系压力。')
            if topic_source is not None:
                source_context = '此私聊内发生过互动' if private_recipient else '本群发生过公开互动'
                sensitive_context = '敏感话题' if private_recipient else '私人对话'
                guidance = (
                    '这是角色基于自己已有兴趣或近期生活记录发起的轻量话题候选，不是成员刚刚提出的问题。'
                    f'成员原话只证明{source_context}，不证明对方与角色有共同经历或同样兴趣。'
                    'topic_source是已校验的独立话题依据：subjective是自己的偏好或兴趣，fiction是角色的虚构日常记录；'
                    '不把虚构经历当作真实世界或群友事实，不把打算当作已经完成。'
                    '只有能拿出一个在本会话里聊得起来的具体发现、看法或问题，才考虑开场。'
                    '如果只能报刚吃饭、起床、休息、换地方或做完一件日常小事，或只能靠硬比喻接话，就ignore。'
                    '决定开场时直接说值得聊的细节，不以日常动作为开头，不复述证据字段或来源ID。'
                    f'不催问、不提对方未回复、不制造熟悉或亲密关系；遇到{sensitive_context}、冲突、忙碌、休息、拒绝或不合时宜就ignore。'
                )
            reason = json.dumps({'kind': 'social_chat' if social_chat else 'contextual_care', 'recipient_user_id': care_target,
                'topic': intent.get('topic',''),
                'topic_source': topic_source,
                'reason': reason, 'waiting_for': intent.get('waiting_for', ''),
                'evidence_quote': intent.get('evidence_quote', ''),
                'source_occurred_at': intent.get('source_occurred_at'), 'now': p.time_service.wall_time(),
                'guidance': guidance}, ensure_ascii=False)
        message = MessageSnapshot(message_id="intent:" + intent["intent_id"], author="bot", text=reason, timestamp=now)
        turn = TurnContext(session_key=key, author="bot", text=reason, messages=(message,), background=(),
                           epoch=runtime.epoch, revision=runtime.user_revisions.get("bot", 0), started_at=now,
                           explicit=False, wake_kind="ambient", bot_id=runtime.bot_id)
        recent = runtime.dag.get_recent_nodes(limit=12) if runtime.dag is not None else []
        turn = replace(turn, background=tuple(
            MessageSnapshot(message_id=str(node.msg_id), author=str(node.user_id), text=str(node.text), timestamp=node.timestamp)
            for node in recent))
        item = ModelTurn(turn, (event,), {"contact_intent": intent}, False)
        bundle = await self.prepare(runtime, item, host)
        if bundle is not None and not same_scope(bundle.scope, scope):
            if not is_horizon or private_recipient:
                return
            scope = dict(bundle.scope)
        if bundle is None and not is_horizon:
            return
        standalone_turn_id = "intent-turn:" + hashlib.sha256(
            f"{scope.get('umo', '')}|{intent['intent_id']}|{now}".encode()
        ).hexdigest()[:24]
        if bundle is not None:
            item = replace(item, narrative_bundle=bundle)
            # An internal motive is not a real incoming user statement.
            data = bundle.wire()
            data["intent_id"] = intent["intent_id"]
            data["internal"] = True
            if private_recipient:
                data["private_outreach"] = True
            data["turn_id"] = bundle.turn_id
            data["bundle_id"] = bundle.bundle_id
            event._extras[MANAGED_KEY] = data
            decision_persona = PersonaSnapshot(host.fingerprint, host.conversation_id, host.persona_id,
                                               data["character"]["core_prompt"] + "\n" + data["character"].get("group_rules", ""))
        else:
            decision_persona = host
        dynamics_horizons.begin(p, runtime, item, decision_persona)
        decision = await p.persona_engine.decide(item, decision_persona, runtime.interaction_state, runtime=runtime)
        if care_target:
            event._extras['_narrative_recipient_v1'] = care_target
        if decision.action == "ignore":
            if care_target or is_horizon:
                await self.call(auth_provider, 'cancel_intent', intent['intent_id'])
                await self._io.call('intent_status', self.journal.intent_status, intent['intent_id'], 'cancelled')
            return
        async with p.persona_engine.bridge.session_lock(scope["umo"]):
            if bundle is not None and not await self.current(bundle, event):
                return
            provider = await p.llm.resolve_provider_id(scope["umo"])
            base_reply_prompt = reply_prompt(
                turn,
                decision,
                reply_guidance=REPLY_GUIDANCE,
                character_context=(
                    {"horizons": h_slot}
                    if bundle is None and (h_slot := dynamics_horizons.decision_slot(runtime, item))
                    else None
                ),
                include_reply_guidance=bundle is None,
            )
            if is_horizon:
                base_reply_prompt = dynamics_horizons.augment_reply(base_reply_prompt, runtime, item)
                # Disable external tools on standalone internal proactive motives as well.
                event._extras.setdefault("_horizons_internal_v1", True)
            output = await p.persona_engine.generate(event, (event,), base_reply_prompt, host, provider)
            fragments = delivery_fragments(output.chains, output.text, p.pacer, max_fragments=1)
            if not fragments:
                return
            fragment = fragments[0]
            if care_target and not private_recipient:
                from astrbot.api.event import MessageChain
                from astrbot.api.message_components import At, Plain
                if isinstance(fragment, str):
                    fragment = MessageChain([Plain(fragment)])
                fragment.chain.insert(0, At(qq=care_target))
            if is_horizon:
                if bundle is not None:
                    current = await self.call("dynamic_life_state", "get_snapshot", scope)
                    if (
                        not current
                        or current.get("revision") != bundle.snapshot.get("revision")
                        or current.get("scope_revision") != bundle.snapshot.get("scope_revision")
                    ):
                        return
                due = await self.call("dynamics_horizons", "list_due_intents", p.time_service.wall_time())
                raw_live = due if isinstance(due, list) else (due or {}).get("items", [])
                live = [
                    norm
                    for x in (raw_live if isinstance(raw_live, list) else ())
                    if (norm := self._normalize_due_intent(x, "dynamics_horizons", p.time_service.wall_time())) is not None
                ]
                if not any(x.get("intent_id") == intent["intent_id"] for x in live):
                    return
            else:
                current = await self.call("dynamic_life_state", "get_snapshot", scope)
                due = await self.call("dynamic_life_state", "list_due_intents", p.time_service.wall_time())
                live = due if isinstance(due, list) else (due or {}).get("items", [])
                if (not current or current.get("revision") != bundle.snapshot.get("revision")
                        or current.get("scope_revision") != bundle.snapshot.get("scope_revision")
                        or not any(x.get("intent_id") == intent["intent_id"] for x in live)):
                    return
            async with runtime.send_lock:
                if (not self.intent_outreach_allowed(intent) or p.shadow_mode or p._shutting_down
                        or runtime.epoch != turn.epoch or p._sessions.get(key) is not runtime
                        or p.arbiter.is_in_deep_cooling(key, current_time=p.time_service.time())
                        or not self._takeover_enabled(scope["umo"])
                        or (bundle is not None and not await self.current(bundle, event))):
                    return
                claimed = await self.call(auth_provider, "update_intent", intent["intent_id"], "claimed")
                if not self._claim_succeeded(claimed):
                    return
                if bundle is None:
                    # Attach intent quota tracking metadata only right before _send_owned.
                    event._extras[MANAGED_KEY] = {
                        "scope": dict(scope),
                        "turn_id": standalone_turn_id,
                        "intent_id": intent["intent_id"],
                        "internal": True,
                        "private_outreach": bool(private_recipient),
                    }
                await self._io.call('intent_status', self.journal.intent_status, intent["intent_id"], "sending")
                send_cancellation = None
                try:
                    if care_target:
                        async def care_effect(original_event, *, before_dispatch):
                            latest = await self.call(auth_provider, 'get_intent', intent['intent_id'])
                            record = self._unwrap_authority_intent(latest) or {}
                            from .platform_bridge import SendResult, send_plain
                            if (
                                record.get('status') != 'claimed'
                                or (not is_horizon and not record.get('managed'))
                                or record.get('paused')
                                or record.get('outreach_paused')
                            ):
                                return SendResult(False, error='care_wait_no_longer_current')
                            async def dispatch_current():
                                return (await before_dispatch()
                                        and not p.shadow_mode and not p._shutting_down
                                        and runtime.epoch == turn.epoch and p._sessions.get(key) is runtime
                                        and self.intent_outreach_allowed(intent)
                                        and self._takeover_enabled(scope['umo']))

                            return await send_plain(
                                original_event, p.context, scope['umo'], fragment,
                                before_dispatch=dispatch_current,
                            )
                        result = await p._send_owned(runtime, event, fragment, guarded_send_effect=care_effect)
                    else:
                        result = await p._send_owned(runtime, event, fragment)
                except asyncio.CancelledError as exc:
                    result = getattr(exc, "chat_dynamics_send_result", None)
                    if result is None or not result.success:
                        status = "uncertain" if result is None or result.delivery_uncertain else "failed"
                        await self._io.call('intent_status', self.journal.intent_status, intent["intent_id"], status)
                        await self.call(auth_provider, "update_intent", intent["intent_id"], status)
                        raise
                    send_cancellation = exc
                except Exception:
                    await self._io.call('intent_status', self.journal.intent_status, intent["intent_id"], "uncertain")
                    await self.call(auth_provider, "update_intent", intent["intent_id"], "uncertain")
                    raise
            status = "sent" if result.success else "uncertain" if result.delivery_uncertain else "failed"
            await self._io.call('intent_status', self.journal.intent_status, intent["intent_id"], status)
            await self.call(auth_provider, "update_intent", intent["intent_id"], status)
            if result.success:
                delivered = result.delivered_text(fragment, media_placeholder=False)
                await p.persona_engine.bridge.commit(event, output, delivered)
                mid = str(result.message_id or p._next_outgoing_id())
                node = runtime.dag.add_message(msg_id=mid, user_id=runtime.bot_id or "bot", text=delivered,
                                              timestamp=p.time_service.time(), metadata={
                    "platform_message_id": bool(result.message_id), "sender_is_bot": True,
                    "narrative_intent_id": intent["intent_id"], "dialogue_delivered": True,
                    "recipient_user_id": care_target, "trigger_user_id": care_target or 'bot',
                    "dialogue_state": decision.state, "dialogue_action": decision.action})
                runtime.remember_sent(mid)
                runtime.last_bot_node = node
                runtime.last_model_send = p.time_service.time()
                p._last_bot_nodes[key] = node
                p._observe_routed_bot(runtime, node)
                p.arbiter.record_bot_spoke(key, timestamp=p.time_service.time(), user_id=care_target or 'bot')
                rhythm_ctrl = getattr(p, "natural_rhythm", None)
                if rhythm_ctrl is not None:
                    rhythm_ctrl.note_outreach_sent(key)
                if is_horizon:
                    dynamics_horizons.notify_proactive_delivered(
                        p,
                        umo=scope["umo"],
                        intent=intent,
                        reply_text=delivered,
                        message_id=mid,
                        character_scope=scope,
                    )
            if send_cancellation is not None:
                raise send_cancellation

    def internal_event(self, scope):
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.message.components import Plain

        context = self.plugin.context
        platform = context.get_platform_inst(scope["platform"])
        if platform is None:
            raise ValueError("narrative platform unavailable")
        metadata = platform.meta()
        message = AstrBotMessage()
        umo = scope["umo"]
        private_recipient = private_message_recipient(umo)
        message.type = MessageType.FRIEND_MESSAGE if private_recipient else MessageType.GROUP_MESSAGE
        message.group_id = "" if private_recipient else umo.split(":", 2)[-1]
        runtime = self.plugin._sessions.get(umo)
        message.self_id = getattr(runtime, "bot_id", "") or ("" if private_recipient else "bot")
        message.sender = MessageMember("bot", "角色内部事件")
        message.message_id = "narrative:" + uuid.uuid4().hex
        message.message = [Plain("")]
        message.message_str = ""

        class InternalEvent(AstrMessageEvent):
            async def send(self, chain):
                return await context.send_message(scope["umo"], chain)

        event_type = InternalEvent
        if metadata.name == "aiocqhttp":
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

            send_by_origin = getattr(AiocqhttpMessageEvent, "send_by_origin", None)
            if callable(send_by_origin):
                class OneBotInternalEvent(InternalEvent):
                    _supports_before_dispatch = True

                    async def send(self, chain, *, _before_dispatch=None):
                        if _before_dispatch is None:
                            return await super().send(chain)
                        return await send_by_origin(
                            context, scope["umo"], platform, chain, _before_dispatch,
                        )

                event_type = OneBotInternalEvent
        event = event_type("", message, metadata, private_recipient or message.group_id)
        event.unified_msg_origin = scope["umo"]
        event.set_extra("_narrative_internal_v1", True)
        if private_recipient:
            event._chat_dynamics_scope_kind = "private"
        return event

    async def admin(self, umo):
        bundle = self._bundles.get(umo)
        scope = bundle.scope if bundle else {"platform": umo.rsplit(":", 2)[0], "umo": umo,
                                            "persona_id": "", "instance_id": ""}
        if not bundle:
            character = await self.call("worldbook", "get_character_context", scope, None)
            if character:
                scope = character.get("scope", scope)
        life = await self.call("dynamic_life_state", "get_narrative_admin", scope)
        material = bundle.wire() if bundle else {}
        sources = {name: {"provider": name, "available": material.get("availability", {}).get(name, False),
                          "status": "已收集" if material.get("availability", {}).get(name) else "暂不可用",
                          "source_ids": [row["source_id"] for row in material.get("context_items", []) if row.get("provider") == name]}
                   for name in material.get("completed", {})}
        if "worldbook" in sources:
            sources["worldbook"].update(source_ids=material["character"].get("source_ids", []), fingerprint=material["character"].get("fingerprint"))
        if "dynamic_life_state" in sources:
            sources["dynamic_life_state"].update(source_ids=material["snapshot"].get("source_ids", []),
                                                 revision=material["snapshot"].get("revision"), scope_revision=material["snapshot"].get("scope_revision"))
        return {"schema_version": 1, "enabled": self.enabled(umo), "life": life or {},
                "storage": self.journal.storage_status(),
                "materials": sources,
                **(await asyncio.to_thread(self.journal.snapshot, umo))}

    async def inspect_memory(self, umo, query):
        """Probe the same live memory capability without creating chat events."""
        if not self.enabled(umo):
            return {"schema_version": 1, "available": False, "reason": "unmanaged"}
        scope = {"platform": umo.rsplit(":", 2)[0], "umo": umo,
                 "persona_id": "", "instance_id": ""}
        character = await self.call("worldbook", "get_character_context", scope, None)
        if not character or character.get("disabled") or not character.get("available", True):
            return {"schema_version": 1, "available": False, "reason": "character_unavailable"}
        scope = dict(character.get("scope") or {})
        if scope.get("umo") != umo or not scope.get("persona_id"):
            return {"schema_version": 1, "available": False, "reason": "invalid_scope"}
        if isinstance(character.get("name"), str) and character["name"].strip():
            scope["persona_aliases"] = [character["name"].strip()[:100]]
        result = await self.call("livingmemory", "recall_context", scope, query,
                                 timeout=MEMORY_RECALL_TIMEOUT_SECONDS)
        if result is None:
            return {"schema_version": 1, "available": False, "reason": "recall_unavailable"}
        return result

    async def inspect_social(self, umo, participants):
        """Read the live scoped relationship interface for an administrator."""
        if not self.enabled(umo):
            return {"schema_version": 1, "available": False}
        character = await self.call("worldbook", "get_character_context",
            {"platform": umo.rsplit(":", 2)[0], "umo": umo, "persona_id": "", "instance_id": ""}, None)
        if not character or character.get("disabled") or not character.get("available", True):
            return {"schema_version": 1, "available": False}
        scope = character.get("scope") or {}
        if scope.get("umo") != umo or not scope.get("persona_id"):
            return {"schema_version": 1, "available": False}
        return await self.call("self_learning", "get_social_context", scope, participants, timeout=8) or {
            "schema_version": 1, "available": False}
