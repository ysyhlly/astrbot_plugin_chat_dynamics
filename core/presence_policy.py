"""Participation preferences shared by persona prompting and admission limits."""

AMBIENT_OPENING_LIMITS = {"ghost": 0, "sensible": 2, "lively": 4}
MAX_AMBIENT_OPENINGS = max(AMBIENT_OPENING_LIMITS.values())


def participation_policy(presence: str = "sensible") -> dict:
    mode = presence if presence in AMBIENT_OPENING_LIMITS else "sensible"
    guidance = {
        "ghost": "Observe public chatter. Respond when directly addressed or continuing an addressed exchange.",
        "sensible": "Prefer observing; join when directly addressed, naturally continuing, or clearly helpful.",
        "lively": (
            "Actively look for a relevant, brief contribution to open group discussion: answer public "
            "questions, share a useful detail, or acknowledge a shared experience. An @ or identified "
            "recipient is not required for public discussion. Do not wait for a long silence. "
            "Avoid empty filler, repeated reactions, interrupting explicit human-to-human exchanges, "
            "private boundaries, conflict, or requests to stop. Choose ignore when nothing useful fits."
        ),
    }[mode]
    return {
        "presence_knob": mode,
        "ambient_openings_per_minute": AMBIENT_OPENING_LIMITS[mode],
        "guidance": guidance,
    }
