# Memory feedback projection (rc15 staging)

Chat Dynamics rc15 consumes LivingMemory's public `get_evidence_view` API during
the asynchronous narrative preparation phase. It freezes one caller-selected
`purpose="reply"` or `purpose="history_quote"` view for Jev and the owned reply. Legacy LivingMemory recall summaries are
removed from the managed bundle when the evidence view is available; source
atoms, current relationship material, and static Worldbook material remain in
their own slots.

The `card_version` sent to LivingMemory and SelfLearning is SHA-256 over the
canonical UTF-8 JSON object
`{schema: "effective_core_v1", card_id, core_prompt, group_rules}` using sorted
keys, compact separators, and unescaped Unicode. Dynamic Worldbook matches,
topics, and update timestamps do not change this value. The Worldbook turn
fingerprint is kept separately for turn freshness.

For host history, CD uses the current ConversationManager conversation ID,
SHA-256 of the exact serialized history snapshot, absolute list positions,
text hashes, and a SHA-256 prefix anchor over all preceding canonical host
rows. When the host Conversation exposes a positive `created_at`, CD also sends
an opaque history-epoch hash derived from that trusted creation field and CID.
Missing epochs remain absent; old unanchored material is not assigned a guessed
generation. A history row is sent to the evidence API only when it is a plain-text
`user` or `assistant` row with no tool/protocol fields. CD does not invent an
event ID, author UID, event time, or delivery state for these rows. The provider
projection is applied only if the fixed request boundary's complete role/content
sequence still matches the captured host snapshot. Unknown, multimodal, tool,
and incomplete rounds remain intact. If a mapped old assistant round has no
verified atom, its old wording is omitted; pending continuity uses only explicit
candidate spans and remains labelled unverified. Current user text is not part
of this pre-request projection. Captured host rows and quote spans stay
request-local and are redacted from CD's persisted bundle archive; the archive
keeps snapshot fingerprints, epochs, and counts.

CD selects `history_quote` only when the actual current user query explicitly
asks for exact original wording, or a trusted non-model caller selects that
route. Other requests use `reply`. Quote mode may include a full, hash-matched
old assistant row as labelled quote material; it does not change tool
authorization or turn quoted content into an execution receipt. Normal replies
use source-backed atoms or complete, labelled pending spans.

Jev and reply share the same frozen evidence slots. Current evidence and
commitments/boundaries are whole-atom priority slots; recent and historical
claims are optional whole atoms omitted first. Duplicate evidence atoms are
excluded from `context_items`. Renderer diagnostics report slot counts and mark
when required atoms alone exceed the soft budget; the renderer never truncates
a claim's negation, conditions, or deadline.

The public native-hook callback is
`ChatDynamicsPlugin.prepare_memory_request(event, req, materials=None, purpose=None)`. It
returns a plain receipt with `provider`, `status`, fixed `purpose`, four-key
Worldbook scope, effective-core `card_version`, conversation/snapshot identity,
diagnostics, and the evidence view. It is idempotent per request. It never
mutates `req.contexts`; a valid request is `prepared` and reports
`native_history_covered=false` until the runner wrapper is installed. CD-owned
requests return `status="skipped"` because their frozen bundle is projected by
the CD provider wrapper.

On AstrBot 4.28.2, `build_main_agent(apply_reset=False)` returns a deferred reset
coroutine; the native pipeline calls `OnLLMRequestEvent` and then awaits that
coroutine, which materializes `req.contexts` and appends `req.prompt` to the real
runner. CD installs a reversible wrapper around the two loaded
`build_main_agent` function aliases. It prepares the evidence view before the
native hook and delegates reset; after reset, it captures the final current-user
message identity once and wraps the actual primary, fallback, and streaming
providers. History views transform only provider argument copies, so AstrBot's
runner messages and saved conversation retain the original host text. Exact
snapshot matches enable evidence projection; unmatched snapshots conservatively
omit simple old assistant wording and report degraded coverage. Unknown, tool,
multimodal, and no-save protocol rounds remain intact. On plugin termination the
aliases restore only if they still point to CD's own wrappers; retired wrappers
pass through to the original builder. The request-local receipt reports
`awaiting_provider_call`, then `projected`, `partial`, or `degraded`; wrapper
installation does not claim full coverage when protocol rounds remain.

The history identity matcher permits only provable leading `_no_save` persona
examples and leading system messages before the captured persisted history. It
preserves these examples at their original provider positions and excludes them
from historical evidence projection. A protected or unprotected extra message
after persisted history begins makes the mapping ambiguous and prevents exact
projection; it is never skipped to force a match. The current SDK capture covers
four begin-dialog rows, including a synthetic signed-think part, plus old
user/assistant history and the captured current user across primary, fallback,
and stream provider calls.

When the role describes a commitment from an evidence atom, the reply guidance
preserves its original conditions and deadline meaning. Persona pacing cannot
reinterpret or extend the agreed time. Without a life record or system receipt,
the character cannot claim it has started, is wrapping up, or has completed the
work, and cannot invent a reason for delay. It can say progress is unconfirmed
and express present willingness; a plan, execution, and completion remain
separate claims.

For a question about an old statement, the reply separates the checkable quote
from the character's current attitude. An unknown past motive stays unknown;
the character does not invent that it was being stubborn or lazy. It may explain
or correct its present meaning while acknowledging why the old words matter to
the speaker. It does not blame the speaker for asking or dismiss the concern,
while remaining free to disagree or state a boundary. The four-arm replay
reported deadline drift, invented in-progress claims, and invented past motives;
these rules address those observed failures without prescribing a fixed apology.

Native companion hooks must discover the currently activated CD instance on
each call and use only receipts whose provider is exactly
`astrbot_plugin_chat_dynamics`; `bypass` or `skipped` is not evidence that legacy
summaries are safe to inject. Typed memory summaries may use a valid `prepared`
receipt, but must not inject a legacy raw summary.

CD synchronously rechecks LivingMemory's public `peek_evidence_revision(scope)`
before each provider call, tool execution, and native/owned send. A changed or
unavailable revision invalidates the frozen generation and prevents the
mandatory-reply fallback from reviving it. The historical projection is
contextual evidence, not a delivery receipt. CD does not emit a
`tool.execution_receipt` for host-history rows; future delivery binding requires
an explicit, validated `eventMetadata.history_binding` containing the complete
host turn and receipt segment.

Tests in `tests/test_memory_feedback_projection.py` use synthetic host history
and public API doubles. They do not call an LLM, QQ, or modify live plugin data.
