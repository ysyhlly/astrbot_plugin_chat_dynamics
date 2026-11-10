# Autonomous Blocking

This is a standalone AstrBot 4.28.2 plugin. It owns a WAL SQLite database at
`data/plugin_data/astrbot_plugin_autonomous_blocking/blocking.sqlite3`; disabling
or reloading the plugin leaves that database intact. Rules begin empty and
`enabled` defaults to `true`. A `bot` rule applies across group/private sessions
and personas for one `(platform instance ID, bot self ID)` owner. A `session`
rule matches the full AstrBot unified message origin. Another bot instance has
an independent policy namespace.

`silence` allows event observers to inspect an incoming event, disables the
native default LLM path, then stops ordinary collectors at the lowest-priority
finalizer. The event is marked as silenced at arrival, so expiry during the
event does not restore a reply. This does not stop other plugins from reading
the message before the finalizer or from making their own unrelated LLM calls.
`shield` stops the event at the highest-priority entry, before ordinary
collectors. Both modes deny actual outbound sends for that event and every send
lease rechecks the policy. Messages already accepted by a platform cannot be
recalled.

The `/ablock list`, `/ablock status [uid]`, `/ablock set <uid> <silence|shield>
<30m|2h|permanent> [bot|session] [reason...]`, and `/ablock remove <uid>
[all|bot|session]` controls require AstrBot's real `event.is_admin()` result.
They run before ordinary collectors and return only UID, mode, and expiry.
`protected_user_ids` cannot be targeted, and the bot cannot target itself.
There is no blocked-user acknowledgement. An administrator's control command
remains available even when that administrator is covered by an ordinary rule.

Version 0.1.1 also registers a narrow Dashboard administrator API through
AstrBot's `Context.register_web_api` interface. Its routes are
`/api/plug/astrbot_plugin_autonomous_blocking/admin/owners` (GET),
`/api/plug/astrbot_plugin_autonomous_blocking/admin/rules` (GET and POST).
The authenticated Dashboard username is required and is recorded as
`dashboard:<username>` in the audit trail. The v1 plugin extension aliases are
rejected. The owner list is obtained from active aiocqhttp adapters with the
public `get_client().get_login_info()` call; every read and write revalidates
the chosen platform instance and returned bot UID. There is no owner fallback
from configured IDs or old message events.

The Dashboard POST accepts only permanent `shield` rules at `bot` scope, with
a caller-stable `action_id`, a numeric `target_uid`, and an optional reason.
It rejects caller-supplied actor/event fields, self-targeting, configured
protected users, stale/disconnected owners, and reuse of an `action_id` with
different parameters. Mutations are serialized by the current runtime's owner
lock and use the existing PolicyStore transaction, revision, audit, and cache
refresh path. A bot-wide rule matches all group and private conversations owned
by that same platform instance and bot UID.

The LLM receives request-local `set_interaction_boundary` and
`clear_interaction_boundary` tools. Setting can target only the current trusted
event author; an explicit different UID is rejected. Clearing only removes
existing rules belonging to this bot owner. Reasons are subjective audit notes
and are never included in policy reads or administrator listings.

Chat Dynamics integrations can use the instance methods `resolve_context`,
`peek_policy`, `get_policy`, `policy_snapshot`, `send_lease`, `filter_frames`,
and `filter_material`. These exchange plain dictionaries and lists and do not
expose database handles or audit history. A lease is held under the owner lock
across a platform send. Draft tokens contain `target_revision` and
the owner-wide `shield_epoch`; stale drafts stay invalid after a temporary rule
expires. Request-local mutation tools also bind `owner_revision` and recheck it
under the owner lock before changing a rule. A denied `event.send` or
`event.send_streaming` returns `False`; an allowed send preserves AstrBot's
original return value. Tool cancellation records only plugin-owned diagnostic
metadata and propagates `CancelledError`, leaving AstrBot's unrelated stop flags
untouched.

Shielding filters only material with trusted provenance metadata
(`author_uid`, `source_uids`, `subject_id`, `source_id`, and
`provenance_complete`). When a shield exists, unprovenanced user/assistant
history and retrieval frames are conservatively dropped. Static system and
developer instructions are retained. The plugin cannot guarantee removal of
content already summarized without source identity, OCR or image/audio meaning
repeated by another person, or information held by a tool that has no local
handler and no provenance. Tools without a local callable are removed because
their execution and result cannot be guarded. Opaque retrieval tools are also
removed when their name signals search, retrieval, memory, history, knowledge,
or MCP. A shield does not delete stored records, erase old aggregates already
derived from blocked material, or remove screenshots, quoted retellings, or
other content that no longer has trustworthy source identity. Handoff tools
are removed rather than treated as ordinary tools. Internal narrative events
marked `_narrative_internal_v1` do not receive the plugin's mutation tools.

Native streaming output is buffered until generation finishes and the send
lease is acquired, then the original chain sequence is replayed unchanged. This
prevents already streamed partial output from escaping a later rule, at the
cost of real-time token display. Buffering is capped at 4096 chain items and
8 MiB by the local representation-size estimate; exceeding either cap fails
closed. This cannot retract a send already in flight when another runtime
changes policy outside this plugin's owner lock. Outbound `At` targets and
`Reply` sender IDs are checked against recipient policy as well as the current
author. The plugin keeps a bounded in-memory ledger of at most 256 IDs for each
of 128 recently active sessions, mapping observed message IDs to author UIDs
without storing message text. Unknown reply targets are rejected while any policy is active for that session. Calls that
bypass `event.send`/`event.send_streaming` and invoke a platform adapter
directly are outside this guard.

Before disabling or removing this plugin while an integration has a required
gate, first clear that integration's requirement or leave the plugin enabled.
The database is retained across reloads. Plugin shutdown retires event-local
wrappers: any still-live old event is stopped and its pending
send wrapper remains closed until that event is collected. This prevents queued
drafts from escaping between reloads. It never patches shared AstrBot classes
or the global tool manager. New events handled by a reloaded instance get new
guards.

Validation should use the supplied isolated pytest suite and an AstrBot 4.28.2
fixture. Deployment, live LLM requests, and real QQ traffic are intentionally
outside this package's validation.
