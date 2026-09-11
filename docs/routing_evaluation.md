# Routing regression evaluation

## Stateful replay

Replay is not "route every message, then score the last one". Each scored turn runs
the production order: expire buffered hovers, score against the committed state,
then commit this turn's own decision through the same
`SessionRuntime.commit_participation` that live routing calls. Bot output is an
anchor, never a scored turn.

Each case therefore reports `turns`, one entry per scored message, with its own
`actual` (recipients / bot_targeted / persona_addressed / level), the state it
actually saw (`pending_hover`, `pending_hover_user`, `active_interlocutor`,
`last_bot_message_id`, `last_bot_was_question`, `waiting_for_answer`,
`intervening_users`) and its own mismatches. The decision trace carries the same
real state instead of placeholders, and `should_reply` still stays null until a real
send decision exists.

A message may carry an `expected` object to assert that turn specifically; those
failures are reported as `turn<index>.<field>` and count towards `failed`. The
case-level `expected` still describes the final scored turn.

`"at"` sets a message's deterministic offset in seconds from the fixed clock, so
hover TTL expiry and stale-conversation boundaries are reproducible; the default
remains one-second steps.

Locked stateful boundaries:

| Case | Boundary |
| --- | --- |
| `hover_escalation_bounded` | A bystander's two consecutive ambiguous turns stay hover |
| `pending_hover_resolves_for_author` | The same messages from the bot's interlocutor escalate to strong |
| `answer_then_third_party_stays_hover` | After the interlocutor answers, a third party stays hover |
| `stale_hover_expires` | A hover older than the TTL is gone before the next turn is scored |

These are behaviour locks, not accuracy claims: they make cross-message state changes
visible in review instead of silently changing who gets answered.

## Optional topic and parent supervision

`tests/fixtures/routing_multiturn.json` adds deterministic interleaved hardware,
gaming, lunch and travel conversations. Run it with `--fixtures` independently
of the original golden file. Message IDs are their zero-based string indices.
Each message may provide `expected_topic` (a symbolic cluster label) and
`expected_parent` (an earlier message ID or explicit JSON null for no parent).
Missing, empty, whitespace-only and UNKNOWN labels are excluded. Explicit null
parent labels are scored; missing parent labels are not. Observations are taken
at each routing decision, so later rerouting does not rewrite earlier evidence.

Topic metrics compare all labeled pairs within each session, using equality
relations rather than matching predicted topic IDs to label spelling. They are
invariant to any one-to-one renaming of either set of labels. `wrong_merge`
counts pairs predicted together but labeled apart; `fragmentation` counts pairs
labeled together but predicted apart (including unassigned predictions).
Precision is TP/(TP+wrong_merge), recall TP/(TP+fragmentation). Undefined ratios
are null. Sessions are never compared with each other.

Candidate R@1/3/5 measures whether the recorded ranked candidates contain any
predicted topic ID attached to an earlier message with the same true label.
It is an observable proxy, not retrieval recall against an oracle topic store.
Only labeled turns with a prior observable matching topic and a recorded
candidate list are eligible. Missing candidate data and newly appearing topics
are excluded; an observed empty list on an eligible turn is a miss.

Parent accuracy is exact parent agreement over labeled turns, including true
null parents; coverage is the fraction of these turns with a nonempty predicted
parent. Recipient confusion counts and precision/recall are grouped by the
optional case `group` (for example explicit/continuation/ambient); unspecified
groups remain `ungrouped`, and absent recipient truth does not score.
With `--check`, supervised topic wrong merges or fragmentation and incorrect
labeled parents also fail the command. `checked_constraints` names the evaluated
conditions and `metric_failures` lists failed conditions; the existing `failed`
field retains its recipient-case count. Unlabeled topic/parent data imposes no
constraint. Candidate recall and parent coverage remain descriptive, with no
invented thresholds.

`--benchmark` optionally records elapsed evaluation seconds including metrics
construction. Default reports contain no timing and remain reproducible. This
is a local replay duration, not production latency or a throughput claim.

Run `python scripts/evaluate_routing.py --output docs/routing_current.json --check`
from the repository root. `--check` returns exit code 1 for any golden mismatch;
without it the evaluator records known failures without failing the command.
The package path is verified to avoid importing the stale nested plugin copy.
Use `--fixtures path/to/scenarios.json` to replay additional synthetic cases,
and `--baseline docs/routing_baseline.json` for a read-only comparison by case
ID. Comparison reports additions, removals, changed outcomes and failure delta.
It compares only the fields both reports recorded, so a newly recorded field is not
reported as a behaviour change; a field dropped from the current report still is.
Old baselines without fixture hashes report hash compatibility as null.
The CLI rejects output paths that overwrite its baseline or fixtures and creates
missing output directories. `--check` evaluates golden expectations, not whether
outcomes match historical defects.

Each case records the recipients, bot-targeted flag, persona admission and the
addressivity level (strong/hover/weak) for its final scored turn, plus the same
fields per turn. The level makes the silent-hover boundary
observable: a bystander short followup after the bot answered someone else stays
untargeted, admits nobody in persona mode, and is buffered at hover instead of
being answered. Locking the level means any future participation rework shows up
as a golden failure rather than an unnoticed behaviour change.

Each report includes SHA-256 config/fixture hashes, an explicit weights version,
and one schema 2 trace per case and mode. Identity comes from BotIdentityMatcher
with separate mention/vocative/subject booleans. `should_reply` remains null:
the evaluator observes routing/addressivity and persona admission, without a
final LLM decision or send. Persona traces do not invent a numeric score.
The configuration hash covers evaluator settings; per-case names and messages
are represented by the fixture hash. It is not a source-code checksum.

`tests/fixtures/routing_golden.json` contains synthetic, manually specified
recipient expectations. Each case creates a fresh session/DAG at time 1000,
uses hashed semantics without neural services, and disables the intense-dialogue
requirement. ThreadRouter, AddressivityRouter, describe_message, and persona
turn_is_addressed are the actual production implementations. Both participation
modes have separate confusion matrices; these are admission checks, not a full
LLM/provider/send replay or a population accuracy estimate.

`routing_baseline.json` was captured before identity/recipient edits from a
temporary copy of the root core package on 2026-09-11, after the ambiguity hotfix.
It contains 14 scenarios and three failing cases: a human mention plus Bot
vocative, the single-character Bot name 卡 used as a vocative, and a subject
reference followed by a direct vocative. The baseline
is historical evidence and must not be regenerated to conceal a regression.
The topic-ambiguity fixture deliberately sets topic uncertainty after real
routing; it isolates the consumer contract rather than reproducing a topic tie.

The golden suite covers mixed recipients, name substrings, single-character
vocatives and negative examples, active/bystander short followups, topic
uncertainty, subject references and human-only mentions. Extend it with small
synthetic scenarios when a real failure is understood; do not treat existing
topic annotation counts as recipient labels.

`build_routing_trace` in `core/routing_trace.py` accepts keyword-only routing,
identity, participation, state, mode and weights_version. It returns schema 2
using field allowlists and detached JSON-safe values. Pass identity as
`{"bot_reference": "vocative", "mention": false, "vocative": true, "subject": false}`,
participation as score/level/should_reply (null until a final decision exists),
and state as pending_hover/active_interlocutor/intervening_users. Never pass
message bodies in identifier or categorical fields. The trace does not include
raw text or free-form reasoning, and is diagnostic rather than a routing input.
