import json
from astrbot_plugin_chat_dynamics.core.annotation_draft import build_batch, build_batches, build_prompt, parse_drafts
from astrbot_plugin_chat_dynamics.tests.test_annotation_draft import node, reply, reply_for


def test_identity_and_mentions_are_exact_and_shared():
    nodes = [node("a", "hello", user="12345678a", mentions=["12345678b"]),
             node("b", "hello", user="12345678b", mentions=["bot", "12345678a"], reply_to="a")]
    batch, _ = build_batch(nodes, {}, bot_id="bot")
    assert batch[0]["user"] != batch[1]["user"]
    assert batch[0]["mentions_bot"] is False
    assert batch[1]["mentions_bot"] is True
    assert batch[0]["mentioned_users"] == [batch[1]["user"]]
    assert batch[1]["quoted_author"] == batch[0]["user"]
    assert batch[1]["mentioned_users"][0] == "BOT"


def test_unknown_identity_and_external_quote():
    n = node("a", "hello", user="", reply_to="external")
    n.metadata["quoted_author_id"] = "external-author"
    batch, _ = build_batch([n], {})
    assert batch[0]["user"] is None
    assert batch[0]["mentions_bot"] is None
    assert batch[0]["quoted_author"] == "U1"


def test_sixty_long_chinese_messages_are_complete_bounded_batches():
    nodes = [node(str(i), "中文消息" * 125, reply_to=str(i-1) if i else "") for i in range(60)]
    batches, stats = build_batches(nodes, {}, limit=60, bot_id="bot")
    assert len(batches) > 1
    assert stats["asked"] == 60
    asked = []
    for batch in batches:
        system, prompt = build_prompt(batch)
        assert len(system) + len(prompt) <= 8000
        payload = json.loads(prompt[prompt.index("{"):prompt.rindex("}")+1])
        assert payload["messages"] == batch
        ids = {r["msg_id"] for r in batch}
        for r in batch:
            if r["draft_this"]:
                asked.append(r["msg_id"])
                assert r["text"] == "中文消息" * 125
                assert not r["quotes"] or r["quotes"] in ids
    assert asked == [str(i) for i in range(60)]


def test_diagnostics_distinguish_missing_duplicate_external_and_invalid():
    batch, _ = build_batch([node(str(i), "hello") for i in range(4)], {}, limit=4)
    result = parse_drafts(reply(reply_for("0"), reply_for("0"), reply_for("9"),
                                reply_for("1", confidence=2), reply_for("2", confidence=float("nan"))),
                          batch, diagnostics=True)
    assert list(result["drafts"]) == ["0"]
    assert result["missing"] == ["3"]
    assert result["duplicate"] == ["0"]
    assert result["out_of_window"] == ["9"]
    assert result["invalid_confidence"] == ["1", "2"]


def test_omission_never_becomes_negative_label():
    batch, _ = build_batch([node("a", "hello")], {})
    result = parse_drafts('{"rows": []}', batch, diagnostics=True)
    assert result["drafts"] == {}
    assert result["missing"] == ["a"]
