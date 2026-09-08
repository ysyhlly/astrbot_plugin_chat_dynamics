# Conversation Router Test Infrastructure & Methodology (`TEST_INFRA.md`)

- **Project**: AstrBot Chat Dynamics Plugin (`astrbot_plugin_chat_dynamics`)
- **Layer**: Short-Term Group Chat Conversation Router (`core/thread_router.py`)
- **Target Specification**: `ORIGINAL_REQUEST.md` (Section `## 2026-09-08T05:49:12Z`), `PROJECT.md`
- **Owner**: `teamwork_preview_test_writer_e2e_1`
- **Test File**: `tests/test_conversation_router.py`

---

## 1. Test Philosophy & Principles

The Conversation Router operates as an intermediate conversational topology inference layer between raw message ingestion (`ConversationDAG`) and downstream decision/addressivity components (`AddressivityRouter`, `PersonaEngine`). 

Our testing philosophy is guided by five foundational principles:

1. **Topology Decoupling**:
   - Ingestion into `ConversationDAG` is strictly deterministic. `ConversationDAG.add_message()` must never create heuristic or semantic edges.
   - `node.thread_id` strictly tracks explicit reply trees; high-level conversational topics are tracked in `node.metadata["topic_id"]`.
   - All heuristic/semantic links (`inferred_reply`) are solely proposed and evaluated by `ThreadRouter`.

2. **Addressee vs. Subject Disambiguation**:
   - Talking *about* the bot in the third person (e.g., `"这个bot怎么老不回"`) denotes the bot as a **subject**, not an **addressee**.
   - Direct vocative address (e.g., `"bot，你怎么老不回"`) or explicit `@bot` establishes the bot as an **addressee**.
   - Tests assert strict distinction between `subject_is_bot` and `bot_is_addressee`.

3. **Multi-Factor Scoring & Promotion Margins**:
   - Parent and topic retrieval rely on multi-factor composite scores (semantic similarity, QA fit, recency, participant continuity).
   - Inferred edges are only promoted to the DAG if composite score meets the threshold ($\ge 0.72$) and exhibits a clear margin over competing candidates ($\ge 0.08$ - $0.10$). Ambiguous candidates must NOT be promoted.

4. **Temporal Bounds & Session Isolation**:
   - Conversation state is bounded by a strict sliding window (TTL 300s, max 80 nodes). Inactive topics must expire and cannot be revived by ambiguous elliptical turns.
   - State must remain isolated per session runtime without cross-session contamination.

5. **Test Integrity & Determinism**:
   - Tests are production-grade, self-contained, and reproducible.
   - Semantic similarity doubles (such as keyword matching or calibrated scoring or deterministic embeddings) provide predictable evidence without depending on external network services, while exercising all router routing and scoring branches.

---

## 2. 4-Tier Testing Methodology

The test suite is structured across 4 progressive tiers ensuring comprehensive coverage from isolated units to realistic multi-turn chat dynamics:

```
+-------------------------------------------------------------------------+
| Tier 4: Realistic Multi-Turn Group Chat Scenarios (10 Core Scenarios)   |
|   - Interleaved multi-topic chatter (GPU vs Gaming)                    |
|   - Quoting 3rd party advice to Bot during active inquiry               |
|   - Active interlocutor followup vs bystander interjections            |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
| Tier 3: Cross-Feature Combinations & Precedence Hierarchy               |
|   - Explicit @mention overriding active interlocutor bot followup       |
|   - In-dialogue quote vs bystander quote                                |
|   - Re-routing idempotency and edge replacement                         |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
| Tier 2: Boundaries, Edge Cases & Temporal Decay                         |
|   - TTL expiration (> 300s) enforcing dead topic drop                   |
|   - Low-information filler suppression ("哈哈", "2333")                |
|   - Elliptical contextual query expansion (<= 16 chars author borrowing)|
|   - Candidate scoring margin gates (ambiguity rejection)                |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
| Tier 1: Core Feature Unit Coverage                                      |
|   - RoutingInference field contracts and defaults                       |
|   - TopicResolver & RoutingState lifecycle                              |
|   - ParentRetriever candidate scoring                                   |
|   - AddresseeResolver vocative vs subject detection                     |
|   - SessionRuntime isolation and state reset                            |
+-------------------------------------------------------------------------+
```

### Tier 1: Core Feature Unit Coverage
- Tests unit-level interfaces: `RoutingInference` attributes, `TopicState`, `RoutingState.prune()`, `build_contextual_query()`.
- Verifies deterministic DAG edge properties: explicit `reply_to_id`, `@mentions`, and fragment preservation.

### Tier 2: Boundaries, Edge Cases & Temporal Decay
- Tests boundary conditions:
  - Temporal TTL cutoff at 300s; candidate retrieval cutoff at 180s.
  - Text length boundaries for contextual expansion ($\le 16$ characters borrows prior author turn; $> 16$ uses raw text).
  - Short/phatic low-information turns (`"哈哈"`, `"?"`, `"2333"`) rejected from establishing inferred links.
  - Threshold margin boundaries ($< 0.08$ margin marks inference as ambiguous).

### Tier 3: Cross-Feature Combinations & Precedence Hierarchy
- Tests interaction between orthogonal routing signals:
  - Direct `@mention` of human user `B` while in active conversation with Bot $\rightarrow$ `@mention` wins, `bot_is_addressee=False`.
  - Re-routing a previously routed node removes stale inferred edges while preserving deterministic platform edges.
  - Concurrent distinct technical topics maintaining independent topic centroids.

### Tier 4: Realistic Multi-Turn Group Chat Scenarios
- End-to-end multi-turn sequences verifying the 10 core conversational regression scenarios specified in `ORIGINAL_REQUEST.md`.
- Simulates realistic multi-party dynamics: interleaved chatters, out-of-turn interjections, quoted subject inquiries, and vocative addressing.

---

## 3. Feature Inventory & Test Mapping

| Feature ID | Feature Description | Test Coverage Function in `tests/test_conversation_router.py` | Tier |
|---|---|---|---|
| **R1.1** | Decouple Heuristic Semantic Edges from DAG | `test_tier1_dag_ingestion_is_strictly_deterministic` | Tier 1 |
| **R1.2** | Deterministic DAG Ingestion | `test_tier1_dag_explicit_edges_preserved` | Tier 1 |
| **R1.3** | Thread ID vs Topic ID Separation | `test_tier1_thread_id_vs_topic_id_separation` | Tier 1 |
| **R2.1** | RoutingInference Dataclass Structure | `test_tier1_routing_inference_dataclass_defaults` | Tier 1 |
| **R2.2** | TopicResolver Composite Scoring & Clustering | `test_tier1_topic_resolver_clustering_and_state` | Tier 1 |
| **R2.3** | ParentRetriever Reranking & Inferred Edge Linking | `test_tier1_parent_retriever_promotes_inferred_edge` | Tier 1 |
| **R2.4** | AddresseeResolver Precedence Hierarchy | `test_tier1_addressee_resolver_precedence` | Tier 1 |
| **R2.5** | Contextual Query Expansion Boundaries | `test_tier2_contextual_query_expansion_boundaries` | Tier 2 |
| **R2.6** | Low-Information Filter Suppression | `test_tier2_low_information_filler_suppression` | Tier 2 |
| **R3.1** | Session State Isolation & Clean Prune | `test_tier2_session_runtime_state_isolation_and_prune` | Tier 2 |
| **R3.2** | Bot Turn Observation & Interlocutor State | `test_tier3_bot_turn_observation_and_interlocutor_tracking` | Tier 3 |
| **R3.3** | Precedence: Explicit Mention vs Active Bot Followup | `test_tier3_explicit_mention_overrides_active_bot_followup` | Tier 3 |
| **R4.1** | MessageSemantics Routing Fields Exposure | `test_tier3_message_semantics_routing_attributes` | Tier 3 |
| **R4.2** | Addressivity Consumption of Routing Inference | `test_tier3_addressivity_router_consumption` | Tier 3 |
| **R5.2.1** | Scenario 1: Bot->A, A: "那怎么办？" -> addressee=Bot | `test_scenario_1_active_interlocutor_followup` | Tier 4 |
| **R5.2.2** | Scenario 2: Bot->A, B: "真的假的" -> addressee!=Bot | `test_scenario_2_bystander_interjection_not_addressed` | Tier 4 |
| **R5.2.3** | Scenario 3: D: "风扇怎么设？" + interleaved gaming + A: "默认" | `test_scenario_3_interleaved_qa_retrieval` | Tier 4 |
| **R5.2.4** | Scenario 4: A quotes B + "@bot 他说得对吗" -> quoted=B, addressee=Bot | `test_scenario_4_quote_human_with_explicit_bot_mention` | Tier 4 |
| **R5.2.5** | Scenario 5: A quotes B: "这个呢？" while active with Bot -> addressee=Bot | `test_scenario_5_quote_human_as_subject_in_active_bot_dialogue` | Tier 4 |
| **R5.2.6** | Scenario 6: A: "这个bot怎么老不回" -> subject=Bot, addressee!=Bot | `test_scenario_6_bot_as_subject_not_addressee` | Tier 4 |
| **R5.2.7** | Scenario 7: A: "bot，你怎么老不回" -> addressee=Bot | `test_scenario_7_direct_vocative_call` | Tier 4 |
| **R5.2.8** | Scenario 8: A: "哈哈" -> no high-confidence parent link | `test_scenario_8_low_information_chatter_no_parent_link` | Tier 4 |
| **R5.2.9** | Scenario 9: Two concurrent technical topics -> separated | `test_scenario_9_concurrent_technical_topics_isolated` | Tier 4 |
| **R5.2.10**| Scenario 10: Inactive > 5 min followed by "这个呢" -> does not resume | `test_scenario_10_expired_topic_inactivity_boundary` | Tier 4 |

---

## 4. 10 Core Conversational Regression Scenarios Traceability Matrix

| # | Scenario ID | Trigger Utterance & Context | Target Output Assertions | Authoritative Specification |
|---|---|---|---|---|
| 1 | `active_interlocutor` | Bot answers A; A asks `"那怎么办？"` within 60s | `bot_is_addressee == True`, `addressee_ids == ["bot"]`, `parent_message_id == bot_node.msg_id`, `edge_kinds[bot] == "inferred_reply"` | R2, R5.2 #1 |
| 2 | `bystander_interjection` | Bot answers A; bystander B says `"真的假的"` | `bot_is_addressee == False`, `"bot"` not in `addressee_ids`, `addressivity.level in (WEAK, SAFE_HOVER)` | R2, R5.2 #2 |
| 3 | `interleaved_qa` | Alice & David discuss GPU cooling; Charlie & Eric interleave gaming chatter; Alice answers `"我默认的"` | `topic_id == gpu_topic`, `topic_id != game_topic`, `parent_message_id == david_node.msg_id`, `addressee_ids == ["David"]` | R2, R5.2 #3 |
| 4 | `quote_plus_mention` | Alice quotes Bob with text `"@bot 他说得对吗"` | `parent_message_id == bob_node.msg_id`, `addressee_ids == ["bot"]`, `quoted_author_id == "Bob"`, `bot_is_addressee == True` | R4, R5.2 #4 |
| 5 | `quote_as_subject` | Alice in active dialogue with Bot; Alice quotes Bob saying `"这个呢？"` | `parent_message_id == bob_node.msg_id`, `addressee_ids == ["bot"]`, `bot_is_addressee == True`, `evidence` contains `quoted_subject_active_interlocutor` | R2, R5.2 #5 |
| 6 | `bot_as_subject` | Alice states `"这个bot怎么老不回"` in group chat | `subject_is_bot == True`, `bot_is_addressee == False`, `addressivity.level == WEAK` | R2, R5.2 #6 |
| 7 | `direct_vocative` | Alice states `"bot，你怎么老不回"` | `bot_is_addressee == True`, `addressee_ids == ["bot"]`, `bot_addressee_confidence >= 0.90`, `addressivity.level == STRONG` | R2, R5.2 #7 |
| 8 | `low_info_suppression` | Bob chats; Alice replies with filler `"哈哈"` | `parent_message_id == ""`, `parent_confidence < 0.72`, `edge_kinds` has NO `inferred_reply` | R2, R5.2 #8 |
| 9 | `topic_separation` | Interleaved turns on Python asyncio and OpenWrt DNSMasq | `topic_py != topic_net`, turns properly cluster into respective topic IDs | R2, R5.2 #9 |
| 10| `ttl_expiry_drop` | Bot answers A; silence > 300s; A says `"这个呢"` | `bot_is_addressee == False`, `parent_message_id != bot_msg_id`, dead topic pruned from `RoutingState` | R2, R5.2 #10 |

---

## 5. Test Execution Instructions

### 5.1 Environment Prerequisites
- Python 3.10+ (tested on Python 3.10 and 3.12).
- Dependencies: `pytest`, `pytest-asyncio`.
- All AstrBot SDK dependencies are automatically mocked via `tests/conftest.py` if AstrBot is not globally installed.

### 5.2 Executing the Conversation Router Test Suite
To run the dedicated Conversation Router test suite:
```bash
python -m pytest tests/test_conversation_router.py -v
```

To run with detailed assertion diffs and stdout logging:
```bash
python -m pytest tests/test_conversation_router.py -vv -s
```

To run specific scenario tests:
```bash
python -m pytest tests/test_conversation_router.py -k "scenario_1 or scenario_5" -v
```

To run all routing and graph related tests across the repository:
```bash
python -m pytest tests/test_conversation_router.py tests/test_thread_router.py tests/test_graph.py -v
```

---

## 6. Verification and Output Standards

1. **Deterministic Double Standard**:
   - In unit/regression scenarios requiring calibrated semantic similarities, explicit semantic doubles or deterministic `EmbeddingAdapter` mocks are injected to ensure 0 flaky failures while exercising real router logic.
2. **Explicit Assertion Derivation**:
   - Every assertion derives directly from requirements in `ORIGINAL_REQUEST.md` (R1-R5).
   - Expected outputs match documented system contracts (e.g. edge kinds, confidence scores, evidence tags, topic IDs).
3. **No Facade Tests**:
   - All tests instantiate live `ConversationDAG`, `SessionRuntime`, `ThreadRouter`, and `AddressivityRouter` objects. Mocking is restricted to external network embeddings.
