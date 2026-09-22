import json
import pytest

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
              'reason': 'addressed_request', 'target.0': True, 'target.1': True, 'target.2': False}
    import json
    answers = teacher_answers(json.dumps(labels), questions)
    assert turn_from_answers(turn, answers, candidates).target_message_ids == ('m1', 'm2')
    labels.update({'target.0': False, 'target.1': False})
    answer = turn_from_answers(turn, teacher_answers(json.dumps(labels), questions), candidates)
    assert answer.reason_code == 'learned_missing_target'


def test_teacher_labels_are_not_fabricated_probabilities():
    questions = {'join': {'type': 'noul'}}
    assert teacher_answers('{"join":0.99}', questions) is None
    assert teacher_answers('{"join":true,"extra":false}', questions) is None
    assert teacher_answers('{"join":false}', questions)['join']['noul'] == 0
    assert answer_uncertainty({'type': 'noul', 'noul': float('nan')}) is None


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
