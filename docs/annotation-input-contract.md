# Annotation input contract

`build_batch` constructs the selected window. `build_batches` partitions its targets before JSON serialization. Every requested target occurs in exactly one batch; context rows are never implicit targets. Each complete system plus user prompt is at most 8,000 characters. Target text and available quoted messages have priority, then nearest chronological context. If even a target and its quote cannot fit a caller-supplied budget, construction raises an explicit error. No JSON string is truncated.

Authors, mentions and quoted authors share a window-local one-to-one U1/U2 mapping. The known bot is BOT. Missing author identity and unknown bot mention status are JSON null. Mentions of another person do not mean mentions of the bot. External quoted authors retain an identity even when the quoted message is absent. No host decision is included.

`parse_drafts(..., diagnostics=True)` returns diagnostics even when no draft survives. `missing`, `duplicate`, `out_of_window` and `invalid_confidence` are separate. Invalid confidence includes non-numeric values, booleans, infinities, NaN and values outside 0 through 1; these rows are rejected rather than clamped into certainty. Missing answers never become negative labels. The legacy default still returns None when no valid draft survives. Partial boolean answers remain partial; they never acquire an invented negative field.

The runtime must call the model separately for each returned batch, parse against that exact batch, and aggregate successful drafts and diagnostics. The generic text truncator must not subsequently modify these prompts.
