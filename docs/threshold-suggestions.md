# Offline threshold suggestions

Run against one or more JSON annotation exports from the replay page:

```console
python scripts/suggest_thresholds.py topic-annotations.json other-session.json
```

The command prints JSON to stdout. It never writes configuration, policies,
input files, or reports. It accepts the exported `records` object, the API
`data.records` wrapper, or a list of annotation records. Unlabelled replay
snapshots cannot establish correctness and produce no suggestion.

## Evidence and scope

Only `strong_addressivity_threshold` can currently be assessed. Each sample
needs a human `expected_reply` label, `session_hash`, `msg_id`, and a legacy
`decision_trace.shadow` containing `reason=ambient`, a finite `score`, and
consistent `baseline_threshold`/`baseline_reply`. This is the same scalar
comparison used by `core/learning_policy.py:shadow_decision`. Explicit and
early-return decisions bypass this threshold and are excluded. Human-accepted
AI drafts count as human submissions; their labels still require human review.

Topic, parent, recipient, hover, and time thresholds are deliberately absent:
these exports do not preserve sufficient counterfactual state to recommend
them. Missing shadow evidence is not reconstructed from heuristic confidence.
The command does not require enabling collection or changing runtime settings.

At least 60 distinct eligible messages from 4 sessions are required. Conflicting
copies of a message are excluded; identical exports cannot increase support.
Weights versions and baseline thresholds must match. Sessions are sorted by
SHA-256 and alternated into training and validation, with at least 10 positive
and 10 negative labels in each split. Session identifiers are not printed.

Training chooses the best balanced accuracy over 0.50 through 0.90 in steps of
0.01 (plus the existing baseline), preferring the closest value on ties.
Validation must improve balanced accuracy by at least 0.05 without increasing
either false positives or false negatives. Otherwise `suggestions` is empty
and `reason` explains why. Reports include eligible and excluded counts, split
sizes, and validation confusion counts when available. These support minima
are conservative operational rules, not statistical significance guarantees.

A suggestion concerns only ambient admission on the recorded turns. It is
neither probability calibration nor a delivery prediction; later gates and
future conversation state are not replayed. Review label quality and run full
conversation replay before manually adopting a candidate. Selectively labelled
errors or shadow-only data may not represent ordinary traffic.
