import importlib.util
import json
from pathlib import Path

from astrbot_plugin_chat_dynamics.core.routing_trace import build_routing_trace


ROOT = Path(__file__).resolve().parents[1]


def load_evaluator():
    spec = importlib.util.spec_from_file_location("routing_evaluator", ROOT / "scripts/evaluate_routing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trace_is_json_safe_private_and_does_not_alias_inputs():
    routing = {"addressee_ids": ["bot", "human"], "bot_is_addressee": True,
               "topic_confidence": float("nan"), "text": "private text"}
    state = {"intervening_users": ["human"], "payload": "private payload"}
    before = list(routing["addressee_ids"])
    trace = build_routing_trace(routing=routing, state=state)
    assert trace["trace_schema_version"] == 3
    assert trace["topic"]["confidence"] is None
    assert trace["routing"]["topic_candidates"] == []
    assert "private" not in json.dumps(trace, allow_nan=False)
    trace["recipient"]["ids"].append("new")
    trace["state"]["intervening_users"].clear()
    assert routing["addressee_ids"] == before
    assert state["intervening_users"] == ["human"]


def test_evaluator_is_deterministic_and_loads_root_package():
    evaluator = load_evaluator()
    assert Path(evaluator.thread_router.__file__).resolve() == ROOT / "core/thread_router.py"
    cases = json.loads((ROOT / "tests/fixtures/routing_golden.json").read_text(encoding="utf-8"))
    before = json.dumps(cases, ensure_ascii=False)
    report = evaluator.evaluate(cases)
    assert report == evaluator.evaluate(cases)
    assert report["failed"] == 0
    assert len(report["fixture_hash"]) == len(report["config_hash"]) == 64
    for row in report["results"]:
        assert row["actual"]["level"] in {"strong", "hover", "weak"}
        for mode, trace in row["traces"].items():
            assert trace["mode"] == mode
            assert trace["participation"]["should_reply"] is None
            assert set(trace["identity"]) >= {"mention", "vocative", "subject"}
    assert json.dumps(cases, ensure_ascii=False) == before


def test_evaluator_reports_mismatches_without_mutating_golden():
    evaluator = load_evaluator()
    cases = [{"id": "intentional_negative", "messages": [{"text": "hello", "mentions": ["bot"]}],
              "expected": {"bot_targeted": False}}]
    report = evaluator.evaluate(cases)
    assert report["failed"] == 1
    assert report["confusion"]["legacy"]["fp"] == 1


def test_trace_consumes_enum_without_retaining_objects():
    from astrbot_plugin_chat_dynamics.core.addressivity import AddressivityLevel
    trace = build_routing_trace(routing={}, participation={"level": AddressivityLevel.STRONG})
    assert trace["participation"]["level"] == "strong"


def test_unlabeled_regression_pass_is_not_effectiveness_pass():
    report = load_evaluator().evaluate([{'id': 'empty_truth', 'messages': [{'text': 'hello'}]}])
    assert report['failed'] == 0
    assert report['effectiveness_gate']['status'] == 'not_ready'
    assert 'insufficient_labels' in report['effectiveness_gate']['reasons']


def test_synthetic_truth_and_ai_labels_are_separate_from_real_validation():
    cases = json.loads((ROOT / 'tests/fixtures/routing_multiturn.json').read_text(encoding='utf-8'))
    cases[0]['label_source'] = 'ai_assisted'
    report = load_evaluator().evaluate(cases)
    gate = report['effectiveness_gate']
    assert gate['status'] == 'not_ready'
    assert gate['by_label_source']['ai_assisted']['topic']['pairs'] > 0
    assert gate['independent_validation_sessions'] == 0
    assert gate['execution']['status'] == 'not_measured'


def test_replay_rejects_future_parent_and_session_split_leakage():
    import pytest
    evaluator = load_evaluator()
    with pytest.raises(ValueError, match='visible earlier'):
        evaluator.evaluate([{'id': 'future', 'messages': [{'text': 'x', 'expected_parent': '1'}]}])
    with pytest.raises(ValueError, match='crosses dataset splits'):
        evaluator.evaluate([{'id': str(i), 'session_id': 'same', 'split': split,
                             'messages': [{'text': 'x'}]}
                            for i, split in enumerate(['train', 'validation'])])


def test_observation_is_causally_prefix_stable():
    evaluator = load_evaluator()
    case = {'id': 'prefix', 'messages': [{'text': 'GPU fan', 'expected_topic': 'hardware'},
            {'text': 'GPU fan again', 'reply_to': '0', 'expected_topic': 'hardware'}]}
    short = evaluator.evaluate([{**case, 'messages': case['messages'][:1]}])
    long = evaluator.evaluate([case])
    assert short['results'][0]['observations'][0] == long['results'][0]['observations'][0]


def test_effectiveness_requires_labeled_sessions_and_rejects_measured_errors():
    evaluator = load_evaluator()
    # Contract unit data, never represented as a collected real corpus.
    cases = [{'id': str(i), 'session_id': str(i), 'reviewer_id': 'reviewer',
              'split': 'validation', 'label_source': 'independent_human',
              'corpus_kind': 'real'} for i in range(30)]
    rows = [{'observations': [
        {'expected_topic': 'human-label', 'topic_id': 'runtime-id',
         'expected_parent': None, 'parent_message_id': None},
        {'expected_topic': 'human-label', 'topic_id': 'runtime-id'}]} for _ in cases]
    assert evaluator.effectiveness(cases, rows)['status'] == 'passed'
    rows[0]['observations'][1]['topic_id'] = 'different'
    assert evaluator.effectiveness(cases, rows)['status'] == 'failed'
    rows[0]['observations'] = []
    gate = evaluator.effectiveness(cases, rows)
    assert gate['status'] == 'not_ready'
    assert gate['independent_validation_sessions'] == 29


def test_baseline_comparison_is_read_only():
    evaluator = load_evaluator()
    baseline = {"failed": 1, "results": [{"id": "a", "actual": {"bot_targeted": False}}]}
    current = {"failed": 0, "fixture_hash": "new", "results": [
        {"id": "a", "actual": {"bot_targeted": True}}]}
    before = json.dumps([baseline, current])
    comparison = evaluator.compare_reports(current, baseline)
    assert comparison["failed_delta"] == -1
    assert comparison["changes"][0]["id"] == "a"
    assert json.dumps([baseline, current]) == before
    # A field added after the baseline was captured is not a behaviour change.
    widened = {"failed": 0, "fixture_hash": "new", "results": [
        {"id": "a", "actual": {"bot_targeted": False, "level": "weak"}}]}
    assert evaluator.compare_reports(widened, baseline)["changes"] == []
    narrowed = {"failed": 0, "results": [{"id": "a", "actual": {}}]}
    assert evaluator.compare_reports(narrowed, baseline)["changes"][0]["id"] == "a"
