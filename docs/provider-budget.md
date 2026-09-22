# Provider admission and latency

`LLMAdapter.provider_budget` is shared by adapters attached to the same host
Context. It limits each resolved provider to four concurrent invocations;
background work may use at most three slots. Reply and necessary routing have
priority, followed by manual drafts, then automatic drafts, title and vibe work.
Every five seconds of waiting improves priority by one level, so an old background
request can progress even with continuing foreground arrivals. Running calls are
not preempted. These limits describe concurrency, **not RPM or token quotas**.

The queue holds at most 128 waiting calls. Queue overflow fails immediately.
Modern completions, legacy provider calls and native tool-loop calls all use
admission. The adapter's existing outer deadline covers provider lookup, queue
waiting and generation together (native execution also covers hooks/media).
Cancellation removes the waiter or releases its occupied slot. No adapter-level
retries are introduced; host/provider internal retry policy is outside this
concurrency controller.

Persona-axis projections use the background `persona_projection` purpose, with
at most four plugin-owned tasks and one in-flight calculation per fingerprint.
They never await model or KV I/O on the reply path or under the session lock;
the current turn reads only completed in-memory results. First use may therefore
have no axes. The persistent cache retains up to 128 fingerprints and merges
concurrent writes. Typed backends only load cached projections. Unload cancels
and drains these tasks; resetting a session does not discard this shared cache.

Routing and title use the reply provider preference; automatic drafts use the
draft preference. Only vibe uses the vibe preference. Context lifetime owns the
controller; slots-only weak-referenceable contexts use a weak registry. Unusual
legacy objects supporting neither attributes nor weak references should inject
the same `provider_budget=ProviderBudget()` into their adapters explicitly.

`provider_budget.diagnostics()` reports capacity, active/queued calls and per-purpose
lookup, queue and generation counts, with P50/P95 seconds over the latest 512
samples per stage. Queue samples include cancelled waits, generation samples
include failed/cancelled invocations. Legacy provider lookup emits the same separate lookup samples. Statistics are
in-memory and contain no prompts, provider credentials or message content.

## Reproducible controlled comparison

Run `.venv/Scripts/python scripts/benchmark_provider_budget.py` from the repository.
The script imports the actual `ProviderBudget` implementation. It compares the
same inputs with a FIFO semaphore baseline: two total slots, four background
calls followed by one reply after 10 ms, each provider call waiting 60 ms.
Each case runs two discarded warmups and twelve measured rounds; case order
alternates between rounds. The JSON includes environment, parameters, quantile
method, all raw queue times, total round duration and per-round call counts.

Results in [provider-budget-benchmark.json](provider-budget-benchmark.json):

| Metric | FIFO P50 / P95 | Reserved P50 / P95 |
| --- | --- | --- |
| Reply queue | 124.99 / 126.26 ms | 0.008 / 0.010 ms |
| Background queue | 30.78 / 63.26 ms | 93.30 / 187.04 ms |
| Total round duration | 186.67 / 189.31 ms | 248.52 / 251.24 ms |

Each measured round made exactly four background calls and one reply call.
The reservation improves reply admission in this scenario while increasing
background queue latency and total completion time. This is a controlled
synthetic provider-delay experiment, not a production throughput or end-to-end
quality claim. OS timer precision affects the realized 60 ms sleeps.

Run `.venv/Scripts/python -m pytest tests/test_provider_budget.py -q` for the
separate priority, aging, cancellation, shared-capacity, native-agent, lookup
observability and timeout correctness checks.
