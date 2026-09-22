"""Reduce identifiers in model-written reasoning before it is stored.

Why this exists
---------------
The bounded `decision_trace` record holds no free text at all. The reasoning
chain beside it -- `rationale`, `why_rejected`, `response_goal` -- does contain
prose, and prose written by a model that has just read a conversation can quote
names, handles and message text back. That text is wanted for training, so it
gets its own store with its own contract, and it is scrubbed on the way in.

What this is not
----------------
This is **best-effort reduction, not anonymisation**. Rule-based scrubbing cannot
catch a nickname spelled around a substitution, a name spelled with different
characters, or a fact that identifies someone without naming them. Treat a
scrubbed record as lower-risk, never as safe.

The primary control is upstream and stronger than any of this: the decision
prompt asks the model to cite `message_id`s and to never reproduce names, handles
or message text. A model that does not quote has nothing to scrub. This module is
the net under that, not the substitute for it.

Pseudonyms
----------
Every known entity becomes ``<ENT_xxxxxx>``, derived from an HMAC over a
per-installation salt. Two properties matter and pull against each other:

* **stable within the corpus** -- the same person is the same token in every
  record, so a learner can see that ENT_A replied to ENT_B and later ENT_B asked
  ENT_A a question. A per-record numbering would sever exactly that.
* **not reversible without the salt** -- a bare hash would not do: a QQ number has
  ten digits and is brute-forceable in seconds.

Rotating the salt severs the linkability deliberately, which is also how a
retention limit is enforced.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import asdict
from typing import Iterable, Mapping, Sequence

# Long digit runs are ids of every kind: QQ numbers, group numbers, message ids,
# timestamps, phone numbers. Fewer than five digits is ordinariness -- years,
# counts, "3 reasons" -- and scrubbing it would mangle the text for nothing.
ID_RUN = re.compile(r"\d{5,}")

URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

# Anything the model put in quotation marks is overwhelmingly a reproduction of
# what someone said. It is removed wholesale rather than scrubbed inside, because
# a partially preserved quote is still a quote.
QUOTED = re.compile(r"[「“『\"']([^「”』\"'\n]{1,60})[」”』\"']")

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def new_salt() -> bytes:
    return secrets.token_bytes(32)


def collect_identifiers(turn) -> list[str]:
    """Every name, handle and id this turn actually knows about.

    Deliberately derived from the turn's own snapshots rather than pattern-matched
    out of the prose: the people who are present are exactly the ones a rationale
    is most likely to name, and they are already enumerated here.

    Message *text* is excluded. It is not an identifier to swap for a token -- it
    is content, and content is handled by the quoting rule.
    """
    found: list[str] = []
    for snapshot in (*getattr(turn, "messages", ()), *getattr(turn, "background", ())):
        record = asdict(snapshot) if hasattr(snapshot, "__dataclass_fields__") else {}
        for key, value in record.items():
            if key == "text" or not isinstance(value, str):
                continue
            if len(value.strip()) >= 2:
                found.append(value.strip())
    author = str(getattr(turn, "author", "") or "").strip()
    if author:
        found.append(author)
    return [item for item in dict.fromkeys(found) if item]


def _token(value: str, salt: bytes) -> str:
    digest = hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"<ENT_{digest[:6].upper()}>"


def build_substitutions(identifiers: Iterable[str], salt: bytes) -> dict[str, str]:
    """Stable entity -> pseudonym map. Longest first so a full name is not
    shredded into a surname before the whole name can be replaced."""
    unique = {value for value in identifiers if isinstance(value, str) and value.strip()}
    ordered = sorted(unique, key=lambda value: (-len(value), value))
    return {value: _token(value, salt) for value in ordered}


def scrub(text: str, substitutions: Mapping[str, str]) -> str:
    """Best-effort identifier reduction. See the module docstring for the limits."""
    if not isinstance(text, str) or not text:
        return ""
    out = text
    out = URL.sub("<URL>", out)
    out = EMAIL.sub("<EMAIL>", out)
    out = QUOTED.sub("<QUOTE>", out)
    for original, token in substitutions.items():
        out = out.replace(original, token)
    out = ID_RUN.sub("<ID>", out)
    return out[:1200]
