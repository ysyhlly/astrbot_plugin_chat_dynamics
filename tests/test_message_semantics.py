from dataclasses import asdict
import json
import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.message_semantics import describe_message
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext, TurnDecision, reply_prompt, decision_prompt


def test_reply_and_mentions_preserve_distinct_evidence():
    dag = ConversationDAG()
    dag.add_message("a", "alice", "hello", timestamp=1)
    node = dag.add_message("b", "bob", "你怎么看？", timestamp=2, reply_to_id="a", mentioned_users=["carol"])
    result = describe_message(node, dag, "bot")
    assert result.sender_id == "bob"
    assert result.recipient_ids == ("carol",)
    assert result.quoted_author_id == "alice"
    assert result.mentioned_user_ids == ("carol",)
    assert result.certainty == "explicit"
    assert result.intent == "question"


def test_missing_reply_resolves_without_cross_session_lookup():
    dag = ConversationDAG()
    node = dag.add_message("b", "bob", "回复", timestamp=2, reply_to_id="a")
    other = ConversationDAG()
    other.add_message("a", "stranger", "hello", timestamp=1)
    assert describe_message(node, dag).certainty == "unknown"
    dag.add_message("a", "alice", "hello", timestamp=1)
    assert describe_message(node, dag).recipient_ids == ("alice",)


def test_mentions_unknown_and_semantic_guesses():
    dag = ConversationDAG()
    a = dag.add_message("a", "alice", "代码接口报错了", timestamp=1)
    assert describe_message(a, dag).basis == "unknown"
    b = dag.add_message("b", "bob", "代码接口为什么报错了", timestamp=2)
    b.metadata["routing"] = {"addressee_ids": ["alice"], "addressee_confidence": 0.5}
    assert describe_message(b, dag).certainty == "possible"
    c = dag.add_message("c", "carol", "来看看", timestamp=3, mentioned_users=["bot", "alice", "bot"])
    assert describe_message(c, dag).recipient_ids == ("bot", "alice")
    d = dag.add_message("d", "bot", "收到", timestamp=4)
    assert describe_message(d, dag, "bot").sender_is_bot


def test_both_model_prompts_include_attribution():
    dag = ConversationDAG()
    node = dag.add_message("m", "alice", "你好", mentioned_users=["bot"])
    semantics = describe_message(node, dag, "bot")
    snapshot = MessageSnapshot("m", "alice", "", semantics=semantics)
    turn = TurnContext("session", "alice", "你好", (snapshot,), (snapshot,), 0, 0, 1, True)
    for prompt in (decision_prompt(turn, "observing", {}), reply_prompt(turn, TurnDecision.fallback(turn, "test"))):
        data = json.loads(prompt)["conversation"]
        for key in ("messages", "background"):
            assert data[key][0]["semantics"] == json.loads(json.dumps(asdict(semantics)))


def test_merged_mentions_do_not_change_fragment_attribution():
    dag = ConversationDAG()
    node = dag.add_message("m", "alice", "补充", mentioned_users=["bot"], metadata={"actual_mentions": []})
    assert describe_message(node, dag).recipient_ids == ()


@pytest.mark.asyncio
async def test_native_hook_and_exclusive_reply_receive_semantics():
    from types import SimpleNamespace
    from astrbot_plugin_chat_dynamics.tests.test_plugin_lifecycle import MockEvent, _plugin, _session_key
    from astrbot_plugin_chat_dynamics.core.vibe_analyzer import GroupChatMode

    plugin = _plugin()
    key = _session_key("semantics")
    runtime = plugin._get_or_create_runtime(key, group_id="semantics", umo=key, bot_id="bot")
    node = runtime.dag.add_message("m", "alice", "看看接口？", mentioned_users=["bot"])
    event = MockEvent("看看接口？", group_id="semantics", message_id="m", self_id="bot")
    request = SimpleNamespace(prompt="看看接口？")
    await plugin.on_llm_request(event, request)
    assert '"sender_id": "alice"' in request.prompt
    assert '"recipient_ids": ["bot"]' in request.prompt
    captured = []

    async def generate(*args, **kwargs):
        captured.append(json.loads(kwargs["text"]))
        return ""

    plugin._run_native_reply = generate
    await plugin._dispatch_bot_response(runtime, node, GroupChatMode.CHILL_FADE, event, runtime.revision)
    assert captured[0]["message_semantics"][0]["recipient_ids"] == ["bot"]
    await plugin.terminate()


def test_routing_preserves_subject_and_quote_separately():
    dag = ConversationDAG()
    dag.add_message("q", "alice", "a quoted fact", timestamp=1)
    node = dag.add_message("m", "bob", "what do you think?", timestamp=2, reply_to_id="q",
        metadata={"routing": {"addressee_ids": ["bot"], "addressee_confidence": 0.9,
        "parent_message_id": "q", "subject_user_ids": ["alice"], "subject_is_bot": False,
        "bot_is_addressee": True, "bot_addressee_confidence": 0.9, "topic_id": "t1"}})
    result = describe_message(node, dag, "bot")
    assert result.recipient_ids == ("bot",)
    assert result.quoted_author_id == "alice"
    assert result.subject_user_ids == ("alice",)
    assert result.certainty == "probable"
    assert result.topic_id == "t1"


def test_addressivity_subject_vocative_and_routed_quote():
    from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
    router = AddressivityRouter(bot_id="bot", bot_names=["bot"])
    dag = ConversationDAG()
    assert not router._name_mentioned_in_text("bot", "bot is broken again")
    assert not router._name_mentioned_in_text("bot", "I think bot is useful")
    assert router._name_mentioned_in_text("bot", "bot, can you help?")
    dag.add_message("q", "alice", "a fact", timestamp=1)
    node = dag.add_message("m", "bob", "what do you think?", timestamp=2, reply_to_id="q",
        metadata={"routing": {"bot_is_addressee": True, "bot_addressee_confidence": 0.9}})
    assert router.compute_addressivity(node, dag).is_bot_targeted
    node.mentioned_users = ["carol"]
    assert not router.compute_addressivity(node, dag).is_bot_targeted
    node.mentioned_users = []
    node.metadata["routing"]["ambiguous"] = True
    assert not router.compute_addressivity(node, dag).is_bot_targeted


def test_active_interlocutor_confidence_and_explicit_mentions():
    from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
    router = AddressivityRouter(bot_id="bot")
    dag = ConversationDAG()
    node = dag.add_message("m", "bob", "and then?", timestamp=2,
        metadata={"routing": {"bot_is_addressee": True, "bot_addressee_confidence": 0.76,
                              "addressee_ids": ["bot"], "addressee_confidence": 0.76}})
    assert router.compute_addressivity(node, dag).is_bot_targeted
    assert describe_message(node, dag, "bot").certainty == "probable"
    node.mentioned_users = ["alice"]
    assert not router.compute_addressivity(node, dag).is_bot_targeted
