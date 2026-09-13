"""Dynamics Learning: records why a decision was made, not only what it was.

Shadow only. Nothing in this package writes configuration or changes a live
decision; it turns existing decision evidence and human labels into samples a
later learner can fit, and reports what the errors look like.

Two rules hold everywhere in here:

* a fact that was never observed is absent, not zero — see LearningSample.present
  and build_matrix;
* a row that would need a guess to build is not built, and the reason is counted
  — see SampleBuildReport and SKIP_REASONS.
"""
from .sample import FEATURE_SCHEMA_VERSION, SAMPLE_SCHEMA_VERSION, LearningSample, TASKS
from .store import SampleStore
from .builder import (
    SKIP_NO_BOT_ID, SKIP_NO_RECIPIENT_LABEL, SKIP_NO_REPLY_LABEL, SKIP_REASONS,
    SKIP_UNDECIDED_REPLY, SampleBuildReport, build_samples, facts_from_text, facts_from_trace,
    features_from_trace, samples_from_annotation, samples_from_annotations, session_hash,
)
from .stats import POSITIVE_CLASS, factor_disagreement, outcome_bucket, summarize
from .candidates import OUTCOMES, SCORED_OUTCOMES, annotation_metrics, candidate_metrics
from .recipient_learner import (
    DecisionOutcome, FactorFinding, Recommendation, ShadowPolicy, analyze_recipient,
    build_matrix, evaluate_decisions, feature_columns, fit_logistic, split_by_group,
)

__all__ = [
    "FEATURE_SCHEMA_VERSION", "SAMPLE_SCHEMA_VERSION", "LearningSample", "TASKS",
    "SampleStore",
    "SKIP_NO_BOT_ID", "SKIP_NO_RECIPIENT_LABEL", "SKIP_NO_REPLY_LABEL", "SKIP_REASONS",
    "SKIP_UNDECIDED_REPLY", "SampleBuildReport", "build_samples", "features_from_trace",
    "facts_from_text", "facts_from_trace", "samples_from_annotation",
    "samples_from_annotations", "session_hash",
    "POSITIVE_CLASS", "factor_disagreement", "outcome_bucket", "summarize",
    "OUTCOMES", "SCORED_OUTCOMES", "annotation_metrics", "candidate_metrics",
    "DecisionOutcome", "FactorFinding", "Recommendation", "ShadowPolicy", "analyze_recipient",
    "build_matrix", "evaluate_decisions", "feature_columns", "fit_logistic", "split_by_group",
]
