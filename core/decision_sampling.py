"""Prioritize real teacher annotation without treating student guesses as labels."""
import hashlib
import json
from collections import OrderedDict

from .decision_tasks import task_id


class AnnotationPriority:
    def __init__(self):
        self.recent = OrderedDict()

    def choose(self, session, state, questions, student, coverage, now):
        conversation = state.get("conversation", state) if isinstance(state, dict) else {}
        reasons = []
        if conversation.get("explicit") is True:
            reasons.append("explicit_address")
        if sum(key.startswith("target.") for key in questions) > 1:
            reasons.append("multiple_targets")
        if conversation.get("truncated") is True:
            reasons.append("long_context")
        for key, answer in student.items():
            kind = questions.get(key, {}).get("type")
            value = answer.get(kind)
            if kind == "noul" and isinstance(value, (int, float)):
                label = "true" if value >= .5 else "false"
            elif kind == "score" and isinstance(value, (int, float)):
                label = str(round(value))
            elif kind == "choice" and isinstance(value, str):
                label = value
            else:
                continue
            counts = coverage.get(task_id(key), {})
            if not counts.get("dynamic") and counts.get("class_counts", {}).get(label, 0) < 50:
                reasons.append("predicted_rare_class")
                break
        if not reasons:
            return 0, []
        # Repeated text/candidate shapes do not monopolize priority; ordinary
        # missing-label collection and random request sampling still proceed.
        text = conversation.get("text", conversation.get("current_message", ""))
        fingerprint = hashlib.sha256(json.dumps([session, text, sorted(questions)],
                                                 ensure_ascii=False).encode()).hexdigest()
        while self.recent and (now - next(iter(self.recent.values())) >= 900 or len(self.recent) >= 2048):
            self.recent.popitem(last=False)
        if fingerprint in self.recent:
            return 0, ["repeat_priority_suppressed"]
        self.recent[fingerprint] = now
        return (3 if "explicit_address" in reasons else 2), reasons
