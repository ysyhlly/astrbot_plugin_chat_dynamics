"""Bridge AstrBot DecisionProvider.ask and the plugin's closed question format."""
import json
from collections.abc import Mapping

from astrbot.core.provider.entities import (
    DecisionBooleanAnswer, DecisionBooleanQuestion, DecisionChoiceAnswer,
    DecisionChoiceQuestion, DecisionScoreAnswer, DecisionScoreQuestion,
)
from astrbot.core.provider.provider import DecisionProvider

from .client import SystemOneError, validate_answers


def is_decision_provider(provider):
    return isinstance(provider, DecisionProvider) or bool(getattr(provider, 'is_systemone_provider', False))


def from_typed_questions(questions):
    result = {}
    for key, question in questions.items():
        if isinstance(question, DecisionChoiceQuestion):
            spec = {'type':'choice','criteria':dict(question.criteria)}
        elif isinstance(question, DecisionScoreQuestion):
            spec = {'type':'score','criteria':list(question.criteria)}
        elif isinstance(question, DecisionBooleanQuestion):
            spec = {'type':'noul'}
        else:
            raise SystemOneError('不支持该原生抉择问题类型。', code='invalid_questions')
        result[key] = {**spec,'instructions':question.instructions}
    return result


def to_typed_answers(answers, questions):
    result = {}
    for key, spec in questions.items():
        answer = answers[key]
        if spec['type'] == 'noul':
            result[key] = DecisionBooleanAnswer(probability=answer['noul'])
        elif spec['type'] == 'choice':
            result[key] = DecisionChoiceAnswer(choice=answer['choice'],
                probability=answer['probabilities'][answer['choice']],
                probabilities=answer['probabilities'], confidence=answer['confidence'])
        else:
            result[key] = DecisionScoreAnswer(score=answer['score'],
                legend={str(i):level for i,level in enumerate(spec['criteria'])},
                probabilities=answer['probabilities'], confidence=answer['confidence'])
    return result


async def ask_native(provider, *, state, questions):
    typed = {}
    singles = {}
    for key, spec in questions.items():
        instructions = spec.get('instructions')
        if isinstance(instructions, dict):
            instructions = json.dumps(instructions,ensure_ascii=False)
        if not isinstance(instructions,str) or not instructions.strip():
            raise SystemOneError('抉择问题说明无效。',code='invalid_questions')
        kind, criteria = spec['type'], spec.get('criteria')
        if kind == 'noul':
            if criteria:
                instructions += '\nCondition rubric: '+json.dumps(criteria,ensure_ascii=False)
            typed[key] = DecisionBooleanQuestion(instructions)
        elif kind == 'choice' and isinstance(criteria,dict) and len(criteria) == 1:
            singles[key] = next(iter(criteria))
            typed[key] = DecisionBooleanQuestion(instructions+'\nIs the sole candidate appropriate? '+
                                                  json.dumps(criteria,ensure_ascii=False))
        elif kind == 'choice':
            typed[key] = DecisionChoiceQuestion(instructions,dict(criteria))
        elif kind == 'score':
            typed[key] = DecisionScoreQuestion(instructions,list(criteria))
        else:
            raise SystemOneError('不支持该抉择问题类型。',code='invalid_questions')
    returned = await provider.ask(typed,state,request_max_retries=0)
    if not isinstance(returned,Mapping) or set(returned) != set(questions):
        raise SystemOneError('原生抉择答案缺失或不匹配。',code='invalid_decision_answers')
    result = {}
    for key,spec in questions.items():
        answer = returned[key]
        if key in singles:
            if not isinstance(answer,DecisionBooleanAnswer):
                raise SystemOneError('单候选抉择类型不匹配。',code='invalid_decision_answers')
            value = singles[key]
            result[key] = {'type':'choice','choice':value,'confidence':answer.probability,
                           'probabilities':{value:answer.probability}}
        elif spec['type'] == 'noul' and isinstance(answer,DecisionBooleanAnswer):
            result[key] = {'type':'noul','noul':answer.probability}
        elif spec['type'] == 'choice' and isinstance(answer,DecisionChoiceAnswer):
            if set(answer.probabilities) != set(spec['criteria']):
                raise SystemOneError('抉择候选不匹配。',code='invalid_decision_answers')
            result[key] = {'type':'choice','choice':answer.choice,'confidence':answer.confidence,
                           'probabilities':dict(answer.probabilities)}
        elif spec['type'] == 'score' and isinstance(answer,DecisionScoreAnswer):
            expected = {str(i):level for i,level in enumerate(spec['criteria'])}
            if dict(answer.legend) != expected or set(answer.probabilities) != set(expected):
                raise SystemOneError('抉择评分等级不匹配。',code='invalid_decision_answers')
            result[key] = {'type':'score','score':answer.score,'confidence':answer.confidence,
                           'probabilities':dict(answer.probabilities)}
        else:
            raise SystemOneError('原生抉择答案类型不匹配。',code='invalid_decision_answers')
    return validate_answers({'answers':result},questions)
