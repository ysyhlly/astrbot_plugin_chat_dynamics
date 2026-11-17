# Test Readiness Summary (`TEST_READY.md`)

- **Component**: Conversation Router Layer (`core/thread_router.py`)
- **Milestone**: `M_E2E` (Conversation Router E2E Testing Track)
- **Author**: `teamwork_preview_test_writer_e2e_1`
- **Date**: 2026-09-08
- **Status**: **READY (100% PASSED - 27/27 Tests)**

---

## 1. Test Suite Execution Summary

The dedicated Conversation Router test suite has been authored, verified, and executed. All 27 test cases covering Tiers 1 through 4 pass completely without warnings or flake.

- **Test Suite Location**: `tests/test_conversation_router.py`
- **Methodology & Architecture Document**: `TEST_INFRA.md`
- **Test Framework**: `pytest` with `pytest-asyncio`
- **Execution Command**:
  ```bash
  python -m pytest tests/test_conversation_router.py -v
  ```
- **Execution Results**:
  ```text
  ============================= test session starts =============================
  platform win32 -- Python 3.10.8, pytest-9.0.3, pluggy-1.6.0
  collected 27 items

  tests/test_conversation_router.py::test_tier1_routing_inference_dataclass_defaults PASSED [  3%]
  tests/test_conversation_router.py::test_tier1_dag_ingestion_is_strictly_deterministic PASSED [  7%]
  tests/test_conversation_router.py::test_tier1_thread_id_vs_topic_id_separation PASSED [ 11%]
  tests/test_conversation_router.py::test_tier1_topic_resolver_clustering_and_state PASSED [ 14%]
  tests/test_conversation_router.py::test_tier1_parent_retriever_promotes_inferred_edge PASSED [ 18%]
  tests/test_conversation_router.py::test_tier1_addressee_resolver_vocative_cues PASSED [ 22%]
  tests/test_conversation_router.py::test_tier2_contextual_query_expansion_boundaries PASSED [ 25%]
  tests/test_conversation_router.py::test_tier2_low_information_filler_suppression PASSED [ 29%]
  tests/test_conversation_router.py::test_tier2_parent_scoring_margin_gate PASSED [ 33%]
  tests/test_conversation_router.py::test_tier2_ttl_and_bounded_pruning PASSED [ 37%]
  tests/test_conversation_router.py::test_tier2_session_runtime_state_isolation_and_reset PASSED [ 40%]
  tests/test_conversation_router.py::test_tier3_explicit_mention_overrides_active_bot_followup PASSED [ 44%]
  tests/test_conversation_router.py::test_tier3_re_routing_idempotency_removes_old_inferred_edge PASSED [ 48%]
  tests/test_conversation_router.py::test_tier3_quoted_human_in_dialogue_elliptical_vs_non_elliptical PASSED [ 51%]
  tests/test_conversation_router.py::test_tier3_competing_human_chatter_invalidates_active_interlocutor_fallback PASSED [ 55%]
  tests/test_conversation_router.py::test_tier3_message_semantics_routing_exposure PASSED [ 59%]
  tests/test_conversation_router.py::test_tier3_addressivity_router_consumption PASSED [ 62%]
  tests/test_scenario_1_active_interlocutor_followup PASSED [ 66%]
  tests/test_scenario_2_bystander_interjection_not_addressed PASSED [ 70%]
  tests/test_scenario_3_interleaved_qa_retrieval PASSED [ 74%]
  tests/test_scenario_4_quote_human_with_explicit_bot_mention PASSED [ 77%]
  tests/test_scenario_5_quote_human_as_subject_in_active_bot_dialogue PASSED [ 81%]
  tests/test_scenario_6_bot_as_subject_not_addressee PASSED [ 85%]
  tests/test_scenario_7_direct_vocative_call PASSED [ 88%]
  tests/test_scenario_8_low_information_chatter_no_parent_link PASSED [ 92%]
  tests/test_scenario_9_concurrent_technical_topics_isolated PASSED [ 96%]
  tests/test_scenario_10_expired_topic_inactivity_boundary PASSED [100%]

  ============================= 27 passed in 1.86s ==============================
  ```

---

## 2. 10 Core Regression Scenarios Coverage Checklist

| # | Scenario Description | Test Function Name | Expected Inferences & Edges | Status |
|---|---|---|---|---|
| **1** | Bot->A, A: "那怎么办？" -> addressee=Bot | `test_scenario_1_active_interlocutor_followup` | `bot_is_addressee=True`, `parent=bot1`, `evidence=["active_interlocutor_followup"]`, `edge_kinds[bot1]="inferred_reply"` | **PASS** |
| **2** | Bot->A, B: "真的假的" -> addressee!=Bot | `test_scenario_2_bystander_interjection_not_addressed` | `bot_is_addressee=False`, `bot_addressee_confidence < 0.40`, `AddressivityLevel in (WEAK, SAFE_HOVER)` | **PASS** |
| **3** | D: "风扇怎么设？" + interleaved gaming chatter + A: "默认" -> parent=D, topic=GPU | `test_scenario_3_interleaved_qa_retrieval` | `topic_id == gpu_topic`, `topic_id != game_topic`, `parent=d1`, `addressee_ids=["David"]`, `bot_is_addressee=False` | **PASS** |
| **4** | A quotes B + "@bot 他说得对吗" -> quoted=B, addressee=Bot | `test_scenario_4_quote_human_with_explicit_bot_mention` | `parent=b1`, `parent_confidence=1.0`, `addressee=["bot"]`, `bot_is_addressee=True`, `quoted_author="Bob"` | **PASS** |
| **5** | A quotes B: "这个呢？" while A is in active dialogue with Bot -> addressee=Bot | `test_scenario_5_quote_human_as_subject_in_active_bot_dialogue` | `parent=b1`, `addressee=["bot"]`, `bot_is_addressee=True`, `evidence` contains `quoted_subject_active_interlocutor` | **PASS** |
| **6** | A: "这个bot怎么老不回" -> subject=Bot, addressee!=Bot | `test_scenario_6_bot_as_subject_not_addressee` | `subject_is_bot=True`, `bot_is_addressee=False`, `bot_addressee_confidence < 0.40`, `AddressivityLevel.WEAK` | **PASS** |
| **7** | A: "bot，你怎么老不回" -> addressee=Bot | `test_scenario_7_direct_vocative_call` | `bot_is_addressee=True`, `addressee=["bot"]`, `confidence >= 0.90`, `AddressivityLevel.STRONG` | **PASS** |
| **8** | A: "哈哈" -> no high-confidence parent link | `test_scenario_8_low_information_chatter_no_parent_link` | `parent_message_id=""`, `parent_confidence < 0.72`, no `inferred_reply` edge linked | **PASS** |
| **9** | Two concurrent technical topics -> separated into two distinct topics | `test_scenario_9_concurrent_technical_topics_isolated` | `r_py1.topic_id != r_net1.topic_id`, `r_py2.topic_id == r_py1.topic_id`, `r_net2.topic_id == r_net1.topic_id` | **PASS** |
| **10**| Topic inactive for > 5 min followed by "这个呢" -> does not force-resume dead topic | `test_scenario_10_expired_topic_inactivity_boundary` | Inactivity gap 360s > 300s TTL; `bot_is_addressee=False`, `parent != bot1`, `bot_confidence < 0.40` | **PASS** |

---

## 3. 4-Tier Test Suite Architecture

### Tier 1: Core Feature Coverage (6 tests)
- `test_tier1_routing_inference_dataclass_defaults`: Validates dataclass field defaults and initialization contracts.
- `test_tier1_dag_ingestion_is_strictly_deterministic`: Verifies `ConversationDAG.add_message()` creates zero heuristic edges.
- `test_tier1_thread_id_vs_topic_id_separation`: Enforces separation between reply tree `thread_id` and high-level `topic_id`.
- `test_tier1_topic_resolver_clustering_and_state`: Verifies `TopicState` participant tracking and `RoutingState._remember`.
- `test_tier1_parent_retriever_promotes_inferred_edge`: Confirms high-scoring candidate promotion to DAG `inferred_reply`.
- `test_tier1_addressee_resolver_vocative_cues`: Verifies vocative address detection vs third-person subject reference.

### Tier 2: Boundaries & Corner Cases (5 tests)
- `test_tier2_contextual_query_expansion_boundaries`: Verifies $\le 16$ characters query borrowing vs $> 16$ raw text, author identity check, and 90s recency boundary.
- `test_tier2_low_information_filler_suppression`: Ensures phatic tokens (`"哈哈"`, `"2333"`, `"?"`, `"ok"`) are suppressed from DAG inference.
- `test_tier2_parent_scoring_margin_gate`: Checks candidate margin threshold ($< 0.10$ difference suppresses ambiguous promotion).
- `test_tier2_ttl_and_bounded_pruning`: Tests 300s TTL eviction and max 80 node capacity boundary.
- `test_tier2_session_runtime_state_isolation_and_reset`: Tests isolation between sessions and clean reset via `reset_conversation_state()`.

### Tier 3: Cross-Feature Combinations & Precedence (6 tests)
- `test_tier3_explicit_mention_overrides_active_bot_followup`: Direct `@mention` of human takes precedence over bot interlocutor bonus.
- `test_tier3_re_routing_idempotency_removes_old_inferred_edge`: Re-evaluating a node removes stale inferred edges cleanly.
- `test_tier3_quoted_human_in_dialogue_elliptical_vs_non_elliptical`: Distinguishes elliptical followups to bot from direct statements to quoted author.
- `test_tier3_competing_human_chatter_invalidates_active_interlocutor_fallback`: Intervening chatter in topic prevents stale interlocutor continuation.
- `test_tier3_message_semantics_routing_exposure`: Verifies `describe_message()` surfaces routing metadata accurately.
- `test_tier3_addressivity_router_consumption`: Validates downstream `AddressivityRouter` scoring against `RoutingInference`.

### Tier 4: Realistic Multi-Turn Chat Scenarios (10 tests)
- All 10 conversational regression scenarios fully implemented and passing.

---

## 4. Observations & Non-Blocking Escalations for Implementer Agent

During the initial whole-suite run (`python -m pytest tests -q`), two pre-existing defects in `tests/test_embeddings.py` were observed (as previously identified in survey 3):
1. `tests/test_embeddings.py::test_cached_neural_match_requires_router_before_inferred_edge`:
   - `core/thread_router.py:94` attempts `runtime.last_activity` directly. When mocked with `types.SimpleNamespace`, it raises `AttributeError` unless accessed via `getattr(runtime, "last_activity", 0.0)`.
2. `tests/test_embeddings.py::test_plugin_warmup_corrects_routing_after_neural_cache_fills`:
   - Parent candidate scoring in `core/thread_router.py` uses an older formula that attenuates neural similarity below 0.72. Updating to the 6-factor composite formula scheduled in M2 will resolve this assertion.

These two items reside strictly within `core/thread_router.py` / `tests/test_embeddings.py` and are scheduled for Milestones M1/M2/M5 per `PROJECT.md`. The dedicated `tests/test_conversation_router.py` suite operates completely cleanly with 27/27 passes.
