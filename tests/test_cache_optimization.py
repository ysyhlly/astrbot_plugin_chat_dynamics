"""Regression tests for CD-1 through CD-6 prefix-cache and token optimizations."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_chat_dynamics.core import vision_context as vision
from astrbot_plugin_chat_dynamics.core.character_view import (
    HistoryViewProvider,
    _LM_MATERIAL_PREFIX,
    build_lm_final_material_payload,
    history_view,
)
from astrbot_plugin_chat_dynamics.core.config import parse_runtime_config
from astrbot_plugin_chat_dynamics.core.context_retrieval import CONTEXT_INSTRUCTIONS
from astrbot_plugin_chat_dynamics.core.jev_decision import build_questions, build_state
from astrbot_plugin_chat_dynamics.core.llm_decision import LLMDecisionClient, split_decision_payload
from astrbot_plugin_chat_dynamics.core.media_reply_policy import media_reply_instructions
from astrbot_plugin_chat_dynamics.core.reply_adaptation import measure as measure_adaptation
from astrbot_plugin_chat_dynamics.core.turn_decision import (
    REPLY_INSTRUCTIONS,
    MessageSnapshot,
    TurnContext,
    TurnDecision,
    reply_prompt,
)


def _turn(text="今天天气真不错", *, msg_id="m1", author="u1", messages=None, wake_kind="none"):
    msgs = messages or (MessageSnapshot(msg_id, author, text, author_name="小明", platform="qq"),)
    return TurnContext("room:1", author, text, msgs, (), 0, 0, 100.0, False, wake_kind=wake_kind, bot_id="bot1")


def _aligned_boundary(raw_prefix, *, evidence_view=None, protected=()):
    spans = [(i, i + 1) for i in range(len(raw_prefix))]
    boundary = {
        "state": "aligned",
        "reason": "",
        "prefix_count": len(raw_prefix),
        "raw_prefix": raw_prefix,
        "spans": spans,
        "protected_raw_indices": list(protected),
    }
    if evidence_view is not None:
        boundary["evidence_identity"] = "matched"
        boundary["evidence_view"] = evidence_view
        boundary["evidence_position_offset"] = 0
    return boundary


def _make_evidence_history(round_count: int, *, repeated_user_text: bool = False):
    raw_prefix = []
    history_projection = {}
    atoms = []
    for idx in range(round_count):
        user_text = (
            "这是一段超过八十个字符的重复历史背景文本，用于验证旧版全局重打包不再触发前缀缓存抖动。" * 2
            if repeated_user_text
            else f"用户第{idx}轮消息内容"
        )
        asst_text = f"助手第{idx}轮回复内容"
        raw_prefix.append({"role": "user", "content": user_text})
        a_pos = len(raw_prefix)
        raw_prefix.append({"role": "assistant", "content": asst_text})
        mat_id = f"mat-{idx}"
        history_projection[mat_id] = {
            "material_id": mat_id,
            "source_position": a_pos,
            "source_role": "assistant",
            "text_hash": hashlib.sha256(asst_text.encode("utf-8")).hexdigest(),
            "projection_status": "atoms_only",
        }
        atoms.append({
            "atom_id": f"atom-{idx}",
            "claim": f"第{idx}轮已核实事实",
            "source_refs": [{"material_id": mat_id}],
        })
    evidence_view = {
        "schema_version": 1,
        "available": True,
        "purpose": "reply",
        "card_version": "v1",
        "scope_key": "scope:test",
        "correction_revision": 0,
        "atoms": atoms,
        "slots": {},
        "history_projection": history_projection,
        "revocations": [],
    }
    return raw_prefix, evidence_view


# ---- CD-1: Static REPLY_INSTRUCTIONS & media_reply_policy dedup ----


def test_reply_instructions_omits_main_vision_hint_and_includes_context_policy():
    assert vision.MAIN_VISION_HINT not in REPLY_INSTRUCTIONS
    assert CONTEXT_INSTRUCTIONS in REPLY_INSTRUCTIONS


def test_media_reply_instructions_concise_with_static_flag_and_full_fallback():
    def make_event(extras):
        return SimpleNamespace(get_extra=lambda key, default=False: extras.get(key, default))

    static_event = make_event({"visual_media_policy_v1": True, "_visual_media_static_policy_v1": True})
    legacy_event = make_event({"visual_media_policy_v1": True})
    absent_event = make_event({})

    concise = media_reply_instructions(static_event, internal=False)
    fallback = media_reply_instructions(legacy_event, internal=False)

    assert concise and fallback
    assert len(concise) < len(fallback)
    assert "当前回复会话" in concise and "不扩大其他外部工具" in concise and "实际调用工具" in concise
    # Concise mode must not duplicate visual plan format instructions already covered by static MEDIA_POLICY.
    assert "视觉发送计划" not in concise
    assert "视觉发送计划" in fallback
    assert media_reply_instructions(absent_event, internal=False) == ""
    assert media_reply_instructions(static_event, internal=True) == ""


# ---- CD-2 / CD-3 / CD-6: Config schema defaults & validation ----


def test_runtime_config_cache_defaults_and_validation():
    cfg, warnings = parse_runtime_config({})
    assert cfg.stream_safe_strip_tools is False
    assert cfg.history_cache_high_rounds == 36
    assert cfg.history_cache_low_rounds == 20
    assert cfg.force_refine_host_caption is False
    assert not warnings

    custom, warnings = parse_runtime_config({
        "stream_safe_strip_tools": True,
        "history_cache_high_rounds": 40,
        "history_cache_low_rounds": 24,
        "force_refine_host_caption": True,
    })
    assert custom.stream_safe_strip_tools is True
    assert custom.history_cache_high_rounds == 40
    assert custom.history_cache_low_rounds == 24
    assert custom.force_refine_host_caption is True
    assert not warnings

    bad, warnings = parse_runtime_config({"history_cache_high_rounds": 20, "history_cache_low_rounds": 24})
    assert bad.history_cache_high_rounds == 36 and bad.history_cache_low_rounds == 20
    assert any("history_cache_low_rounds" in w for w in warnings)


# ---- CD-3: HistoryViewProvider immutable prefix, hysteresis trim, chunk-boundary image strip ----


def test_history_view_evidence_append_only_byte_invariance_across_consecutive_turns():
    store = {}
    prev_projected = None
    for total_rounds in range(1, 13):
        raw_prefix, evidence_view = _make_evidence_history(total_rounds, repeated_user_text=True)
        current_turn = {"role": "user", "content": f"当前第{total_rounds}轮问题"}
        contexts = [*raw_prefix, current_turn]
        boundary = _aligned_boundary(raw_prefix, evidence_view=evidence_view)
        viewed, diag = history_view(
            contexts, boundary, session_key="s:evidence", watermark_store=store
        )
        projected_prefix = viewed[:-1]
        assert len(projected_prefix) == total_rounds
        assert diag["converted_rounds"] == total_rounds
        assert diag["history_trimmed_rounds"] == 0
        for row in projected_prefix:
            assert row["content"].startswith("CHAT_DYNAMICS_EVIDENCE_HISTORY_V1\n")
            assert "CHAT_DYNAMICS_EVIDENCE_HISTORY_COMPACT_V1" not in row["content"]
        if prev_projected is not None:
            # Byte-identical prefix invariant across consecutive turns!
            assert projected_prefix[: len(prev_projected)] == prev_projected
        prev_projected = projected_prefix


def test_history_view_plain_append_only_byte_invariance():
    store = {}
    prev_projected = None
    for total_rounds in range(1, 10):
        raw_prefix = []
        for idx in range(total_rounds):
            raw_prefix.append({"role": "user", "content": f"u{idx}"})
            raw_prefix.append({"role": "assistant", "content": f"a{idx}"})
        contexts = [*raw_prefix, {"role": "user", "content": "now"}]
        viewed, diag = history_view(
            contexts, _aligned_boundary(raw_prefix), session_key="s:plain", watermark_store=store
        )
        projected_prefix = viewed[:-1]
        assert len(projected_prefix) == total_rounds
        assert diag["converted_rounds"] == total_rounds
        for row in projected_prefix:
            assert row["content"].startswith("CHAT_DYNAMICS_HISTORY_SOURCE_V1\n")
        if prev_projected is not None:
            assert projected_prefix[: len(prev_projected)] == prev_projected
        prev_projected = projected_prefix


def test_history_view_hysteresis_trim_36_to_20_and_append_only_window():
    store = {}
    all_raw, full_view = _make_evidence_history(56)
    protected_example = [
        {"role": "user", "content": "人设示例问"},
        {"role": "assistant", "content": "人设示例答"},
    ]

    def run_turn(start_round: int, end_round: int):
        slice_raw = all_raw[start_round * 2 : end_round * 2]
        raw_prefix = [*protected_example, *slice_raw]
        # Shift source_position in history_projection for the slice
        offset_ev = dict(full_view)
        shifted_proj = {}
        for mid, row in full_view["history_projection"].items():
            pos = row["source_position"] - start_round * 2 + len(protected_example)
            if pos >= len(protected_example):
                shifted_proj[mid] = {**row, "source_position": pos}
        offset_ev["history_projection"] = shifted_proj
        contexts = [*raw_prefix, {"role": "user", "content": f"cur-{end_round}"}]
        boundary = _aligned_boundary(raw_prefix, evidence_view=offset_ev, protected=(0, 1))
        return history_view(
            contexts,
            boundary,
            session_key="s:hysteresis",
            high_rounds=36,
            low_rounds=20,
            watermark_store=store,
        )

    # Rounds 1..36: strictly append-only, zero trims.
    prev_prefix = None
    for n in range(1, 37):
        viewed, diag = run_turn(0, n)
        prefix = viewed[:-1]
        assert diag["history_trimmed_rounds"] == 0
        assert diag["history_kept_rounds"] == n
        # Protected example always retained at index 0..1.
        assert prefix[:2] == protected_example
        if prev_prefix is not None:
            assert prefix[: len(prev_prefix)] == prev_prefix
        prev_prefix = prefix

    # Round 37 (> 36): hysteresis trim fires ONCE, dropping back to 20 kept rounds.
    viewed_37, diag_37 = run_turn(0, 37)
    assert diag_37["history_kept_rounds"] == 20
    assert diag_37["history_trimmed_rounds"] == 17
    assert viewed_37[:2] == protected_example
    prev_window_prefix = viewed_37[:-1]

    # Rounds 38..53 (21..36 kept rounds): 16 consecutive turns strictly append-only!
    for n in range(38, 54):
        # Even simulate host sliding off already-trimmed oldest rounds after round 42.
        host_drop = 5 if n >= 42 else 0
        viewed, diag = run_turn(host_drop, n)
        prefix = viewed[:-1]
        assert diag["history_kept_rounds"] == n - 17
        assert prefix[: len(prev_window_prefix)] == prev_window_prefix
        prev_window_prefix = prefix

    # Round 54 (37th kept round): trims in one step back to 20 again!
    viewed_54, diag_54 = run_turn(5, 54)
    assert diag_54["history_kept_rounds"] == 20
    assert diag_54["history_watermark_round"] == 34


def test_history_view_chunk_boundary_stale_image_stripping():
    store = {}
    image_rounds = {
        0: [
            {"type": "text", "text": "<image_caption>窗台上的橘猫</image_caption>"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 500}},
        ],
        2: [
            {"type": "text", "text": "看看这张"},
            {"type": "image_url", "image_url": {"url": "https://cdn.example.com/photos/cat_photo.png?sig=1"}},
        ],
        8: [
            {"type": "text", "text": "第八轮图"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "B" * 500}},
        ],
        18: [
            {"type": "text", "text": "近期图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "C" * 500}},
        ],
    }

    def build_prefix(count: int):
        rows = []
        for idx in range(count):
            if idx in image_rounds:
                rows.append({"role": "user", "content": image_rounds[idx]})
            else:
                rows.append({"role": "user", "content": f"纯文字第{idx}轮"})
            rows.append({"role": "assistant", "content": f"回复第{idx}轮"})
        return rows

    # Turns 1..13: no chunk has crossed (abs_end - 6) >= 8 -> zero images stripped, 100% byte-identical prefix.
    prev = None
    for n in range(1, 14):
        raw = build_prefix(n)
        viewed, diag = history_view(
            [*raw, {"role": "user", "content": "now"}],
            _aligned_boundary(raw),
            session_key="s:img",
            chunk_size=8,
            recent_image_tail_rounds=6,
            watermark_store=store,
        )
        prefix = viewed[:-1]
        assert diag["history_stripped_images"] == 0
        if prev is not None:
            assert prefix[: len(prev)] == prev
        prev = prefix

    # Turn 14: (14 - 6) // 8 * 8 = 8 -> Chunk 0 [0..7] has rounds 0 and 2 stripped ONCE!
    raw_14 = build_prefix(14)
    viewed_14, diag_14 = history_view(
        [*raw_14, {"role": "user", "content": "now"}],
        _aligned_boundary(raw_14),
        session_key="s:img",
        chunk_size=8,
        recent_image_tail_rounds=6,
        watermark_store=store,
    )
    assert diag_14["history_stripped_images"] == 2
    serialized_14 = json.dumps(viewed_14[:-1], ensure_ascii=False)
    assert "A" * 500 not in serialized_14
    assert "窗台上的橘猫" in serialized_14
    assert "cat_photo.png" in serialized_14
    assert "B" * 500 in serialized_14  # Round 8 is in Chunk 1, still intact!

    # Turns 15..21: strip boundary stays 8 -> 7 consecutive turns with 100% byte-identical prefix!
    prev_after_chunk0 = viewed_14[:-1]
    for n in range(15, 22):
        raw = build_prefix(n)
        viewed, diag = history_view(
            [*raw, {"role": "user", "content": "now"}],
            _aligned_boundary(raw),
            session_key="s:img",
            chunk_size=8,
            recent_image_tail_rounds=6,
            watermark_store=store,
        )
        prefix = viewed[:-1]
        assert diag["history_stripped_images"] == 2
        assert prefix[: len(prev_after_chunk0)] == prev_after_chunk0
        prev_after_chunk0 = prefix

    # Turn 22: (22 - 6) // 8 * 8 = 16 -> Chunk 1 [8..15] stripped, Round 18 (within last 6 tail) stays intact!
    raw_22 = build_prefix(22)
    viewed_22, diag_22 = history_view(
        [*raw_22, {"role": "user", "content": "now"}],
        _aligned_boundary(raw_22),
        session_key="s:img",
        chunk_size=8,
        recent_image_tail_rounds=6,
        watermark_store=store,
    )
    assert diag_22["history_stripped_images"] == 3
    serialized_22 = json.dumps(viewed_22[:-1], ensure_ascii=False)
    assert "B" * 500 not in serialized_22
    assert "C" * 500 in serialized_22  # Recent tail round 18 keeps its inline image!


@pytest.mark.asyncio
async def test_history_view_provider_preserves_provenance_and_diagnostics():
    material = {
        "snapshot": {"revision": 2, "stale": False, "empty_str": "", "none_val": None},
        "context_items": [
            {"provider": "livingmemory", "source_id": "a1", "text": "记住他喜欢蓝莓派"},
        ],
        "evidence_slots": {
            "current_evidence": [{"atom_id": "a1", "claim": "喜欢蓝莓派"}],
            "empty_slot": [],
        },
        "evidence_detail": "available_in_local_turn_journal",
        "member_profile": {"member_id": "u1", "items": [], "usage": "重复静态说明"},
        "conversation_adaptation": {
            "schema_version": 1,
            "member_id": "u1",
            "personality_locked": True,
            "member_profile_path": "member_profile",
            "shared_context": {"memory_refs": [], "boundary_refs": [], "dialogue_ref": None, "usage": "重复静态"},
            "rhythm": {"action": "reply", "length": "brief", "mode": "focused", "authority": "重复静态", "user_updates": 0},
            "priority": ["a", "b"],
            "usage": "重复静态",
        },
    }
    managed = {
        "evidence_view": {
            "atoms": [{"atom_id": "a1", "claim": "喜欢蓝莓派"}],
        }
    }
    material_text, provenance = build_lm_final_material_payload(material, managed)
    raw_json = material_text[len(_LM_MATERIAL_PREFIX) :]
    parsed = json.loads(raw_json)
    # Compact serialization and empty/duplicate stripping
    assert '": ' not in raw_json and '", "' not in raw_json
    assert parsed["snapshot"]["stale"] is False
    assert "empty_str" not in parsed["snapshot"]
    assert "none_val" not in parsed["snapshot"]
    assert "evidence_detail" not in parsed
    assert "usage" not in parsed["member_profile"]
    assert "priority" not in parsed["conversation_adaptation"]
    assert "authority" not in parsed["conversation_adaptation"]["rhythm"]
    assert parsed["conversation_adaptation"]["rhythm"]["user_updates"] == 0

    raw_prefix, evidence_view = _make_evidence_history(3)
    contexts = [
        *raw_prefix,
        {"role": "user", "content": [{"type": "text", "text": "当前问题"}, {"type": "text", "text": material_text}]},
    ]
    captured = {}

    class DummyProvider:
        async def text_chat(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(completion_text="ok")

    wrapper = HistoryViewProvider(
        DummyProvider(),
        boundary_callback=lambda ctxs, _p: _aligned_boundary(raw_prefix, evidence_view=evidence_view),
        lm_material_provenance=provenance,
        session_key="s:prov",
        watermark_store={},
    )
    await wrapper.text_chat(contexts=contexts)
    diag = wrapper.last_diagnostic
    assert diag["lm_measurement_stage"] == "provider_kwargs_after_boundaries"
    assert diag["lm_measurement_hash_match_count"] == 1
    assert diag["lm_kept_context_items"] == 1
    assert diag["lm_kept_atoms"] == 1
    assert diag["reply_adaptation_stage"] == "provider_kwargs_after_boundaries"
    assert measure_adaptation(captured["contexts"], provenance)["reply_adaptation_stage"] == "provider_kwargs_after_boundaries"


# ---- CD-4: Compact reply_prompt ----


def test_reply_prompt_compact_json_strips_empty_and_deduplicates():
    turn = _turn("帮我看下这段代码")
    decision = TurnDecision("reply", "focused", ("m1",), "回答代码问题", "brief", "addressed_question")
    serialized = reply_prompt(
        turn,
        decision,
        delivery_constraints={"length_hint": "brief", "rhythm_state": "awake", "rhythm_action": ""},
        character_context={"empty": [], "note": ""},
    )
    assert '": ' not in serialized
    data = json.loads(serialized)
    assert "context_policy" not in data["conversation"]
    assert "length" not in data["attention_plan"]
    assert "length_hint" not in data.get("delivery_constraints", {})
    assert data["delivery_constraints"] == {"rhythm_state": "awake"}
    assert "character_state" not in data
    # Empty optional TurnDecision record fields stripped; core fields preserved.
    assert set(data["response_plan"]) == {"action", "state", "target_message_ids", "response_goal", "length", "reason_code"}
    assert data["conversation"]["background"] == []
    assert data["conversation"]["truncated"] is False


# ---- CD-5: Static-first decision prompts ----


def test_jev_build_state_orders_static_keys_before_volatile_conversation():
    turn = _turn("明天去哪里吃饭")
    state = build_state(
        turn,
        previous_state="casual",
        observations={"queue_delay_seconds": 0.1},
        presence="sensible",
        persona_prompt="温柔理性的朋友",
        character_rules="保持自然口吻",
        affection={"user_id": "u1", "score": 60, "stage": "familiar"},
    )
    keys = list(state.keys())
    conv_index = keys.index("conversation")
    obs_index = keys.index("observations")
    for static_key in ("identity_policy", "participation_policy", "decision_prompt", "persona", "character_rules"):
        assert keys.index(static_key) < obs_index < conv_index
    if "affection_policy" in keys:
        assert keys.index("affection_policy") < conv_index


@pytest.mark.asyncio
async def test_llm_decision_splits_static_system_prompt_and_keeps_it_byte_identical():
    t1 = _turn("第一轮随便聊聊", msg_id="m1")
    t2 = _turn(
        "第二轮发两条\n补充说明",
        messages=(
            MessageSnapshot("m2a", "u1", "第二轮发两条"),
            MessageSnapshot("m2b", "u1", "补充说明"),
        ),
    )
    s1 = build_state(t1, previous_state="observing", observations={"queue_delay_seconds": 0.2}, persona_prompt="角色设定" * 40, character_rules="角色规则" * 20)
    q1 = build_questions(t1)
    s2 = build_state(t2, previous_state="casual", observations={"queue_delay_seconds": 1.5}, persona_prompt="角色设定" * 40, character_rules="角色规则" * 20)
    q2 = build_questions(t2)
    assert "target" not in q1 and "target" in q2

    sys1, usr1 = split_decision_payload(s1, q1)
    sys2, usr2 = split_decision_payload(s2, q2)

    # Static system prompt is byte-identical across turns even when turn 2 adds dynamic `target` question!
    assert sys1 == sys2
    assert len(sys1) > 4000
    assert "identity_policy" in sys1 and "participation_policy" in sys1 and "character_rules" in sys1 and "persona" in sys1
    assert '"completion"' in sys1 and '"action"' in sys1
    assert "第一轮随便聊聊" not in sys1 and "第二轮发两条" not in sys1

    u1_data = json.loads(usr1)
    u2_data = json.loads(usr2)
    assert "identity_policy" not in u1_data["state"]
    assert "persona" not in u1_data["state"]
    assert "questions" not in u1_data
    assert set(u2_data["questions"]) == {"target"}

    # End-to-end check through LLMDecisionClient.evaluate
    captured_calls = []

    class FakeProvider:
        model_name = "fake-chat"

        async def text_chat(self, **_kwargs):
            return None

    provider = FakeProvider()

    async def fake_llm_generate(*, chat_provider_id, prompt, system_prompt):
        captured_calls.append({"provider_id": chat_provider_id, "prompt": prompt, "system_prompt": system_prompt})
        return SimpleNamespace(
            completion_text=json.dumps({
                "answers": {
                    "completion": {"type": "choice", "choice": "complete", "confidence": 0.9, "probabilities": {"complete": 0.9, "wait": 0.1}},
                    "join": {"type": "noul", "noul": 0.8},
                    "action": {"type": "choice", "choice": "reply", "confidence": 0.85, "probabilities": {"ignore": 0.05, "acknowledge": 0.05, "clarify": 0.02, "reply": 0.85, "close": 0.03}},
                    "state": {"type": "choice", "choice": "casual", "confidence": 0.8, "probabilities": {"observing": 0.04, "casual": 0.8, "focused": 0.04, "supportive": 0.04, "playful": 0.04, "disengaging": 0.04}},
                    "length": {"type": "choice", "choice": "brief", "confidence": 0.8, "probabilities": {"brief": 0.8, "normal": 0.15, "detailed": 0.05}},
                    "reason": {"type": "choice", "choice": "open_group_topic", "confidence": 0.8, "probabilities": {"addressed_request": 0.02, "addressed_question": 0.03, "ongoing_thread": 0.05, "open_group_topic": 0.8, "social_signal": 0.03, "other_recipient": 0.02, "boundary_or_sensitive": 0.02, "low_value_chatter": 0.03}},
                }
            })
        )

    host = SimpleNamespace(
        _shutting_down=False,
        _runtime_config=SimpleNamespace(
            decision_backend="llm",
            decision_llm_provider="chat1",
            reply_provider_id="",
            decision_llm_timeout=5.0,
        ),
        context=SimpleNamespace(
            get_provider_by_id=lambda pid: provider if pid == "chat1" else None,
            llm_generate=fake_llm_generate,
        ),
    )
    client = LLMDecisionClient(host)
    result = await client.evaluate(state=s1, questions=q1, umo="room:1")
    assert result is not None and result["action"]["choice"] == "reply"
    assert len(captured_calls) == 1
    assert captured_calls[0]["system_prompt"] == sys1
    assert captured_calls[0]["prompt"] == usr1


# ---- CD-6: Skip redundant <image_caption> VLM refinement ----


@pytest.mark.asyncio
async def test_refine_host_caption_skips_when_usable_and_refines_when_forced(monkeypatch):
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="精修识图结果")))
    context = SimpleNamespace(
        get_config=lambda **kwargs: {"provider_settings": {"default_image_caption_provider_id": "vision"}},
        get_provider_by_id=lambda pid: provider if pid == "vision" else None,
    )
    monkeypatch.setattr(vision, "collect_media_urls", AsyncMock(return_value=(["img-1"], [])))
    usable_req = SimpleNamespace(
        extra_user_content_parts=[
            SimpleNamespace(text="<image_caption>一位穿着深蓝色校服的长发女生站在教室窗边微笑</image_caption>")
        ]
    )
    event = SimpleNamespace(unified_msg_origin="room")

    # Default: skip redundant second VLM call
    skipped = await vision.refine_host_caption(context, event, usable_req)
    assert skipped == ""
    provider.text_chat.assert_not_awaited()

    # Opt-in forced refinement: invokes configured VLM provider
    forced = await vision.refine_host_caption(context, event, usable_req, force=True)
    assert forced == "精修识图结果"
    provider.text_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_llm_request_non_persona_mode_skips_redundant_caption_refine(monkeypatch):
    from dataclasses import replace

    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin

    refine_calls = []

    async def fake_refine(context, event, request, *, force=False, **_kwargs):
        refine_calls.append(force)
        return "精修补充说明" if force else ""

    monkeypatch.setattr(vision, "refine_host_caption", fake_refine)
    plugin = _plugin({"pipeline_mode": "filter", "takeover_all": True})
    plugin.decision_mode = "legacy"
    try:
        event = MockEvent("看看这张图", group_id="g1", message_id="m1")
        usable_part = SimpleNamespace(text="<image_caption>画面中是一位穿着深蓝色校服的长发少女站在窗边</image_caption>")

        # Default force_refine_host_caption=False: skips refine_host_caption completely!
        req1 = SimpleNamespace(prompt="看看这张图", extra_user_content_parts=[usable_part], image_urls=[])
        await plugin.on_llm_request(event, req1)
        assert refine_calls == []
        injected1 = req1.prompt + "\n".join(getattr(part, "text", "") for part in req1.extra_user_content_parts)
        assert vision.MAIN_VISION_HINT in injected1

        # Opt-in force_refine_host_caption=True: invokes refine_host_caption(force=True)
        plugin._runtime_config = replace(plugin._runtime_config, force_refine_host_caption=True)
        req2 = SimpleNamespace(prompt="看看这张图", extra_user_content_parts=[usable_part], image_urls=[])
        await plugin.on_llm_request(event, req2)
        assert refine_calls == [True]
        injected2 = req2.prompt + "\n".join(getattr(part, "text", "") for part in req2.extra_user_content_parts)
        assert "精修补充说明" in injected2
    finally:
        await plugin.terminate()
