# Narrative coordination (schema v1)

## Communication purpose and nonliteral language (rc.33)

Three optional Choice questions use only capacity left after participation,
context completion, topic and addressee questions. They classify the current
speaker's communication purpose, expression mode and contribution to the prior
exchange. No extra Jev or wording-model round is introduced. Under a full budget
these estimates remain unavailable rather than reducing existing context.

The reply request's `interaction_understanding` contains validated, tentative
estimates tied to the session, speaker and trigger message. Intent/update require
at least 0.7 confidence and nonliteral expression at least 0.8, also respecting
a higher configured decision floor. Missing, invalid or low-confidence values
become `unclear`. A context-completion recheck replaces earlier estimates.

Reply guidance distinguishes sharing/venting from requests for solutions, and
humorous wording from the real need underneath. Known memes may be met naturally;
callbacks require actual shared evidence, and uncertain references are not given
invented explanations. The response should address the new detail or correction,
not repeat the prior reassurance, advice or question. These are wording hints,
not participation permission or facts about the speaker's mind. Explicit requests,
real distress, current source evidence and existing boundaries remain authoritative.

## Optional context completion (rc.32)

Enable **上下文不足时自动补查** in Settings → Decision model to let Jev request
one read-only lookup before the final reply decision. The default is off. The
first decision distinguishes ready/recent/topic/memory/clarify; the second offers
only ready/clarify. Silence remains a valid decision and is not itself a reason
to retrieve. The step applies to group dialogue and pokes, not private chats,
observation mode or autonomous contacts.

Local context is restricted to the original turn watermark, the same session,
and the preceding thirty minutes. At most eight excerpts (3,000 characters total)
are added after member-boundary filtering. Memory mode, or an empty local lookup,
uses the existing public LivingMemory recall API within a shared eight-second
lookup deadline. The subsequent Jev decision has its existing timeout.

Memory additions preserve original input event IDs and host-history snapshots.
Each atom must pass scope/card/revision, provenance and revocation validation;
whole atoms fit within a 3,000-character addition budget. Changed correction
revisions or invalidated turns are not combined. No event is replayed or sent
by retrieval. Logs record mode, status, count and duration without query text.
An empty/unavailable lookup is not proof of absence; the model should clarify a
necessary missing fact. Existing user-requested Agent tools remain available.

## Poke sender context (rc.31)

Trusted platform pokes carry a `poke_at_bot` graph flag. A standalone poke
selects up to six messages from its actual sender and six confirmed delivered
bot replies to that sender in the same session, within thirty minutes and the
original turn watermark. Each excerpt is bounded to 400 characters. This
selection does not follow an unrelated inferred topic or another member's thread.
Typed poke-like text cannot opt into this path.

The existing member-boundary filter applies before the decision and expression
requests. The legacy reply path uses the same selection and filter. Scoped memory
recall uses an attributed sender query with at most four short source excerpts;
social and persona context continue to use the actual initiating member.
History is background, not a new instruction or proof of the poke's intent.
No new model round, contact schedule or external send is added.

## Continuing a conversation (rc.28)

The immutable dialogue snapshot now includes `continuation_kind` (local source
evidence) and `next_step` (check the current answer, respond to an earlier answer,
continue the topic, or evaluate reopening). These are projected from delivered
bot turns and the same member's visible updates, not an additional mutable memory.
Candidates expire after ten minutes, or fifteen minutes for a focused task.

One extra `continuity` choice in the existing Jev call checks the current message
against that exchange. Confident answer/continue/reopen and bot-recipient choices
can preserve a response despite a generic ignore/close or low ambient join score.
Missing/invalid classification, another recipient, stop/boundary evidence, an
unfinished input or strong competing-peer evidence cannot force a response.
An ordinary conversational close can reopen; a member stop invalidates the anchor.

Confirmed dialogue replies avoid new-opening quotas and unrelated-human relay
heuristics. They do not acquire explicit-mention wake or media permissions.
They still pass runtime revisions, member boundaries, sleep and media checks.
The reply goal addresses the new contribution and may add one relevant detail,
thought or useful question. It never requires automatic follow-up messages.

## Continuing character behavior (rc.27)

The Memory Notebook's **Life diary**, **Current session commitments**,
**Contact intents** and **Delivery receipts** sections start collapsed. Click a heading
or focus it and press Enter/Space to expand or collapse it. Long diary lists
scroll within each expanded section; refreshing life data preserves each section's open
state. Reloading the page starts collapsed again. Stored records and contact actions are unchanged.

Dynamic Life State v0.4.10 supplies an optional `humanlike` snapshot. Jev and
the expression model receive the same current-speaker projection: stable
preferences, pending small goals, decaying affect and an optional nickname.
These are attributed background, not instructions or new execution authority.
Current feedback and explicit nickname objections take priority. Nicknames
never participate in routing, aliases, mentions or wake detection.

Only the current member's mood/name and relevant scoped interests are selected.
Role-input material also passes the public member-boundary filter. A natural
ending permits silence or a brief warm close, without a new question or goal;
existing explicit-mention and silence-boundary rules remain authoritative.

New state accumulates through existing narrative calls, subject to the existing
queue and hourly budget. No prefilled preferences, emotions or nicknames are
written at deployment. Source rollback preserves the optional state fields.

The default is disabled: `narrative_enabled=false`, `narrative_sessions=[]`,
`narrative_outreach_enabled=false`. Existing all-group takeover does not grant
narrative access. Complete UMO values must also be enabled in the life plugin.

## Ownership

Worldbook resolves the character and complete group rules. Dynamic Life State
owns the continuing life, scoped experience and commitments. Chat Dynamics
owns participation, the current native Agent, all proactive sends and durable
platform attempts. LivingMemory, SelfLearning and Portrayal expose scoped public
material; no plugin opens another plugin's database.

`IntegrationRegistry.narrative_provider(name)` discovers an activated instance
from the current public AstrBot catalog on each use. Results from an instance
unloaded during a call are discarded. There are no cross-plugin private imports
or HTTP loops.

`TurnContextBundle` is frozen serialized JSON. Core/group rules, role identity,
life revisions, source materials and exact incoming envelopes are shared between
Jev and expression. The 1,500-character life view participates in existing
12,000-character state / 18,000-character request budgets. Optional background
is removed before current identity and constraints.

## Interaction

Managed Jev turns additionally classify `impact` as none/activity/commitment/
relationship. Significant turns get at most one eight-second narrative wait and
one decision recheck. Exact original envelopes are used even after notification
acknowledgement; an event ID is never reused with changed metadata. Timeouts leave
events queued. Direct platform @ is always answered, including a brief refusal
while busy/asleep. Unmanaged @ still uses the existing direct path.

Managed requests carry `_narrative_managed_v1`, with per-provider completed flags
even when a lookup returned no material. Optional-provider availability is
reported separately. Normal hooks therefore do not inject duplicate memory,
character or social goals. Internal motives additionally carry
`_narrative_internal_v1`; they are bot-owned events, never historical users.
They force full tool-schema mode with no tools or cron authorization.

Life replaces the old independent sleep narrative in managed turns. The existing
media/privacy/manners checks remain active. Random cold-gap, morning and insomnia
triggers are disabled there. Narrative participation uses the sensible profile;
existing unmanaged settings are unchanged.

## Execution facts and contacts

`submit_intent(intent)` is the public plugin entry point. Intentions contain a
source-attributed reason and exact destination, not future prewritten text.
The life authority is reread before every attempt; pause postpones rather than
deletes an intention. Scope comparison includes platform, full UMO, persona and
instance. Both global and scoped revisions, host persona, enablement, runtime
epoch and cooldown are checked again. A durable life claim is required before
the platform call. Normal native reminders continue on their original route.

Spontaneous contacts are limited to two per group per China day, separated by
two hours, and paused from 00:00 through 08:00. Definite failure or unknown
delivery never causes automatic contact redelivery. No-response is neutral.

`narrative-delivery.sqlite3` records sending before transport. Actual attempt
status and the pending notification event are committed in one transaction.
Each successful fragment supplies its actual text; failed/uncertain drafts have
empty text. A crash during sending recovers as uncertain. Notification retries
are independent of platform retries; source APIs deduplicate their event IDs.
Late definite receipts can clarify uncertainty without another send.

Tool receipts distinguish a tool call returning from proof that an external
operation succeeded; generic receipts do not complete unrelated promises.

## Management and compatibility

The existing memory page includes life/diary/commitment/intent/source/receipt
sections. `GET/POST narrative` requires the host Dashboard administrator JWT;
plugin-scoped API keys cannot read a diary. Pause/resume, contact pause,
intent cancellation and source-attributed manual corrections are supported.
Corrections append history rather than overwrite it.

Installers should set SelfLearning
`Persona_Evolution_Settings.managed_persona_ids` before enabling a pilot, so its
background writers cannot update that core personality during startup.
Learned expressions and observations remain available. Old provenance-free
portraits do not enter managed contexts.

Rolling back source/configuration leaves new sidecar journals intact, avoids
discarding real user observations, and allows future reinstallation. The default
mode and empty whitelist are compatible with older unmanaged behavior.
