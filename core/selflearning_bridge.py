"""Compatibility import; integration ownership lives in core.integrations."""
from .integrations.registry import IntegrationRegistry as SelfLearningBridge
from .integrations.legacy.selflearning_legacy import SelfLearningStatus

__all__ = ["SelfLearningBridge", "SelfLearningStatus"]
