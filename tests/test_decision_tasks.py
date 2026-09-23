import json
import pytest
from types import SimpleNamespace

from astrbot_plugin_chat_dynamics.core.decision_tasks import (
    answer_uncertainty, teacher_answers, turn_questions, turn_from_answers,
)
from astrbot_plugin_chat_dynamics.core.integrations.laya import LayaClient
from astrbot_plugin_chat_dynamics.core.turn_decision import MessageSnapshot, TurnContext


def test_multi_target_labels_are_preserved_and_unselected_targets_not_injected():
    turn = TurnContext('room', 'user', '请回答前面两条',
        (MessageSnapshot('m1', 'u', '第一条'), MessageSnapshot('m2', 'u', '第二条')),
        (MessageSnapshot('old', 'u', '背景'),), 1, 1, 0, True)
    questions, candidates = turn_questions(turn)
    labels = {'join': True, 'action': 'reply', 'state': 'focused', 'length': 'normal',
              'reply_length': 'short', 'recipient_choice': 'u', 'reason': 'addressed_request',
              'target.0': True, 'target.1': True, 'target.2': False}
    import json
    answers = teacher_answers(json.dumps(labels), questions)
    decision = turn_from_answers(turn, answers, candidates)
    assert decision.target_message_ids == ('m1', 'm2')
    assert decision.length == 'brief' and '一至三句话' in decision.response_goal
    labels.update({'target.0': False, 'target.1': False})
    answer = turn_from_answers(turn, teacher_answers(json.dumps(labels), questions), candidates)
    assert answer.reason_code == 'learned_missing_target'


def test_primary_recipient_choice_uses_human_source_and_none():
    turn = TurnContext('room', 'user', '帮我看一下',
        (MessageSnapshot('m1', 'user', '', source_text='帮我看一下'),),
        (MessageSnapshot('b1', 'bot', '上一轮回复', semantics=SimpleNamespace(sender_is_bot=True)),),
        1, 1, 0, True)
    questions, _ = turn_questions(turn)
    assert set(questions['recipient_choice']['criteria']) == {'user', 'none'}
    assert '帮我看一下' in questions['recipient_choice']['criteria']['user']


def test_recipient_choice_contains_authors_of_all_offered_targets():
    turn = TurnContext('room', 'u0', '回复较早的 u1',
        (MessageSnapshot('now', 'u0', '回复较早的 u1'),),
        tuple(MessageSnapshot(f'm{i}', f'u{i}', f'消息{i}') for i in range(1, 6)),
        1, 1, 0, True)
    questions, candidates = turn_questions(turn)
    assert 'm1' in candidates
    assert 'u1' in questions['recipient_choice']['criteria']


def test_teacher_labels_are_not_fabricated_probabilities():
    questions = {'join': {'type': 'noul'}}
    assert teacher_answers('{"join":0.99}', questions) is None
    assert teacher_answers('{"join":true,"extra":false}', questions) is None
    assert teacher_answers('{"join":false}', questions)['join']['noul'] == 0
    assert answer_uncertainty({'type': 'noul', 'noul': float('nan')}) is None


def test_optional_teacher_questions_do_not_discard_valid_core_answers():
    questions = {'join': {'type': 'noul'},
                 'reply_length': {'type': 'choice', 'criteria': {'tiny': '一句', 'short': '几句'}}}
    assert teacher_answers('{"join":true}', questions) is None
    answer = teacher_answers('{"join":true}', questions, optional_keys={'reply_length'})
    assert set(answer) == {'join'} and answer['join']['noul'] == 1
    answer = teacher_answers('{"join":true,"reply_length":"invalid"}', questions,
                             optional_keys={'reply_length'})
    assert set(answer) == {'join'}
    assert teacher_answers('{"reply_length":"tiny"}', questions,
                           optional_keys={'reply_length'}) is None


@pytest.mark.asyncio
async def test_docker_host_allowlist_is_exact_and_reconfigure_invalidates():
    client = LayaClient(base_url='http://laya-server:8900')
    assert not client.snapshot()['configured']
    client.configure(base_url='http://laya-server:8900', internal_hosts=('laya-server',))
    assert client.snapshot()['configured']
    generation = client._generation
    client.configure(base_url='http://laya-server.evil:8900', internal_hosts=('laya-server',))
    assert not client.snapshot()['configured']
    assert client._generation > generation
    await client.close()



def test_teacher_abstention_is_not_zero_and_scores_keep_all_levels():
    questions = {str(i): {"type": "score", "criteria": list(range(5))} for i in range(5)}
    questions["unknown"] = {"type": "noul"}
    labels = {str(i): i for i in range(5)}
    labels["unknown"] = None
    result = teacher_answers(json.dumps(labels), questions)
    assert result.abstentions == ["unknown"] and "unknown" not in result
    assert [result[str(i)]["score"] for i in range(5)] == list(range(5))
    assert all(row["label_kind"] == "hard" and not row["confidence_is_calibrated"] for row in result.values())


def test_rubrics_preserve_questions_and_cover_five_distinct_levels():
    from astrbot_plugin_chat_dynamics.core.decision_rubrics import rubric_questions, RUBRICS
    original = {key: {"type": "score", "criteria": ["old"]*5} for key in RUBRICS}
    prepared = rubric_questions(original)
    assert original["professionalism"]["criteria"] == ["old"]*5
    assert all(len(set(question["criteria"])) == 5 for question in prepared.values())
