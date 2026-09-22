from astrbot_plugin_chat_dynamics.core.decision_sampling import AnnotationPriority


def test_explicit_priority_dedup_and_expiry_do_not_change_labels():
    policy = AnnotationPriority()
    state = {"conversation": {"explicit": True, "text": "hello"}}
    questions = {"join": {"type": "noul"}}
    student = {"join": {"noul": .9}}
    assert policy.choose("room", state, questions, student, {}, 1000)[0] == 3
    assert policy.choose("room", state, questions, student, {}, 1001)[0] == 0
    assert policy.choose("other", state, questions, student, {}, 1001)[0] == 3
    assert policy.choose("room", state, questions, student, {}, 1900)[0] == 3
    assert student == {"join": {"noul": .9}}


def test_class_coverage_and_dynamic_candidates():
    policy = AnnotationPriority()
    questions = {"join": {"type": "noul"}}
    student = {"join": {"noul": .9}}
    assert policy.choose("room", {}, questions, student, {"join": {"class_counts": {"true": 50}}}, 1000)[0] == 0
    assert policy.choose("room", {}, questions, student, {"join": {"class_counts": {"true": 49}}}, 1000)[0] == 2
    assert policy.choose("room", {}, {"topic": {"type": "choice"}}, {"topic": {"choice": "topic_0"}}, {"topic": {"dynamic": True}}, 1000)[0] == 0
