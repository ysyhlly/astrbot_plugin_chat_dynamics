"""The record-bearing prompt is staged: written, parsed for, not yet sent.

Asking a judge for six more fields is not a neutral addition to a prompt. It costs
attention, and a prompt that asks for a rationale is one that reasons differently
from a prompt that does not. Until there is a baseline to compare against, the
first batch of new data would be indistinguishable from a prompt change.

So the decision path keeps saying exactly what it said before, and the switch is a
flag read with `getattr(..., False)` -- meaning that with no config entry at all
behaviour is unchanged.

These tests pin both halves: the prompt in force asks for the six decision fields
and nothing else, and the parser accepts either shape regardless of which prompt
was sent. That decoupling is what makes turning the switch later a one-line change
instead of a migration.
"""
import json
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.turn_decision import (
    DECISION_INSTRUCTIONS, DECISION_INSTRUCTIONS_RECORDED, RECORD_SECTION,
    TurnContext, TurnDecision, decision_instructions,
)

RECORD_FIELDS = {"reason_category", "rationale", "confidence",
                 "evidence", "alternatives", "assessment"}


def declared(prompt: str) -> set[str]:
    """Field names the prompt actually declares.

    A bare substring test is wrong here: the plain prompt's own prose says
    "Routing confidence is evidence, not certainty", so `confidence` and `evidence`
    appear in it as English words. What distinguishes a requested field from a
    word is that a field is declared at the start of a line.
    """
    return {line.split(":", 1)[0].strip() for line in prompt.splitlines()
            if line and not line[0].isspace() and ":" in line}


def _turn() -> TurnContext:
    return TurnContext("group", "user", "text", (), (), 1, 1, 0, True)


def _six_fields() -> dict:
    return {"action": "ignore", "state": "observing", "target_message_ids": [],
            "response_goal": "goal", "length": "brief", "reason_code": "quiet"}


def test_the_prompt_in_force_asks_for_nothing_new():
    assert decision_instructions() is DECISION_INSTRUCTIONS
    assert decision_instructions(False) is DECISION_INSTRUCTIONS
    asked = declared(DECISION_INSTRUCTIONS)
    assert not (RECORD_FIELDS & asked), f"prompt in force must not ask for {sorted(RECORD_FIELDS & asked)}"
    assert {"action", "state", "length", "reason_code"} <= asked


def test_the_staged_prompt_does_ask_for_the_record():
    assert DECISION_INSTRUCTIONS_RECORDED == DECISION_INSTRUCTIONS + RECORD_SECTION
    assert decision_instructions(True) is DECISION_INSTRUCTIONS_RECORDED
    asked = declared(DECISION_INSTRUCTIONS_RECORDED)
    assert RECORD_FIELDS <= asked, f"staged prompt is missing {sorted(RECORD_FIELDS - asked)}"


def test_the_parser_accepts_either_shape_whichever_prompt_was_sent():
    turn = _turn()
    plain = TurnDecision.parse(json.dumps(_six_fields()), turn)
    assert plain.reason_category == "" and plain.rationale == ""
    assert plain.confidence == () and plain.evidence == ()

    full = {**_six_fields(),
            "reason_category": "direct_address",
            "rationale": "the message names the bot and asks a direct question.",
            "confidence": {"action": 0.8, "state": 0.6, "length": 0.5},
            "evidence": [{"message_id": "m", "cues": ["mention"], "weight": 0.9}],
            "alternatives": [{"action": "ignore", "why_rejected": "it names the bot"}],
            "assessment": {"addressee": "bot", "topic": "unknown",
                           "completeness": "complete", "ambiguity": "low"}}
    recorded = TurnDecision.parse(json.dumps(full), turn)
    assert recorded.reason_category == "direct_address"
    assert recorded.confidence == (("action", 0.8), ("state", 0.6), ("length", 0.5))
    assert recorded.assessment[0] == ("addressee", "bot")
    # The six decision fields mean the same thing in both shapes.
    assert (plain.action, plain.state, plain.length) == ("ignore", "observing", "brief")


def test_a_stray_field_is_still_rejected():
    turn = _turn()
    try:
        TurnDecision.parse(json.dumps({**_six_fields(), "surprise": 1}), turn)
    except ValueError as exc:
        assert str(exc) == "decision_fields"
    else:
        raise AssertionError("an unlisted field must not be silently absorbed")
