# Operational shadow observations

Only actual comparisons returned by `LearningPolicyConsumer.shadow_decision`
are recorded, before the routing trace freezes. Off/active mode and unchanged
thresholds produce no observations. This store is independent of annotation and
training samples; a comparison has no correctness label.

AstrBot shared preferences: `scope="plugin"`, the ChatDynamics plugin scope ID,
`key="shadow_telemetry_v1"`. The version 1 envelope contains `schema_version`,
`updated_at` (Unix seconds), `retention_seconds`, `max_records`, and `observations`.
Each observation contains `observation_id`, `recorded_at`, `policy_id`,
`host_version`, `session_hash`, `reason`, `baseline_reply`, `shadow_reply`,
`score`, `baseline_threshold`, `shadow_threshold`, and `changed`.
The score is shared because phase one changes only the admission threshold.

No message text, user identifier, raw session, or raw message identifier is stored.
Session and observation identifiers are HMAC-SHA256 values with an installation
secret held separately in `shadow_telemetry_salt_v1`. Observation identity includes
session, message ID, policy ID, and host version; retries are deduplicated across
restarts while retained. Consumers must bucket by policy ID and host version.

Retention is 30 days and at most 20,000 observations. Old entries are pruned on
record, restore, and export. Snapshots are saved every 30 seconds and on orderly
shutdown; abrupt process termination can lose the most recent interval. Storage
failures are logged and retried at the next snapshot. Coverage describes this
retained observation window, not lifetime traffic or labelled training volume.
