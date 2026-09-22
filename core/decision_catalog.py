"""SDK-independent coverage vocabulary; tests guard parity with runtime tasks.

Topic labels are request-local candidates. Only KEEP is universally available;
dataset summaries add the actual offered topic keys from recorded questions.
"""

BINARY_LABELS = ("false", "true")
SCORE_LABELS = ("0", "1", "2", "3", "4")
DYNAMIC_TASKS = frozenset({"topic", "recipient_choice"})
TASK_LABELS = {
    "join": BINARY_LABELS,
    "action": ("ignore", "acknowledge", "clarify", "reply", "close"),
    "state": ("observing", "casual", "focused", "supportive", "playful", "disengaging"),
    "length": ("brief", "normal", "detailed"),
    "reply_length": ("tiny", "short", "medium", "long", "very_long"),
    "recipient_choice": ("none",),
    "reason": (
        "addressed_request",
        "addressed_question",
        "ongoing_thread",
        "open_group_topic",
        "social_signal",
        "other_recipient",
        "boundary_or_sensitive",
        "low_value_chatter",
    ),
    "target": BINARY_LABELS,
    "vibe": ("fast_banter", "serious_inquiry", "chill_fade"),
    "topic_relevance": SCORE_LABELS,
    "question_value": SCORE_LABELS,
    "professionalism": SCORE_LABELS,
    "silence_bias": SCORE_LABELS,
    "force_scale": SCORE_LABELS,
    "completeness": BINARY_LABELS,
    "recipient": BINARY_LABELS,
    "topic": ("KEEP",),
    "persona.chattiness": SCORE_LABELS,
    "persona.warmth": SCORE_LABELS,
    "persona.formality": SCORE_LABELS,
    "persona.humour": SCORE_LABELS,
    "persona.initiative": SCORE_LABELS,
    "persona.boundary": SCORE_LABELS,
}
