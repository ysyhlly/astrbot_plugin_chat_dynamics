# Turn evidence lifecycle

`TurnEvidence` wraps the existing `ParticipationSnapshot` and `ParticipationPolicy`.
Capture it after bounded context selection, before any asynchronous work. Its identity
contains session/message, runtime epoch, configuration and policy identities, and the
visible context cutoff. Store stable configuration identities, not object addresses.
Only messages at or before the cutoff belong to its context. `evaluate()` runs the
existing pure policy; ordinary confidence and contribution scores are not calibrated
probabilities. Capturing must not create a second participation decision path.

Validate `is_current` under the session lock before committing asynchronous results.
A generation epoch, configuration or policy change invalidates the prepared result.
Delivery state is committed only after an actual successful send.

Build the allowlisted trace from captured inputs. `finalize_decision_trace` seals the
actual admission decision and its short-circuit/gate branch once. Save the complete
trace through `compact_trace_inputs`; the global snapshot budget controls retention.
Older partial trace inputs may be rebuilt only as legacy incomplete diagnostics.
New traces must never be rebuilt from a later routing state. Annotation exports use
`trace_with_updates` to copy the decision and attach separate outcome/shadow blocks.
Outcome writes are copy-on-write: holders of an earlier trace retain the same decision
and observations. A later reroute cannot rewrite why the original decision happened.
