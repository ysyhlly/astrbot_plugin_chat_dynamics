"""Replay interleaved group turns through production history and Jev state builders."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_chat_dynamics.core.graph import ConversationDAG
from astrbot_plugin_chat_dynamics.core.jev_decision import build_state, decision_from_answers, target_options
from astrbot_plugin_chat_dynamics.core.persona_engine import refresh_topic_context, snapshot_turn
from .test_persona_explicit_context import _runtime, _snapshot


def route(node, topic="task", confidence=.9, **extra):
    node.metadata["routing"] = {
        "topic_id": topic, "topic_confidence": confidence, "topic_ambiguous": False,
        "topic_status": "committed", "evidence": ["topic_jev_match"], **extra,
    }
    return node


CASES = json.loads((Path(__file__).parent / "fixtures/conversation_context_replay.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_interleaved_group_replay(case):
    dag = ConversationDAG("replay")
    for row in case["messages"]:
        node = dag.add_message(row["id"], row["author"], row["text"], timestamp=row["time"],
                               reply_to_id=row.get("reply_to"), mentioned_users=row.get("mentions", []))
        if "routing" in row:
            route(node, **row["routing"])
        if "inferred_parent" in row:
            assert dag.link_inferred_reply(node.msg_id, row["inferred_parent"], row["parent_confidence"],
                                           "replay_semantic_reply")
    current = dag.get_node(case["current"])
    item = _snapshot(_runtime(dag), current)
    state = build_state(item.context)
    background = state["conversation"]["background"]
    assert [message["message_id"] for message in background] == case["expected_background"]
    assert all(message["context_evidence"] for message in background)
    assert list(target_options(item.context)) == [current.msg_id]
    assert [message["message_id"] for message in state["conversation"]["messages"]] == [current.msg_id]
    assert state["conversation"]["text"] == current.text
    assert len(json.dumps(state, ensure_ascii=False)) <= 12000
    # Retrieval never converts a human recipient or an ignored turn into a reply.
    answers = {"action": {"type": "choice", "choice": "ignore", "confidence": .9},
               "join": {"type": "noul", "noul": .1}}
    assert decision_from_answers(replace(item.context, explicit=False), answers).action == "ignore"


@pytest.mark.parametrize("confidence", [.1, .71, float("nan"), float("inf"), True, "0.9"])
def test_inference_requires_finite_numeric_confidence(confidence):
    dag = ConversationDAG("replay")
    parent = dag.add_message("parent", "alice", "缺失的条件", timestamp=10.)
    current = dag.add_message("current", "bob", "继续这个问题", timestamp=20.)
    assert dag.link_related(current.msg_id, parent.msg_id, kind="inferred_reply")
    current.metadata["edge_metadata"] = {parent.msg_id: {"confidence": confidence, "reason": "test"}}
    assert _snapshot(_runtime(dag), current).context.background == ()


def test_accepted_inference_respects_the_recorded_configured_threshold():
    dag = ConversationDAG("replay")
    parent = dag.add_message("parent", "alice", "配置过的判断条件", timestamp=10.)
    current = dag.add_message("current", "bob", "继续", timestamp=20.)
    current.metadata["routing"] = {"parent_threshold": .6}
    assert dag.link_inferred_reply(current.msg_id, parent.msg_id, .66, "configured_threshold")
    history = _snapshot(_runtime(dag), current).context.background
    assert [m.message_id for m in history] == [parent.msg_id]
    assert history[0].context_evidence[0].confidence == .66


def test_explicit_ancestry_reached_through_a_guess_keeps_its_inferred_selection_basis():
    dag = ConversationDAG("replay")
    root = dag.add_message("root", "alice", "原问题", timestamp=10.)
    parent = dag.add_message("parent", "carol", "引用原问题", timestamp=12., reply_to_id=root.msg_id)
    current = dag.add_message("current", "bob", "继续", timestamp=20.)
    assert dag.link_inferred_reply(current.msg_id, parent.msg_id, .9, "semantic_reply")
    history = _snapshot(_runtime(dag), current).context.background
    assert [m.message_id for m in history] == [root.msg_id, parent.msg_id]
    proof = history[0].context_evidence[0]
    assert proof.relation == "reply" and "inferred_ancestry" in proof.evidence
    assert proof.priority == history[1].context_evidence[0].priority


@pytest.mark.parametrize("extra", [
    {"parent_ambiguous": True}, {"parent_message_id": "different"},
    {"parent_confidence": .4}, {"parent_threshold": .95},
])
def test_inference_rejects_stale_or_ambiguous_routing(extra):
    dag = ConversationDAG("replay")
    parent = dag.add_message("parent", "alice", "问题背景", timestamp=10.)
    current = dag.add_message("current", "bob", "继续这个问题", timestamp=20.)
    assert dag.link_inferred_reply(current.msg_id, parent.msg_id, .9, "semantic_reply")
    current.metadata["routing"] = extra
    assert _snapshot(_runtime(dag), current).context.background == ()


def test_possible_parent_without_an_accepted_edge_is_not_history():
    dag = ConversationDAG("replay")
    dag.add_message("candidate", "alice", "可能相似的问题", timestamp=10.)
    current = dag.add_message("current", "bob", "完全不同的需求", timestamp=20.)
    current.metadata["routing"] = {"possible_parent": "candidate", "parent_message_id": "candidate",
                                   "parent_confidence": .99, "parent_candidates": [[.99, "candidate"]]}
    assert _snapshot(_runtime(dag), current).context.background == ()


@pytest.mark.parametrize("extra", [{"topic_ambiguous": True}, {"topic_status": "pending"},
                                  {"topic_status": "unformed"}, {"topic_confidence": .3}])
def test_uncertain_topic_does_not_import_other_members(extra):
    dag = ConversationDAG("replay")
    route(dag.add_message("other", "alice", "别人的补充", timestamp=10.))
    current = route(dag.add_message("current", "bob", "当前问题", timestamp=20.), **extra)
    assert _snapshot(_runtime(dag), current).context.background == ()


def test_quote_outside_recent_candidate_window_is_kept():
    dag = ConversationDAG("replay")
    quote = dag.add_message("old-quote", "alice", "必须保留的原问题", timestamp=1.)
    for index in range(100):
        dag.add_message(f"noise-{index}", "eve", "无关闲聊", timestamp=200. + index)
    current = dag.add_message("current", "bob", "请解释这条", timestamp=400., reply_to_id=quote.msg_id)
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == [quote.msg_id]


def test_future_messages_cannot_crowd_out_queued_turn_context():
    dag = ConversationDAG("replay")
    route(dag.add_message("constraint", "alice", "不要联网", timestamp=10.))
    current = route(dag.add_message("current", "bob", "继续方案", timestamp=20.))
    for index in range(100):
        route(dag.add_message(f"future-{index}", "eve", "未来消息", timestamp=20. if index == 0 else 30. + index))
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == ["constraint"]


def test_provenance_and_source_are_detached_from_later_routing_changes():
    dag = ConversationDAG("replay")
    parent = dag.add_message("parent", "alice", "原始补充条件", timestamp=10.)
    current = dag.add_message("current", "bob", "继续这个问题", timestamp=20.)
    assert dag.link_inferred_reply(current.msg_id, parent.msg_id, .91, "semantic_reply")
    item = _snapshot(_runtime(dag), current)
    before = item.context.learning_payload()
    assert before["background"][0]["context_evidence"][0]["confidence"] == .91
    assert before["messages"][0]["semantics"]["parent_confidence"] == .91
    dag.unlink_inferred_reply(current.msg_id)
    parent.text = "已修改的消息"
    assert item.context.learning_payload() == before


def test_topic_and_recipient_agreement_deduplicates_the_same_supplement():
    dag = ConversationDAG("replay")
    route(dag.add_message("constraint", "alice", "仅支持离线环境", timestamp=10.))
    current = route(dag.add_message("current", "bob", "方案怎么改？", timestamp=20., mentioned_users=["alice"]))
    item = _snapshot(_runtime(dag), current)
    assert len(item.context.background) == 1
    assert {proof.relation for proof in item.context.background[0].context_evidence} >= {"mention", "same_topic"}


def test_context_and_state_budgets_keep_quote_ahead_of_speaker_noise():
    dag = ConversationDAG("replay")
    quote = dag.add_message("question", "alice", "最重要的约束" + "x" * 1200, timestamp=1.)
    for index in range(14):
        dag.add_message(f"same-{index}", "bob", "杂项" + "x" * 1200, timestamp=10. + index)
    current = dag.add_message("current", "bob", "请回答", timestamp=30., reply_to_id=quote.msg_id)
    item = _snapshot(_runtime(dag), current)
    assert "question" in {m.message_id for m in item.context.background}
    assert sum(len(m.text) for m in item.context.background) <= 6000
    state = build_state(item.context, max_chars=4500)
    assert "question" in {m["message_id"] for m in state["conversation"]["background"]}
    assert len(json.dumps(state, ensure_ascii=False)) <= 4500
    assert item.context.learning_payload()["background"][0]["text"] == quote.text


def test_history_count_is_bounded_and_output_stays_chronological():
    dag = ConversationDAG("replay")
    for index in range(30):
        route(dag.add_message(f"constraint-{index}", "alice", "条件", timestamp=float(index)))
    current = route(dag.add_message("current", "bob", "继续", timestamp=40.))
    history = _snapshot(_runtime(dag), current).context.background
    assert len(history) == 15
    assert [m.message_id for m in history] == [f"constraint-{index}" for index in range(15, 30)]


def test_quote_recipient_keeps_prior_conditions_without_topic_routing():
    dag = ConversationDAG("replay")
    dag.add_message("constraint", "alice", "最多一百字", timestamp=10.)
    quote = dag.add_message("question", "alice", "请写封邮件", timestamp=12.)
    current = dag.add_message("current", "bob", "帮她写一下", timestamp=20., reply_to_id=quote.msg_id)
    history = _snapshot(_runtime(dag), current).context.background
    assert [m.message_id for m in history] == ["constraint", "question"]


def test_platform_quote_wins_over_contradictory_inferred_parent_metadata():
    dag = ConversationDAG("replay")
    quote = dag.add_message("question", "alice", "原问题", timestamp=10.)
    dag.add_message("guess", "eve", "无关猜测", timestamp=12.)
    current = dag.add_message("current", "bob", "继续", timestamp=20., reply_to_id=quote.msg_id)
    current.metadata["routing"] = {"parent_message_id": "guess", "parent_confidence": .9}
    item = _snapshot(_runtime(dag), current)
    assert [m.message_id for m in item.context.background] == [quote.msg_id]
    semantics = item.context.messages[0].semantics
    assert semantics.parent_message_id == quote.msg_id and semantics.parent_confidence == 1.0


def test_long_history_keeps_opening_and_final_constraints_in_both_budgets():
    dag = ConversationDAG("replay")
    source = "请准备部署方案" + "x" * 3000 + "必须离线且不能使用 GPU"
    quote = dag.add_message("question", "alice", source, timestamp=10.)
    current = dag.add_message("current", "bob", "按要求继续", timestamp=20., reply_to_id=quote.msg_id)
    item = _snapshot(_runtime(dag), current)
    for text in (item.context.background[0].text,
                 build_state(item.context, max_chars=3500)["conversation"]["background"][0]["text"]):
        assert text.startswith("请准备部署方案") and text.endswith("必须离线且不能使用 GPU")
    assert item.context.learning_payload()["background"][0]["text"] == source


def test_low_confidence_recipient_without_other_evidence_does_not_import_history():
    dag = ConversationDAG("replay")
    dag.add_message("other", "alice", "另一个人的请求", timestamp=10.)
    current = dag.add_message("current", "bob", "你好", timestamp=20.)
    current.metadata["routing"] = {"addressee_ids": ["alice"], "addressee_confidence": .5}
    assert _snapshot(_runtime(dag), current).context.background == ()


def test_same_topic_bot_addressed_supplement_from_another_member_is_kept():
    dag = ConversationDAG("replay")
    route(dag.add_message("question", "alice", "怎么部署？", timestamp=10., mentioned_users=["bot-42"]))
    route(dag.add_message("constraint", "bob", "服务器不能联网", timestamp=12., mentioned_users=["bot-42"]))
    current = route(dag.add_message("current", "alice", "给个安装步骤", timestamp=20., mentioned_users=["bot-42"]))
    history = _snapshot(_runtime(dag), current).context.background
    assert [m.message_id for m in history] == ["question", "constraint"]


def test_shared_bot_recipient_without_topic_does_not_connect_two_requests():
    dag = ConversationDAG("replay")
    dag.add_message("other", "bob", "帮我写封邮件", timestamp=10., mentioned_users=["bot-42"])
    current = dag.add_message("current", "alice", "帮我选显卡", timestamp=20., mentioned_users=["bot-42"])
    assert _snapshot(_runtime(dag), current).context.background == ()


@pytest.mark.parametrize("author", ["alice", "bob"])
def test_known_participant_side_exchange_to_unrelated_member_is_excluded(author):
    dag = ConversationDAG("replay")
    quote = route(dag.add_message("question", "alice", "工具怎么安装？", timestamp=10.))
    route(dag.add_message("private", author, "账号配置私信给你", timestamp=12., mentioned_users=["dave"]))
    current = route(dag.add_message("current", "bob", "给个安装步骤", timestamp=20., reply_to_id=quote.msg_id))
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == ["question"]


@pytest.mark.parametrize("link", ["mention", "reply"])
def test_other_topic_supplement_to_an_anchor_is_excluded(link):
    dag = ConversationDAG("replay")
    quote = route(dag.add_message("question", "alice", "怎么部署工具？", timestamp=10.), topic="deployment")
    kwargs = {"mentioned_users": ["alice"]} if link == "mention" else {"reply_to_id": quote.msg_id}
    noise = route(dag.add_message("dinner", "eve", "今晚吃火锅吗？", timestamp=12., **kwargs), topic="dinner")
    assert noise.edge_kinds[quote.msg_id] == link
    current = route(dag.add_message("current", "bob", "继续给安装步骤", timestamp=20., reply_to_id=quote.msg_id),
                    topic="deployment")
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == [quote.msg_id]


def test_mention_to_an_anchor_without_a_topic_is_not_an_actual_supplement():
    dag = ConversationDAG("replay")
    quote = dag.add_message("question", "alice", "部署工具", timestamp=10.)
    dag.add_message("mention", "eve", "吃火锅吗？", timestamp=12., mentioned_users=["alice"])
    current = dag.add_message("current", "bob", "继续方案", timestamp=20., reply_to_id=quote.msg_id)
    assert [m.message_id for m in _snapshot(_runtime(dag), current).context.background] == [quote.msg_id]


def test_later_platform_quote_survives_three_earlier_inferred_proofs_and_budget_pressure():
    dag = ConversationDAG("replay")
    quote = dag.add_message("quote", "alice", "只能离线安装" + "x" * 600, timestamp=1.)
    mention = dag.add_message("mention", "carol", "最近发言" + "y" * 600, timestamp=2.)
    nodes = []
    for i in range(4):
        node = dag.add_message(f"current-{i}", "bob", "继续这个问题", timestamp=10. + i,
                               reply_to_id=quote.msg_id if i == 3 else None)
        if i < 3:
            assert dag.link_inferred_reply(node.msg_id, quote.msg_id, .9, "semantic_reply")
        nodes.append(node)
    assert dag.link_related(nodes[-1].msg_id, mention.msg_id, kind="mention")
    parsed = [SimpleNamespace(message_id=n.msg_id, sender_id=n.user_id, text=n.text,
                              reply_to_id=n.reply_to_id, media_component_types=[]) for n in nodes]
    result = SimpleNamespace(user_id="bob", consolidated_text="\n".join(n.text for n in nodes), raw_events=parsed,
                             start_time=10., metadata={}, last_event=parsed[-1])
    turn = snapshot_turn(_runtime(dag), result, [n.msg_id for n in nodes], parsed, True, {}, False).context
    proofs = turn.background[0].context_evidence
    assert len(proofs) == 3 and any(proof.relation == "reply" and proof.priority == 0 for proof in proofs)
    state = build_state(turn, max_chars=4750)
    assert "quote" in {m["message_id"] for m in state["conversation"]["background"]}
    assert len(json.dumps(state, ensure_ascii=False)) <= 4750


def test_long_guidance_does_not_evict_the_current_request_and_critical_quote():
    dag = ConversationDAG("replay")
    quote = dag.add_message("quote", "alice", "只能离线安装，不要联网下载", timestamp=10.)
    current = dag.add_message("current", "bob", "请解释" + "x" * 3996 + "末尾条件", timestamp=20., reply_to_id=quote.msg_id)
    turn = _snapshot(_runtime(dag), current).context
    state = build_state(turn, persona_prompt="p" * 1200, decision_prompt="开头规则" + "g" * 3992 + "末尾规则")
    assert [m["message_id"] for m in state["conversation"]["background"]] == [quote.msg_id]
    assert "不要联网下载" in state["conversation"]["background"][0]["text"]
    assert state["conversation"]["text"].startswith("请解释") and state["conversation"]["text"].endswith("末尾条件")
    assert state["decision_prompt"].startswith("开头规则") and state["decision_prompt"].endswith("末尾规则")
    assert len(json.dumps(state, ensure_ascii=False)) <= 12000


def test_topic_refresh_keeps_the_original_watermark_and_immutable_decision_context():
    dag = ConversationDAG("replay")
    route(dag.add_message("constraint", "alice", "目标机器不能联网", timestamp=10.), topic="deployment")
    current = dag.add_message("current", "bob", "解释安装步骤", timestamp=20.)
    item = _snapshot(_runtime(dag), current)
    before = item.context.learning_payload()
    for mid, ts in [("late-backdated", 15.), ("late-equal", 20.), ("late-future", 25.)]:
        route(dag.add_message(mid, "carol", "等待期间到达的消息", timestamp=ts), topic="deployment")
    route(current, topic="deployment", semantic_topic_decision=True)
    refreshed = refresh_topic_context(_runtime(dag), item)
    assert refreshed is not item and item.context.learning_payload() == before
    assert [m.message_id for m in refreshed.context.background] == ["constraint"]
    assert refreshed.context.messages[0].semantics.topic_id == "deployment"
    assert refreshed.context.text == item.context.text
    assert refreshed.context.allowed_ids == {"current", "constraint"}
    assert refreshed.platform_message_ids == item.platform_message_ids
    assert refreshed.context.epoch == item.context.epoch and refreshed.context.revision == item.context.revision
