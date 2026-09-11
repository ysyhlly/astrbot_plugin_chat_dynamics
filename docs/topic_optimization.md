# Topic optimization review

The production `ThreadRouter.route` calls `TopicResolver.resolve`; topic scoring
already has a bounded two-view profile cache on session-owned topic state.
The fingerprint includes actual node content, recipients and parent identity,
the eligible historical node set, provider generation, dimensions, and vectors.
Reassignment, eviction, routing changes and vector warmup already invalidate the
appropriate view. `remember` already rebuilds only affected topics. These are
existing implementations, not new work required by this round.

Two output-equivalent changes are applied: neural scoring no longer computes a
hashed query vector that it immediately discards, and scoring no longer repeats
the time-window filter already applied by `rebuild_profile`. Public full profiles
and private historical views retain their existing behavior and cache bounds.

A topic shortlist changes recall: a candidate with low lexical overlap may still
win from dialogue lineage or neural semantics. There is no measured candidate
recall/latency result establishing a safe shortlist size here. Leave all existing
candidates eligible until replay evidence reports candidate recall, final topic
accuracy, explicit-reply preservation, and latency at realistic topic counts.

The proposed 70% neural coverage policy also changes output. Current centroids
require complete coverage in one provider generation and dimension, including
the query. A partial neural subset can disproportionately represent the warmed
messages; mixing hashed and neural vectors is invalid even when dimensions
match. Keep complete-coverage fallback until an experiment measures warmup-order
sensitivity, topic membership accuracy, fallback rates, and provider replacement
behavior. Neither a new shortlist default nor a 70% threshold is introduced.
