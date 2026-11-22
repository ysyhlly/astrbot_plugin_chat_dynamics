"""Participation preferences shared by persona prompting and admission limits."""

AMBIENT_OPENING_LIMITS = {"ghost": 0, "sensible": 2, "lively": 4}
MAX_AMBIENT_OPENINGS = max(AMBIENT_OPENING_LIMITS.values())


def participation_policy(presence: str = "sensible") -> dict:
    mode = presence if presence in AMBIENT_OPENING_LIMITS else "sensible"
    guidance = {
        "ghost": "Observe public chatter. Respond when directly addressed or continuing an addressed exchange.",
        "sensible": "Prefer observing; join when directly addressed, naturally continuing, or clearly helpful.",
        "lively": (
            "Actively look for a distinct contribution to open group discussion: answer public "
            "questions, share your own take/stance, a fresh angle, playful riff, banter or fitting joke. "
            "The topic need not concern this participant. Two humans taking turns does not alone make a topic private. "
            "An @ or identified recipient is not required for public discussion. Do not wait for a long silence. "
            "Do NOT join just to echo, agree or act as a yes-man ('true/agree/haha/me too'). "
            "Avoid empty filler, repeated reactions, interrupting explicit human-to-human exchanges, "
            "private boundaries, conflict, or requests to stop. Choose ignore when you have no distinct take or joke to add."
        ),
    }[mode]
    return {
        "presence_knob": mode,
        "ambient_openings_per_minute": AMBIENT_OPENING_LIMITS[mode],
        "guidance": guidance,
    }
