# Routing resource observations

`EmbeddingAdapter.snapshot_stats()` returns a detached dictionary of integers.
Counters cover the adapter lifetime and survive configure; they contain no text,
provider error strings, or identifiers:

| Field | Meaning |
| --- | --- |
| `provider_calls` | Actual calls to a supported provider embedding method |
| `timeouts` | Timeout exceptions from embedding execution |
| `failures` | Other execution exceptions or empty/invalid vector responses |
| `provider_unavailable` | No provider could be resolved |
| `cache_hits` | `embed()` requests served directly from vector cache |
| `singleflight_joins` | `embed()` requests joining an admitted same-text request |
| `capacity_fallbacks` | Distinct requests rejected at the admission limit |
| `inflight` | Currently unfinished internal tasks, including cancelled tasks still cleaning up |
| `cache_entries` | Current vector cache size |

Timeouts and failures are disjoint. Cancellation is neither. Cache hits exclude
the synchronous matching/profile cache reads, so cache probing cannot inflate
the request hit count. Counters observe actual work even if configure invalidates
that generation; they do not authorize stale vectors to enter the new cache.

`context_statistics(payload)` is a pure function for the already-built context.
It returns `current_characters`, `background_characters`, `current_messages`,
`background_messages`, `total_messages`, and `serialized_characters`. Current
message count comes from the fragment semantics entries; current characters come
from the consolidated current turn. Background characters count the bounded text
actually in the payload. Serialized characters use `json.dumps(payload,
ensure_ascii=False)` with default separators, including metadata and JSON escaping.
This normalized context measurement excludes any surrounding prompt. It measures
Unicode characters, not bytes or model tokens. Nothing is added to the LLM payload.

Validation: resource-statistics, embedding-limits and embeddings suites: 18 passed.
Tests cover actual timeout, failure, missing provider, cancellation, overload,
same-text sharing, cache hits, detached snapshots and unchanged context input.
