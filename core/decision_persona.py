"""Select complete persona statements without erasing an entire rule category."""

import re
from functools import lru_cache

MAX_PERSONA_CHARS = 1200
_IDENTITY = re.compile(r"^(?:你是|我是|你正在扮演|姓名|名字|名称|昵称|角色名|身份|name\b|identity\b|you are\b)", re.I)
_PARTICIPATION = re.compile(
    r"参与|接话|群聊|回应|说话|语气|发言|旁听|主动|性格|相处|善意|熟人|熟悉|关心|兴趣|游戏|写作|小说|慢热|省略号|\b(?:participat\w*|speak|respond|tone|chat)\b",
    re.I,
)
_BOUNDARY = re.compile(
    r"隐私|私人争执|越界|强迫|拒绝|同意|不舒服|停止|不贬低|不辱骂|边界|\b(?:boundar\w*|privacy|consent|refus\w*|stop)\b",
    re.I,
)
_WARM = re.compile(
    r"善意|关心|熟人|熟悉|自然|愿意|兴趣|接受|温和|安心|省略号|\b(?:kindness|familiar|warm|interest)\b", re.I
)
_LINK = re.compile(r"^(?:但|不过|然而|只是|除非|否则|其实|并不|不代表|but\b|however\b|unless\b|otherwise\b)", re.I)


def _statements(prompt):
    section = None
    background = False
    for line in prompt.splitlines():
        line = line.strip()
        if not line:
            continue
        if line in {"【角色描述】", "【场景】"} or line.startswith("【世界书】"):
            background = True
        elif line in {"【性格】", "【输出要求】"}:
            background = False
        heading = len(line) <= 48 and (line.endswith(("：", ":")) or line.startswith(("#", "【")))
        if heading:
            section = (
                "boundaries"
                if _BOUNDARY.search(line)
                else "participation"
                if _PARTICIPATION.search(line)
                else "identity"
                if _IDENTITY.search(line.strip("#【】 "))
                else None
            )
            continue
        if background:
            continue
        unit = re.sub(r"^(?:[-*]\s+|\d+[.、]\s*)", "", line)
        # Treat a source line as one indivisible statement. In particular, do
        # not split punctuation inside quotes and lose a following qualifier.
        if _IDENTITY.search(unit):
            yield "identity", unit, 100
        elif re.search(r"隐私|私人争执|不舒服|同意|越界|privacy|consent", unit, re.I):
            yield "boundaries", unit, 80
        elif re.search(r"省略号|开场|推脱|句式", unit):
            yield "participation", unit, 80
        elif _WARM.search(unit) and _PARTICIPATION.search(unit):
            yield "participation", unit, 40
        elif _BOUNDARY.search(unit) or section == "boundaries":
            yield "boundaries", unit, 20
        elif _PARTICIPATION.search(unit) or section == "participation":
            yield "participation", unit, 25
        elif section == "identity":
            yield "identity", unit, 10


@lru_cache(maxsize=32)
def decision_persona_summary(prompt: str) -> str:
    """Select complete source statements separately for identity, tone and limits.

    Oversized statements are skipped individually, not allowed to erase an
    entire category. The complete persona remains the authoritative source.
    """
    prompt = prompt.strip()
    from .decision_compaction import audited_persona

    reviewed = audited_persona(prompt)
    if reviewed is not None:
        return reviewed
    if len(prompt) <= MAX_PERSONA_CHARS:
        return prompt
    groups = {"identity": [], "participation": [], "boundaries": []}
    seen = set()
    for order, (category, unit, score) in enumerate(_statements(prompt)):
        if unit not in seen:
            groups[category].append((score, order, unit))
            seen.add(unit)
    sections = []
    for key, label, allowance in [
        ("identity", "Identity/context excerpts", 180),
        ("participation", "Participation excerpts", 470),
        ("boundaries", "Boundary excerpts", 430),
    ]:
        selected = []
        used = 0
        for score, order, unit in sorted(groups[key], key=lambda x: (-x[0], x[1])):
            if used + len(unit) + bool(selected) <= allowance:
                selected.append((order, unit))
                used += len(unit) + (len(selected) > 1)
        if selected:
            sections.append(label + ":\n" + "\n".join(unit for _, unit in sorted(selected)))
    return "\n".join(sections) or "Persona statements exceed this view; use the authoritative character rules."
