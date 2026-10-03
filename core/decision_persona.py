"""Extract a small participation-oriented persona view without another model call."""

import re
from functools import lru_cache

from .context_retrieval import clip_text

MAX_PERSONA_CHARS = 1200
_IDENTITY = re.compile(r"身份|名字|名称|昵称|称呼|角色|你是|我是|\b(name|identity|you are|role)\b", re.I)
_PARTICIPATION = re.compile(r"参与|接话|群聊|回应|说话|语气|发言|旁听|主动|\b(participat\w*|speak|respond|tone|chat)\b", re.I)
_BOUNDARY = re.compile(r"边界|禁止|不要|不能|不得|不许|拒绝|隐私|停止|避免|仅限|只在|除非|严禁|泄露|私人|争执|不(?:主动)?(?:参与|接话|回应|回复|发言|介入)|\b(boundar\w*|never|do not|don't|avoid|privacy|refuse|only when|unless|must not)\b", re.I)


@lru_cache(maxsize=32)
def decision_persona_summary(prompt: str) -> str:
    """Retain original sentences, prioritizing boundaries wherever they occur.

    Short cards stay verbatim. Long cards use separate identity, participation
    and boundary budgets; no facts or behavioral rules are invented. Head/tail
    clipping keeps late constraints when a category itself is too large.
    """
    prompt = prompt.strip()
    if len(prompt) <= MAX_PERSONA_CHARS:
        return prompt
    groups: dict[str, list[str]] = {"identity": [], "participation": [], "boundaries": []}
    section = ""
    for line in prompt.splitlines():
        line = line.strip()
        if not line:
            continue
        # Preserve the meaning of bullets beneath a named section.
        if len(line) <= 40 and (line.endswith((":", "：")) or line.startswith("#")):
            section = ("boundaries" if _BOUNDARY.search(line) else
                       "identity" if _IDENTITY.search(line) else
                       "participation" if _PARTICIPATION.search(line) else "")
        for sentence in re.split(r"(?<=[。！？!?;；])|(?<=\.)\s+", line):
            sentence = sentence.strip()
            if not sentence:
                continue
            category = ("boundaries" if _BOUNDARY.search(sentence) else
                        section or ("identity" if _IDENTITY.search(sentence) else
                                    "participation" if _PARTICIPATION.search(sentence) else ""))
            if category and sentence not in groups[category]:
                groups[category].append(sentence)
    # The opening is the fallback identity/context when the card has no headings.
    identity = "\n".join(groups["identity"]) or clip_text(prompt, 220)
    sections = [("Identity/context excerpts", identity, 240),
                ("Participation excerpts", "\n".join(groups["participation"]), 260),
                ("Boundary excerpts", "\n".join(groups["boundaries"]), 600)]
    return "\n".join(f"{label}:\n{clip_text(text, budget)}" for label, text, budget in sections if text)
