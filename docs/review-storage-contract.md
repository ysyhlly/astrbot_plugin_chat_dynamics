# Review storage contract

`TopicAnnotations` owns review serialization within one plugin instance. A process
must use that single store; its asyncio lock is not a cross-process transaction.
The storage backend must atomically replace an individual KV value.

## Generation

Read `read_drafts(session).get("draft_epoch", 0)` before starting model work.
Pass it as `expected_epoch` to `save_drafts`, together with `is_current` when the
caller has a lifecycle token. Both checks run under the store lock. Every clear,
including an empty clear, advances the epoch. At commit the store rereads human
labels and dismissed IDs. A normal refresh cannot recreate either; only explicit
`regenerate_dismissed=True` permits dismissed IDs, never human labels.

## Durability

A draft carries the message it was written about. `save_drafts` copies the
routing block, the frozen decision trace, the wall-clock stamp, and the text --
the text only while `console_show_message_content` is on -- into
`drafts.<msg_id>.context`, bounded per draft (`MAX_DRAFT_CONTEXT_BYTES`) and per
session (`MAX_DRAFT_CONTEXTS`, `MAX_DRAFT_CONTEXT_TOTAL_BYTES`). A snapshot that
does not fit either bound is left out, and that draft keeps the old behaviour:
listed, but only dismissible once its message is gone. Capture only ever adds.
Rewriting a snapshot would change the draft revision an open approval page
previewed, and turn a human pressing accept into "草稿已更新，请刷新后重新确认".

`save(..., recovered_node=...)` is the only path that writes a label without the
live node: `annotation_drafts_apply` rebuilds a node from the snapshot
(`annotation_draft.context_node`) and hands it to the same save path, so the
record -- `predicted_topic`, `routing`, `decision_trace` -- is the one a restart
would have produced rather than a second schema. A draft with no snapshot has
nothing to write from and is skipped with its reason.

The pages never receive a snapshot. `public_draft` strips it from `read()` and
from the generation response, and the approval list builds its rows from the
snapshot fields it needs instead of shipping the trace back out.

## Request retries and recovery

`save(body, request_id=...)` records the label and exact successful result in one
authoritative `annotation_review_state_v2_<digest>` value. Retrying the same input
returns that result, even after the live message disappears. Different input with
the same ID is rejected. `topic_annotations_v1_<digest>` remains a list projection
for legacy learning consumers. Projection failure may briefly leave those consumers
with older data; it cannot create an unrecorded successful authoritative label.
`projection_pending` marks recovery work. Await `reconcile()` at startup to rebuild
projections; request retry also repairs its projection. Reconciliation is idempotent.

`clear_drafts` and `remove_drafts` accept request IDs and atomically store their
results beside the draft mutation. `read_request(session, request_id)` retrieves
successful stored results. A batch must use stable unique per-item IDs and inspect
these results **before** requiring a still-present draft. After all items finish,
`record_request(session, request_id, body, result)` caches its aggregate response.
An interrupted batch resumes individual commits; the aggregate cache alone is not
a transaction. Failures before an item commits can be retried. Request IDs should
use distinct suffixes for clear/dismiss suboperations. An accepted item receipt
stores `accepted_draft_revision` atomically with the label. Cleanup on retry uses
that original revision, so a newly written draft survives an old request retry.
Legacy receipts without a revision do not authorize cleanup. Request IDs should
be namespaced by operation. Hot request maps keep at most 257 results; older
committed results are archived under deterministic session/request digest keys.
Archive writes precede pruning the hot map, so interruption only duplicates data.
Lookup and input-conflict checks include archives. Archives are retained indefinitely
and never expire into permission to execute an old request again. This bounds the
hot session document rather than total disk usage. A future disk retention policy
must preserve rejection tombstones if it removes archived result payloads.

## Discoverability

The session index has 256 deterministic SHA-256 prefix shards, each storing its
complete list. The old unsharded index remains readable. Registration precedes data
writes, so a crash leaves a discoverable empty entry, never an unindexed new draft.
`known_sessions()` returns all shards without a 200-session cap. Empty entries and
epoch/dismissal tombstones remain enumerable for reconciliation; clearing drafts
does not delete cancellation history. Previously lost legacy index entries cannot
be reconstructed without a backend key enumeration facility.
