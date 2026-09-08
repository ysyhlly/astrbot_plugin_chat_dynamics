# Project: Conversation Router Layer

## Architecture
The Conversation Router acts as a short-term group chat conversational topology layer (`core/thread_router.py`) positioned between the message ingestion/DAG storage layer (`core/graph.py`) and downstream decision engines (`core/addressivity.py`, `core/persona_engine.py`, `core/arbiter.py`).

### Data Flow
1. **Ingestion (`ConversationDAG.add_message()`)**:
   - Ingests message node into the DAG.
   - Deterministically creates platform-level edges: explicit replies (`reply_to_id`), mentions (`@mentions`), and fragment attachments.
   - Preserves `node.thread_id` strictly for deterministic reply trees. Heuristic/semantic edges are prohibited at ingestion.
2. **Conversation Routing (`ThreadRouter.route()`)**:
   - Invokes `TopicResolver`: matches incoming message against active topic centroids/exemplars using 5-factor composite scoring (semantic, recency, participant, lexical, lineage). Handles short messages (<=12 chars) with contextual expansion, enforces TTL (300s) and join/ambiguity thresholds.
   - Invokes `ParentRetriever`: retrieves candidates within active/ambiguous topics (180s, max 60-80), computes 6-factor reranking score (semantic, topic, qa_fit, temporal, turn, participant). Promotes candidate to DAG edge (`inferred_reply`) if score >= 0.72 and margin >= 0.08.
   - Invokes `AddresseeResolver`: resolves target addressees using strict precedence hierarchy (@mention > reply > inferred parent > active interlocutor > turn-taking > unknown) and distinguishes bot subject reference from bot vocative address using direct cues.
   - Outputs `RoutingInference` and stores high-level topic state in `node.metadata["topic_id"]` and `node.metadata["routing"]`.
3. **Session State Isolation (`SessionRuntime`)**:
   - Encapsulates `TopicState` and `RoutingState` inside `SessionRuntime` per group chat session.
   - Tracks bot-emitted turns to identify active bot topic participation.
   - Gates dialogue continuation bonuses on topic coherence or parent continuity.
4. **Downstream Semantics & Addressivity (`MessageSemantics` & `AddressivityRouter`)**:
   - `MessageSemantics` surfaces `topic_id`, `inferred_parent_id`, `bot_is_subject`, `bot_is_addressee`.
   - `AddressivityRouter` consumes `RoutingInference` directly: respects direct addressivity, applies -0.15 human quote penalty (instead of 0.10 hard ceiling), and rejects third-person bot discussion as direct address.
   - `PersonaEngine` passes routing inference to LLM turn decision context with explicit instructions on unknown addressees and subject vs addressee.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | R1.1 Remove Heuristic Semantic Edges | Remove _semantic_parent() and maybe_link_semantic() from ConversationDAG | M1 | Survey 1 |
| 2 | R1.2 Deterministic DAG Ingestion | Ensure ConversationDAG.add_message() only creates deterministic edges | M1 | Survey 1 |
| 3 | R1.3 Thread ID vs Topic ID Separation | Reserve node.thread_id for reply trees; store topic state in node.metadata["topic_id"] | M1 | Survey 1 |
| 4 | R2.1 RoutingInference Dataclass | Dataclass capturing topic, parent, addressee, subject, and explicit flags | M2 | Survey 2 |
| 5 | R2.2 TopicResolver | 5-factor composite scoring, contextual query expansion, TTL 300s, thresholds 0.58/0.48 | M2 | Survey 2 |
| 6 | R2.3 ParentRetriever | 6-factor reranking, candidate scoping (180s, max 60-80), DAG promotion (>=0.72, margin 0.08) | M2 | Survey 2 |
| 7 | R2.4 AddresseeResolver | Precedence hierarchy, bot vocative cues vs subject discussion separation | M2 | Survey 2 |
| 8 | R2.5 Embedding Fast-Path / Fallback | Fast path sync, ambiguous path 0.5s timeout with graceful hashed fallback | M2 | Survey 1 |
| 9 | R2.6 Structured Router Logging | Structured [Router] diagnostic logging blocks for every evaluated message | M2 | Survey 3 |
| 10 | R3.1 Session State Isolation | Move TopicState and RoutingState into core/session_runtime.py | M3 | Survey 2 |
| 11 | R3.2 Bot Emission Tracking | Track bot messages in router to maintain active bot topic state | M3 | Survey 2 |
| 12 | R3.3 Topic-Coherent Interlocutor Bonus | Gate dialogue continuation bonuses on topic coherence or parent continuity | M3 | Survey 2 |
| 13 | R4.1 MessageSemantics Routing Fields | Expose topic_id, inferred_parent_id, bot_is_subject, bot_is_addressee aliases | M4 | Survey 2 |
| 14 | R4.2 Addressivity Routing Consumption | Consume RoutingInference, -0.15 quote penalty, subject vs addressee handling | M4 | Survey 2 |
| 15 | R4.3 Persona Engine Prompt Guidance | Update decision instructions for unknown addressees and subject vs addressee | M4 | Survey 2 |
| 16 | R5.1 Configuration Parameters | Add 4 missing router parameters to _conf_schema.json and core/config.py | M4 | Survey 3 |
| 17 | R5.2 10 Core Regression Scenarios Test Suite | Implement tests/test_conversation_router.py covering all 10 scenarios | M_E2E | Survey 3 |
| 18 | R5.3 Full Test Suite Regression Pass | Fix test_embeddings.py, verify 100% test pass and Tier 5 coverage hardening | M5 | Survey 1,3 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M_E2E | E2E Testing Track | Test infra (TEST_INFRA.md) & 10 core scenarios in tests/test_conversation_router.py -> TEST_READY.md | none | DONE |
| M1 | DAG Decoupling | Decouple early semantic linking in core/graph.py, clean add_message(), separate thread_id and topic_id | none | DONE |
| M2 | Core ThreadRouter | Implement core/thread_router.py (RoutingInference, TopicResolver, ParentRetriever, AddresseeResolver, [Router] logs, timeout fallback) | M1 | DONE |
| M3 | Session Runtime State | Integrate TopicState & RoutingState in core/session_runtime.py, bot tracking, topic-coherent continuation bonus | M2 | DONE |
| M4 | Downstream Semantics & Config | MessageSemantics aliases, addressivity.py quote penalty fix, persona_engine prompt guidance, config schema & loader | M2, M3 | DONE |
| M5 | Final E2E Pass & Coverage Hardening | Pass 100% E2E test suite (Tiers 1-4) + Tier 5 adversarial testing & coverage audit | M_E2E, M4 | DONE |

## Interface Contracts

### ConversationDAG (core/graph.py)
- `add_message(msg_id, user_id, content, timestamp, reply_to_id=None, mentions=None, ...) -> ConversationNode`:
  - Deterministic only. Creates edges for `reply_to_id`, `@mentions`, and fragments.
  - Returns `node` with `node.thread_id` reflecting strictly reply tree.
- `link_inferred_reply(child_id: str, parent_id: str, confidence: float, reason: str) -> bool`:
  - Explicit API for router to promote high-confidence inferred parent to DAG edge with `kind="inferred_reply"`.

### ThreadRouter (core/thread_router.py)
- `RoutingInference`:
  - Fields: `topic_id: str`, `topic_confidence: float`, `parent_message_id: str`, `parent_confidence: float`, `addressee_ids: List[str]`, `addressee_confidence: float`, `subject_user_ids: List[str]`, `subject_is_bot: bool`, `bot_is_addressee: bool`, `bot_addressee_confidence: float`, `explicit_reply: bool`, `explicit_mention: bool`, `ambiguous: bool`, `evidence: List[str]`.
- `ThreadRouter.route(node: ConversationNode, dag: ConversationDAG, runtime: SessionRuntime, timeout: float = 0.5) -> RoutingInference`

### SessionRuntime (core/session_runtime.py)
- `TopicState`:
  - `topic_id: str`, `label: str`, `created_at: float`, `last_activity: float`, `message_count: int`, `participant_ids: Set[str]`, `exemplar_messages: List[Tuple[str, float]]`, `centroid_vector: Optional[List[float]]`.
- `RoutingState`:
  - `active_topics: Dict[str, TopicState]`, `last_bot_topic_id: Optional[str]`, `last_topic_id: Optional[str]`, `clear()`.

### Downstream Pipeline
- `MessageSemantics`: exposes `topic_id`, `inferred_parent_id`, `bot_is_subject`, `bot_is_addressee`, `routing_ambiguous`, `routing_evidence`.
- `AddressivityRouter.score(node, dag, runtime, semantics)`: uses `bot_addressee_confidence`, applies -0.15 human quote penalty, avoids false triggers when bot is subject only.

## Code Layout
- `core/graph.py`: ConversationDAG, ConversationNode, deterministic edge management.
- `core/thread_router.py`: RoutingInference, TopicResolver, ParentRetriever, AddresseeResolver, ThreadRouter.
- `core/session_runtime.py`: SessionRuntime, TopicState, RoutingState.
- `core/message_semantics.py`: MessageSemantics, describe_message().
- `core/addressivity.py`: AddressivityRouter, AddressivityScore.
- `core/persona_engine.py`: PersonaEngine, turn continuation logic, prompt formatting.
- `core/turn_decision.py`: Prompt instructions and decision schemas.
- `core/config.py`: RuntimeConfig, parse_runtime_config().
- `_conf_schema.json`: Plugin configuration schema.
- `tests/test_conversation_router.py`: Dedicated 10 core regression scenarios test suite.
