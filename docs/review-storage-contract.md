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
