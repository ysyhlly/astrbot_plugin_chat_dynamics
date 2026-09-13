"""Dynamics Learning: records why a decision was made, not only what it was.

Shadow only. Nothing in this package writes configuration or changes a live
decision; it turns existing decision evidence and human labels into samples a
later learner can fit, and reports what the errors look like.
"""
from .sample import LearningSample, TASKS
from .store import SampleStore
from .builder import features_from_trace, samples_from_annotation, samples_from_annotations
from .stats import factor_disagreement, summarize
from .recipient_learner import (FactorFinding, Recommendation, analyze_recipient,
                                fit_logistic)

__all__ = ["LearningSample", "TASKS", "SampleStore", "features_from_trace",
           "samples_from_annotation", "samples_from_annotations",
           "factor_disagreement", "summarize", "FactorFinding", "Recommendation",
           "analyze_recipient", "fit_logistic"]
