from dataclasses import FrozenInstanceError

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.session_runtime import SessionRuntime
from astrbot_plugin_chat_dynamics.core.thread_router import ThreadRouter


def setup_dialogue():
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG())
    router = ThreadRouter(require_intense_dialogue=False)
    user = runtime.dag.add_message("u", "alice", "启动时提示错误", timestamp=1)
    router.route(runtime, user)
    bot = runtime.dag.add_message("b", "bot", "你用什么版本？", timestamp=2, reply_to_id="u")
    router.route(runtime, bot)
    return runtime, router


def test_question_accepts_low_semantic_answer_from_interlocutor():
    runtime, router = setup_dialogue()
    dialogue = runtime.active_dialogue
    assert dialogue.last_bot_was_question and dialogue.user_id == "alice"
    with pytest.raises(FrozenInstanceError):
        dialogue.user_id = "bystander"
    answer = runtime.dag.add_message("a", "alice", "1.21.4", timestamp=3)
    result = router.route(runtime, answer)
    assert result.bot_is_addressee
    assert result.parent_message_id == "b"
    assert "active_dialogue_answer" in result.evidence
    from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityRouter
    from astrbot_plugin_chat_dynamics.core.persona_engine import turn_is_addressed
    from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext
    assert AddressivityRouter().compute_addressivity(answer, runtime.dag).level.value == "strong"
    turn = TurnContext("room", "alice", answer.text,
        (MessageSnapshot("a", "alice", answer.text),), (), 0, 0, 3, False)
    assert turn_is_addressed(runtime, turn, 3)


@pytest.mark.parametrize("user,text,time,interruption", [
    ("bystander", "1.21.4", 3, None),
    ("alice", "1.21.4", 63, None),
    ("alice", "对了，今晚有人打游戏吗", 3, None),
    ("alice", "1.21.4", 4, ("other", "晚上打游戏吗？")),
    ("alice", "1.21.4", 4, ("alice", "1.20")),
])
def test_question_answer_has_bounded_first_response_scope(user, text, time, interruption):
    runtime, router = setup_dialogue()
    if interruption:
        runtime.dag.add_message("between", interruption[0], interruption[1], timestamp=3)
    answer = runtime.dag.add_message("a", user, text, timestamp=time)
    result = router.route(runtime, answer)
    assert "active_dialogue_answer" not in result.evidence


def test_explicit_human_recipient_wins_over_waiting_bot():
    runtime, router = setup_dialogue()
    answer = runtime.dag.add_message("a", "alice", "1.21.4", timestamp=3, mentioned_users=["other"])
    result = router.route(runtime, answer)
    assert result.addressee_ids == ["other"]
    assert not result.bot_is_addressee


def test_fragment_chain_resolves_original_human_and_reset_removes_view():
    runtime, router = setup_dialogue()
    tail = runtime.dag.add_message("tail", "bot", "具体是哪个小版本？", timestamp=2.5, reply_to_id="b")
    router.route(runtime, tail)
    assert runtime.active_dialogue.user_id == "alice"
    assert runtime.active_dialogue.last_user_message_id == "u"
    answer = runtime.dag.add_message("a", "alice", "1.21.4", timestamp=3)
    result = router.route(runtime, answer)
    assert result.parent_message_id == "tail"
    assert "active_dialogue_answer" in result.evidence
    runtime.reset_conversation_state()
    assert runtime.active_dialogue is None


def test_uncommitted_draft_cannot_create_dialogue():
    runtime = SessionRuntime("room", "room", "room", bot_id="bot", dag=ConversationDAG())
    runtime.dag.add_message("u", "alice", "帮我看看", timestamp=1)
    runtime.dag.add_message("draft", "bot", "什么版本？", timestamp=2, reply_to_id="u")
    assert runtime.active_dialogue is None


def test_active_dialogue_golden_runs_both_consumers():
    import json
    from .test_routing_evaluator import ROOT, load_evaluator
    cases = json.loads((ROOT / "tests/fixtures/active_dialogue_golden.json").read_text(encoding="utf-8"))
    report = load_evaluator().evaluate(cases)
    assert report["failed"] == 0
    assert report["routing_metrics"]["parent"]["accuracy"] == 1.0
