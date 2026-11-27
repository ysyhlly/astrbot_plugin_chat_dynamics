"""Tests for Dynamics Horizons guidance and tool-offered feedback merging."""

from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.integrations import dynamics_horizons


def test_horizons_guidance_mentions_topic_inquiry_and_query_tool():
    assert "有趣" in dynamics_horizons.HORIZONS_DECISION_GUIDANCE
    assert "query_horizons_topics" in dynamics_horizons.HORIZONS_REPLY_GUIDANCE
    assert "待聊问题库" in dynamics_horizons.HORIZONS_REPLY_GUIDANCE


def test_delivered_merges_horizons_tool_context_into_feedback_payload():
    ingested: list[dict] = []

    class FakeHorizons:
        def horizons_capabilities(self):
            return {"schema_version": 1, "ready": True, "enabled": True}

        def get_horizons_context(self, **kwargs):
            return {"schema_version": 1, "matched": False}

        def ingest_dynamics_event(self, payload):
            ingested.append(payload)
            return {"accepted": True}

    fake = FakeHorizons()
    host = SimpleNamespace(
        _shutting_down=False,
        instance_id="default",
        context=SimpleNamespace(
            get_all_stars=lambda: {
                "h": SimpleNamespace(
                    name="astrbot_plugin_dynamics_horizons",
                    activated=True,
                    star_cls=fake,
                )
            }
        ),
    )
    runtime = SimpleNamespace(epoch=1)
    msg = SimpleNamespace(message_id="m_1")
    turn = SimpleNamespace(
        session_key="qq:GroupMessage:620537488",
        author="u_1",
        text="最近有什么有趣的话题吗",
        source_text="最近有什么有趣的话题吗",
        revision=1,
        epoch=1,
        messages=(msg,),
        nearby_context=(),
        background=(),
    )
    ev = SimpleNamespace(
        _horizons_tool_context_v1={
            "question_ids": ["q_tool_1", "q_tool_2"],
            "log_ids": ["log_tool_1"],
            "topics": ["多线叙事与演出过载"],
        }
    )
    item = SimpleNamespace(context=turn, events=(ev,), narrative_bundle=None)
    persona = SimpleNamespace(persona_id="anan")

    dynamics_horizons.begin(host, runtime, item, persona)
    dynamics_horizons.delivered(host, runtime, item, "最近在想追番是一口气看还是一周一集更好。", message_id="r_1")

    assert len(ingested) == 1
    payload = ingested[0]
    assert payload["matched"] is True
    assert payload["question_ids"] == ["q_tool_1", "q_tool_2"]
    assert payload["log_ids"] == ["log_tool_1"]
    assert payload["topics"] == ["多线叙事与演出过载"]
