"""The decision and expression models see the same member-bound state."""

import copy
import json
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.character_view import speaker_snapshot
from astrbot_plugin_chat_dynamics.core.narrative import TurnContextBundle
from astrbot_plugin_chat_dynamics.core.narrative_projection import reply_material, filter_reply_material


def material():
    return {
        "scope": {"platform": "qq", "umo": "qq:GroupMessage:1", "persona_id": "a", "instance_id": "a"},
        "speaker_id": "u1",
        "captured_at": 1600,
        "snapshot": {
            "humanlike": {
                "preferences": [{"id": "p", "status": "active", "text": "喜欢读书", "origin": "personal"}],
                "goals": [{"id": "g", "status": "planned", "text": "读一章", "expires_at": 3000}],
                "moods": [
                    {"subject_id": "u1", "text": "轻松", "updated_at": 1000, "expires_at": 2500},
                    {"subject_id": "u2", "text": "不满", "updated_at": 1000, "expires_at": 2500},
                ],
                "nicknames": [
                    {"subject_id": "u1", "nickname": "小鱼", "status": "rejected"},
                    {"subject_id": "u2", "nickname": "他人的名字", "status": "active"},
                ],
            }
        },
    }


def test_same_filtered_state_in_decision_and_reply():
    data = material()
    original = copy.deepcopy(data)
    decision = TurnContextBundle("t", json.dumps(data)).decision_context()
    reply = reply_material(data)
    assert decision["humanlike"] == reply["snapshot"]["humanlike"]
    result = reply["snapshot"]["humanlike"]
    assert result["moods"] == [{"subject_id": "u1", "text": "轻松", "strength": 0.5}]
    assert result["nicknames"] == [{"subject_id": "u1", "nickname": "小鱼", "status": "rejected"}]
    assert data == original


def test_expired_moods_and_goals_and_unrelated_local_interests_are_absent():
    data = material()
    data["snapshot"]["humanlike"]["preferences"].append(
        {
            "id": "other",
            "text": "另一个人的话题",
            "status": "active",
            "origin": "conversation",
            "participant_ids": ["u2"],
        }
    )
    result = speaker_snapshot(data["snapshot"], "u1", 4000)["humanlike"]
    assert "goals" not in result and "moods" not in result
    assert len(result["preferences"]) == 1
    assert "nicknames" not in speaker_snapshot(data["snapshot"], "", 4000)["humanlike"]


def test_humanlike_material_passes_member_boundary(monkeypatch):
    from astrbot_plugin_chat_dynamics.core import narrative_projection as projection

    data = material()
    seen = []

    def filter_material(host, event, payload, **kwargs):
        seen.extend(payload["context_items"])
        return {"context_items": []}, SimpleNamespace(available=True, disabled=False)

    monkeypatch.setattr(projection.member_boundary, "filter_material", filter_material)
    result, _ = filter_reply_material(None, None, reply_material(data), data)
    assert any(row.get("subject_id") == "u1" and row["text"] == "小鱼" for row in seen)
    assert not any(result["snapshot"]["humanlike"].values())
