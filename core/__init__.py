"""Core module for AstrBot Chat Dynamics plugin."""

from __future__ import annotations

from .addressivity import AddressivityLevel, AddressivityRouter, AddressivityScore
from .arbiter import ArbitrationResult, InterventionArbiter
from .debounce import DebounceBuffer, DebounceItem, DebounceResult
from .embedding_adapter import EmbeddingAdapter
from .graph import ConversationDAG, ConversationNode
from .reactions import ReactionDecision, ReactionPolicy
from .semantics import SemanticMatch, hashed_embedding, lexical_tokens, semantic_match
from .incompleteness import IncompletenessDetector, IncompletenessResult
from .pacer import PacingShaper
from .platform_bridge import ParsedEvent, SendResult, parse_group_event, send_plain
from .poke import PokeReplyDecision, PokeReplyPolicy
from .session_runtime import FollowupBatch, PendingTurn, SessionRegistry, SessionRuntime
from .style_shaper import StyleShaper
from .telemetrics import RoomTelemetrics, TelemetricsTracker
from .time_service import SystemClock, TimeService, VirtualClock
from .vibe_analyzer import AtmosphereSnapshot, GroupChatMode, VibeAnalyzer

__all__ = [
    "TimeService",
    "SystemClock",
    "VirtualClock",
    "IncompletenessDetector",
    "IncompletenessResult",
    "DebounceBuffer",
    "DebounceItem",
    "DebounceResult",
    "ConversationNode",
    "ConversationDAG",
    "lexical_tokens",
    "hashed_embedding",
    "semantic_match",
    "SemanticMatch",
    "EmbeddingAdapter",
    "ReactionPolicy",
    "ReactionDecision",
    "AddressivityLevel",
    "AddressivityScore",
    "AddressivityRouter",
    "RoomTelemetrics",
    "TelemetricsTracker",
    "GroupChatMode",
    "AtmosphereSnapshot",
    "VibeAnalyzer",
    "ArbitrationResult",
    "InterventionArbiter",
    "StyleShaper",
    "PacingShaper",
    "ParsedEvent",
    "SendResult",
    "parse_group_event",
    "send_plain",
    "PokeReplyDecision",
    "PokeReplyPolicy",
    "PendingTurn",
    "FollowupBatch",
    "SessionRuntime",
    "SessionRegistry",
]
