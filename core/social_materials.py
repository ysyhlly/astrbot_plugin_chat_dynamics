"""Select current-member evidence before optional, query-relevant terminology."""
import re


def select_social_items(items, query, speaker_id, limit=8):
    if not isinstance(items, list):
        return []
    selected = []
    for row in items:
        if not isinstance(row, dict):
            continue
        if str(row.get("source_id", "")).startswith("jargon:"):
            match = re.fullmatch(r"群内用语：(.+?)；含义：(.*)", str(row.get("text", "")), re.S)
            if not match or match[1].casefold() not in str(query).casefold():
                continue
            # These are unresolved generated guesses, not established meanings.
            if re.search(r"或者|可能指|更确切|更准确|或泛指|实为|实际上更", match[2]):
                continue
        selected.append(row)
    selected.sort(key=lambda row: (
        0 if row.get("subject_id") == speaker_id else
        2 if str(row.get("source_id", "")).startswith("jargon:") else 1
    ))
    return selected[:limit]
