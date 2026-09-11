# Embedding behavior and resource bounds

The production graph and topic router consume `SemanticMatch.score` through
`semantic_match_fn`. `EmbeddingAdapter.should_link()` is a compatibility helper,
not the production edge decision. Its old dependency on `last_backend` therefore
was not evidence of a production routing bug.

Each `SemanticMatch` now carries `backend` (`hashed` by default, or `neural`).
The helper consumes that value, so evaluating another pair cannot change the
meaning of an earlier result. `last_backend` remains a best-effort diagnostic
for the existing status/dashboard consumers. Matching scores and thresholds
are unchanged.

Existing same-text single-flight behavior and the bounded vector LRU remain.
Distinct uncached requests now admit at most eight internal requests per adapter
(the constructor can override `max_inflight`). Excess requests immediately return
`None`, leaving callers on the existing hashed fallback; they do not create a
waiting queue. Duplicate waiters still share an admitted request at capacity.
The limit also counts cancelled requests until completion, including requests
whose providers temporarily suppress cancellation.

Reconfiguration still cancels pending requests and invalidates cached vectors.
Requests capture their generation when created; stale results and errors cannot
update the current generation. Provider objects are intentionally resolved live:
without a host registry revision, caching them could retain a replaced provider.
No existing configuration keys or defaults were renamed.

Validation: `tests/test_embeddings.py` and `tests/test_embedding_limits.py`:
13 passed. Regression coverage includes interleaved backend results, a burst of
98 excess requests, duplicate joining at capacity, and a provider that suppresses
cancellation during reconfiguration. Ruff passed for the modified Python files.
