"""Participation preferences shared by persona prompting and admission limits."""

AMBIENT_OPENING_LIMITS = {"ghost": 0, "sensible": 2, "lively": 4}
MAX_AMBIENT_OPENINGS = max(AMBIENT_OPENING_LIMITS.values())


def participation_policy(presence: str = "sensible") -> dict:
    mode = presence if presence in AMBIENT_OPENING_LIMITS else "sensible"
    guidance = {
        "ghost": "Observe public chatter. Respond when directly addressed or continuing an addressed exchange.",
        "sensible": "Prefer observing; join when directly addressed, naturally continuing, or clearly helpful.",
        "lively": (
            "Actively contribute a distinct own take, fresh angle, playful riff, banter or fitting joke "
            "to open group discussion. The topic need not concern this participant; two humans taking turns "
            "is not alone private, and no @ or silence is needed. Never join just to echo, agree or act as "
            "a yes-man. Avoid empty filler, repeated reactions, interrupting private exchanges, boundaries, "
            "conflict or stops; choose ignore with no distinct take or joke."
        ),
    }[mode]
    return {
        "presence_knob": mode,
        "ambient_openings_per_minute": AMBIENT_OPENING_LIMITS[mode],
        "guidance": guidance,
    }
