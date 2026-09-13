"""Candidate parsing, attribution, and the two metrics that must not merge."""
from astrbot_plugin_chat_dynamics.core.candidate_metrics import (
    CANDIDATE_MISS,
    NOT_RECORDED,
    RANKING_ERROR,
    SELECTED,
    UNATTRIBUTABLE,
    annotation_metrics,
    candidate_metrics,
)
from astrbot_plugin_chat_dynamics.core.routing_metrics import routing_metrics
from astrbot_plugin_chat_dynamics.core.topic_candidates import parse_candidates


def _harness_truth(row, index, rows):
    """The harness resolves a free-text label through earlier labelled members."""
    label = row.get("expected_topic")
    return tuple(sorted({str(previous.get("topic_id")) for previous in rows[:index]
                         if previous.get("expected_topic") == label
                         and previous.get("topic_id")}))


def test_legacy_pairs_keep_their_recorded_order():
    parsed = parse_candidates([[0.9, "b"], [0.4, "a"]])
    assert parsed.recorded
    assert parsed.ids == ("b", "a")
    assert [item.rank for item in parsed.items] == [1, 2]


def test_a_legacy_list_written_ascending_is_read_by_score_not_position():
    """The router writes descending, so a list that is not is read by its scores."""
    assert parse_candidates([[0.1, "low"], [0.9, "high"]]).ids == ("high", "low")


def test_explicit_rank_beats_final_score():
    parsed = parse_candidates([{"topic_id": "a", "final_score": 0.9, "rank": 2},
                               {"topic_id": "b", "final_score": 0.2, "rank": 1}])
    assert parsed.ids == ("b", "a")


def test_structured_rows_without_rank_order_by_final_score():
    parsed = parse_candidates([{"topic_id": "a", "final_score": 0.2},
                               {"topic_id": "b", "final_score": 0.9}])
    assert parsed.ids == ("b", "a")


def test_the_structured_contract_parses_with_evidence_ignored():
    """Evidence is diagnostic: an unrelated key must not drop the candidate."""
    parsed = parse_candidates([
        {"topic_id": "A", "final_score": 0.68, "rank": 2,
         "evidence": {"semantic": 0.74, "reply_edge": 0.0}},
        {"topic_id": "B", "final_score": 0.72, "rank": 1,
         "evidence": {"semantic": 0.66, "reply_edge": 1.0}}])
    assert parsed.ids == ("B", "A")
    assert parsed.items[0].score == 0.72


def test_a_candidate_without_score_is_still_offered():
    """A missing score proves nothing; it must not become a score of zero."""
    parsed = parse_candidates([{"topic_id": "a"}])
    assert parsed.recorded and parsed.ids == ("a",)
    assert parsed.items[0].score is None


def test_missing_and_unreadable_are_not_an_empty_candidate_list():
    assert not parse_candidates(None).recorded
    assert not parse_candidates({"topic_id": "a"}).recorded
    assert not parse_candidates("a").recorded
    assert parse_candidates([]).recorded


def test_malformed_entries_are_dropped_and_counted():
    parsed = parse_candidates([[0.5, "a"], [0.5], "junk", [0.5, ""], {"nope": 1}])
    assert parsed.ids == ("a",)
    assert parsed.dropped == 4


def test_booleans_are_not_scores():
    parsed = parse_candidates([[True, "a"], [0.5, "b"]])
    assert parsed.items[0].score is None
    assert parsed.ids == ("a", "b")


def test_offered_but_lost_is_not_a_retrieval_failure():
    rows = [{"expected_topic": "a", "topic_id": "b",
             "topic_candidates": [[0.9, "b"], [0.8, "a"]]}]
    result = candidate_metrics([rows])
    assert result["outcomes"][RANKING_ERROR] == 1
    assert result["outcomes"][CANDIDATE_MISS] == 0
    assert result["recall"]["3"] == {"hits": 1, "eligible": 1, "value": 1.0}
    assert result["selection"] == {"correct": 0, "eligible": 1, "value": 0.0}


def test_never_offered_is_a_retrieval_failure_and_leaves_nothing_to_rank():
    rows = [{"expected_topic": "a", "topic_id": "b", "topic_candidates": [[0.9, "b"]]}]
    result = candidate_metrics([rows])
    assert result["outcomes"][CANDIDATE_MISS] == 1
    assert result["outcomes"][RANKING_ERROR] == 0
    assert result["selection"] == {"correct": 0, "eligible": 0, "value": None}


def test_selection_accuracy_is_conditioned_on_recall():
    """Retrieval finds half the topics; the ranking is never wrong."""
    rows = [{"expected_topic": "a", "topic_id": "a", "topic_candidates": [[0.9, "a"]]},
            {"expected_topic": "b", "topic_id": "b", "topic_candidates": [[0.9, "b"]]},
            {"expected_topic": "c", "topic_id": "z", "topic_candidates": [[0.9, "z"]]},
            {"expected_topic": "d", "topic_id": "z", "topic_candidates": [[0.9, "z"]]}]
    result = candidate_metrics([rows])
    assert result["recall"]["1"]["value"] == 0.5
    assert result["selection"]["value"] == 1.0
    assert result["selection"]["eligible"] == 2


def test_unrecorded_candidates_are_excluded_rather_than_scored_as_misses():
    rows = [{"expected_topic": "a", "topic_id": "a"},
            {"expected_topic": "b", "topic_id": "b", "topic_candidates": [[0.9, "b"]]}]
    result = candidate_metrics([rows])
    assert result["outcomes"][NOT_RECORDED] == 1
    assert result["recall"]["1"] == {"hits": 1, "eligible": 1, "value": 1.0}
    assert any("没有记录候选" in note for note in result["notes"])


def test_a_new_topic_label_has_no_candidate_membership():
    rows = [{"expected_topic": "NEW", "topic_id": "x", "topic_candidates": [[0.9, "x"]]},
            {"expected_topic": "UNKNOWN", "topic_id": "x", "topic_candidates": []}]
    result = candidate_metrics([rows])
    assert result["outcomes"]["new_topic_expected"] == 2
    assert result["outcomes"][CANDIDATE_MISS] == 0
    assert result["recall"]["1"]["eligible"] == 0


def test_every_accepted_topic_id_counts_as_a_hit():
    """Two labelled members map one label onto two router topics; either is right."""
    rows = [{"expected_topic": "a", "topic_id": "x"},
            {"expected_topic": "a", "topic_id": "y"},
            {"expected_topic": "a", "topic_id": "y", "topic_candidates": [[0.9, "y"]]}]
    result = candidate_metrics([rows], truth_of=_harness_truth)
    assert result["outcomes"][SELECTED] == 1
    assert result["recall"]["1"]["hits"] == 1
    assert result["recall"]["1"]["eligible"] == 1


def test_recall_at_k_follows_rank():
    rows = [{"expected_topic": "a", "topic_id": "b",
             "topic_candidates": [{"topic_id": "b", "rank": 1}, {"topic_id": "a", "rank": 2}]}]
    result = candidate_metrics([rows])
    assert result["recall"]["1"]["hits"] == 0
    assert result["recall"]["3"]["hits"] == 1


def test_rows_that_are_not_mappings_are_counted_not_guessed():
    result = candidate_metrics([["garbage"]])
    assert result["outcomes"][UNATTRIBUTABLE] == 1
    assert result["rows"] == 1


def test_truncated_candidate_lists_are_flagged():
    rows = [{"expected_topic": "a", "topic_id": "a", "topic_candidates": [[0.9, "a"]]}]
    assert any("recall@5 与 recall@3" in note for note in candidate_metrics([rows])["notes"])


def test_routing_metrics_reads_the_structured_candidate_schema():
    """Regression: the recall loop indexed candidates positionally as pair[1]."""
    rows = [{"expected_topic": "a", "topic_id": "x"},
            {"expected_topic": "a", "topic_id": "x",
             "topic_candidates": [{"topic_id": "y", "final_score": 0.9, "rank": 1},
                                  {"topic_id": "x", "final_score": 0.8, "rank": 2}]}]
    topic = routing_metrics([rows])["topic"]
    assert topic["candidate_recall"]["1"] == {"hits": 0, "eligible": 1, "value": 0.0}
    assert topic["candidate_recall"]["3"] == {"hits": 1, "eligible": 1, "value": 1.0}
    assert topic["candidate_selection"]["correct"] == 1
    assert topic["candidate_attribution"][SELECTED] == 1


def test_annotation_metrics_take_the_stored_label_as_the_truth_id():
    """Saved labels are validated topic ids, so no inference from members."""
    records = [
        {"msg_id": "1", "predicted_topic": "b", "expected_topic": "a",
         "routing": {"topic_candidates": [{"topic_id": "a", "final_score": 0.8, "rank": 1},
                                          {"topic_id": "b", "final_score": 0.7, "rank": 2}]}},
        {"msg_id": "2", "predicted_topic": "c", "expected_topic": "c", "routing": {}}]
    result = annotation_metrics(records)
    assert result["outcomes"][RANKING_ERROR] == 1
    assert result["outcomes"][NOT_RECORDED] == 1
    assert result["selection"]["value"] == 0.0
