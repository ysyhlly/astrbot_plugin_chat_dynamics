"""Bound excerpts using whole sentences, keeping qualifications with their context."""
import re

OMITTED = '【原文过长，未展示；不得据此推断态度、授权或承诺】'
PARTIAL = '【节选；未展示部分不代表同意或承诺】'
_BOUNDARY = re.compile(r'不|没|勿|禁止|拒绝|仅|只|除非|如果|但是|不过|然而|边界|同意|意愿|条件|取消|过期|\b(?:not|never|no|only|unless|except|if|but|consent|boundary)\b', re.I)
_LINK = re.compile(r'^(?:但|不过|然而|只是|除非|如果|其实|这|那|并|才怪|开玩笑|否则|but\b|however\b|unless\b|otherwise\b|except\b)', re.I)


def excerpt(value, limit):
    """No new summary or inferred fact. An indivisible long qualifier is omitted."""
    text = str(value or '')
    if len(text) <= limit:
        return text
    if limit < len(OMITTED):
        return ''
    units = [x.strip() for x in re.split(r'(?<=[。！？!?])\s*|(?<=\.)\s+|\n+', text) if x.strip()]
    merged = []
    for unit in units:
        if merged and _LINK.search(unit):
            merged[-1] += ' ' + unit
        else:
            merged.append(unit)
    required = {i for i,unit in enumerate(merged) if _BOUNDARY.search(unit)}
    allowance = limit - len(PARTIAL) - 1
    def size(ids):return sum(len(merged[i]) for i in ids) + max(0, len(ids)-1)
    if size(required) > allowance:
        return OMITTED
    selected = set(required)
    for i in range(len(merged)):
        if size(selected | {i}) <= allowance:
            selected.add(i)
    if not selected:
        return OMITTED
    return PARTIAL + '\n' + '\n'.join(merged[i] for i in sorted(selected))
