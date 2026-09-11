# Routing regression evaluation

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
addressivity level (strong/hover/weak). The level makes the silent-hover boundary
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
