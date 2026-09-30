"""Opt-in teacher/student orchestration, sharing one deadline and immutable input."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path

from .decision_tasks import TASK_VERSION, TASKS, TEACHER_PROMPT_VERSION, OPTIONAL_TURN_QUESTIONS, task_id, teacher_answers, teacher_instructions, answer_uncertainty
from .integrations.typesafe import _validated_answers
from .decision_sampling import AnnotationPriority
from .decision_rubrics import rubric_questions
from .decision_snapshot import SNAPSHOT_VERSION, validate_snapshot

logger = logging.getLogger(__name__)


def _student_training_status(key, student, model_info):
    if key not in student:
        return "no_prediction"
    trained = model_info.get("trained_question_ids", ())
    untrained = model_info.get("untrained_question_ids", ())
    if key == "join" and key in trained and key not in untrained:
        return "trained_join"
    if key in untrained and key not in trained:
        return "untrained_transfer"
    return "unknown"


def _kev_canary_selected(session_id, state, percent):
    """A stable per-message cohort; one busy group must not be all-in or all-out."""
    if not isinstance(state, dict) or type(percent) not in (int, float) or not 0 < percent <= 100:
        return False
    conversation = state.get("conversation") or {}
    messages = conversation.get("messages") if isinstance(conversation, dict) else None
    latest = messages[-1] if isinstance(messages, list) and messages else None
    message_id = latest.get("message_id") if isinstance(latest, dict) else None
    if not isinstance(message_id, str) or not message_id:
        return False
    digest = hashlib.sha256(f"{session_id}\0{message_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 * 100 < percent


def _kev_canary_core(questions, student):
    """One internally consistent proactive turn; do not cherry-pick task answers."""
    core = {key for key in questions if key in {"join", "action"} or key.startswith("target.")}
    targets = {key for key in core if key.startswith("target.")}
    if not targets or set(student) != core:
        return None
    join = student.get("join", {})
    action = student.get("action", {})
    if (join.get("type") != "noul" or
            type(join.get("noul")) not in (int, float) or
            not math.isfinite(join["noul"]) or
            join["noul"] < .5 or
            action.get("type") != "choice" or
            action.get("choice") not in {"reply", "acknowledge"}):
        return None
    selected = [key for key in targets if isinstance(student[key], dict)
                and student[key].get("type") == "noul"
                and type(student[key].get("noul")) in (int, float)
                and math.isfinite(student[key]["noul"])
                and student[key]["noul"] >= .5]
    return core if len(selected) == 1 else None


class TeacherAnswers(dict):
    """Per-call model provenance; never a shared mutable last-response field."""
    def __init__(self, answers, model, prompt_version=TEACHER_PROMPT_VERSION):
        super().__init__(answers)
        self.model = model
        self.prompt_version = prompt_version
        self.abstentions = tuple(getattr(answers, "abstentions", ()))


class DecisionLearning:
    ANNOTATION_CONCURRENCY = 3

    def __init__(self, host, data_root):
        self.host = host
        self.path = Path(data_root) / "decision-learning.sqlite3"
        self.store = None
        self.closed = False
        self.worker = None
        self.maintenance = None
        self._collection_epochs = Counter()
        self.tasks = set()
        self.stats = Counter()
        self.recent = deque(maxlen=50)
        self.disagreements = deque(maxlen=50)
        self._service_status = {}
        self._status_at = 0.0
        self.model_version = ""
        self._io_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)
        self._annotation_slots = asyncio.Semaphore(self.ANNOTATION_CONCURRENCY)
        self._label_claim_lock = asyncio.Lock()
        self._comparison_slots = asyncio.Semaphore(2)
        self._comparison_lock = asyncio.Lock()
        self._label_times = deque()
        self._last_purge = 0.0
        self._annotation_priority = AnnotationPriority()
        self._coverage = {}
        self._coverage_at = 0.0
        self._summary_cache = {}
        self._summary_at = 0.0
        self._summary_lock = asyncio.Lock()
        self._summary_task = None
        self._summary_stop = threading.Event()
        self._summary_signature = None

    @property
    def cfg(self):
        return self.host._runtime_config

    def enabled(self, session_id=""):
        return (not self.closed and not getattr(self.host, "_shutting_down", False)
                and getattr(self.cfg, "decision_learning_mode", "off") != "off")

    def collecting(self, session_id):
        configured = getattr(self.cfg, "decision_learning_sessions", ())
        if not session_id:
            return False
        if "*" not in configured:
            return session_id in configured
        runtime = getattr(self.host, "_sessions", {}).get(session_id)
        if runtime is None:
            return False
        checker = getattr(self.host, "is_group_takeover_enabled", None)
        return bool(callable(checker) and checker(getattr(runtime, "group_id", "")))

    def collection_sessions(self):
        configured = self.cfg.decision_learning_sessions
        if "*" not in configured:
            return configured
        return tuple(key for key in getattr(self.host, "_sessions", {}) if self.collecting(key))

    async def _io(self, method, *args, **kwargs):
        async with self._io_lock:
            def run():
                if self.store is None:
                    from .decision_dataset import DecisionDataset
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.store = DecisionDataset(self.path)
                return getattr(self.store, method)(*args, **kwargs)
            # SQLite operations finish before the lock is released, including cancellation.
            task = asyncio.create_task(asyncio.to_thread(run))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _anonymize_online(self, payload):
        # The keyed anonymizer only reads the store's immutable secret. Running it
        # outside the SQLite write lock keeps queued sample writes off the turn path.
        store = self.store
        if store is None:
            async with self._io_lock:
                if self.store is None:
                    from .decision_dataset import DecisionDataset
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.store = await asyncio.to_thread(DecisionDataset, self.path)
                store = self.store
        return await asyncio.to_thread(store.anonymize, payload)

    def _spawn(self, coro):
        task = self.host._create_background_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _identity(self, session_id):
        runtime = getattr(self.host, "_sessions", {}).get(session_id)
        return (id(runtime), getattr(runtime, "epoch", None), getattr(runtime, "revision", None), id(self.cfg))

    async def _teacher(self, provider_id, state, questions, *, background=False, prompt_version=TEACHER_PROMPT_VERSION, comparison=False):
        if getattr(self.cfg, "decision_backend", "model") == "kev":
            raise RuntimeError("llm_decision_disabled")
        validate_snapshot(state, questions)
        from .provider_budget import context_budget
        from .persona_engine import completion_text
        budget = getattr(self.host.llm, "provider_budget", None) or context_budget(self.host.context)
        model = str(provider_id)
        async def invoke():
            nonlocal model
            getter = getattr(self.host.context, "get_provider_by_id", None)
            if callable(getter):
                provider = getter(provider_id)
                get_model = getattr(provider, "get_model", None)
                if callable(get_model):
                    model = f"{provider_id}:{get_model()}"
            return await self.host.context.llm_generate(
                chat_provider_id=provider_id, system_prompt=teacher_instructions(prompt_version),
                prompt=json.dumps({"state": state, "questions": questions}, ensure_ascii=False))
        async with (self._comparison_slots if comparison else self._annotation_slots if background else self._slots):
            result = await budget.run(provider_id, "decision_annotation" if background else "routing", invoke)
        optional = OPTIONAL_TURN_QUESTIONS if not background else ()
        answers = teacher_answers(completion_text(result), questions, optional_keys=optional)
        return TeacherAnswers(answers, model, prompt_version) if answers is not None else None

    async def evaluate(self, *, session_id, state, questions, timeout=None, outcome_node=None,
                       bot_speaker_id=None):
        if not self.enabled(session_id):
            return None
        questions = rubric_questions(questions)
        identity = self._identity(session_id)
        collection_epoch = self._collection_epochs[session_id]
        limit = float(timeout if timeout is not None else self.cfg.decision_timeout)
        end = time.monotonic() + max(.001, limit)
        started = time.monotonic()
        mode = self.cfg.decision_learning_mode
        model_only = getattr(self.cfg, "decision_backend", "model") == "kev"
        if model_only and (mode != "active" or
                           getattr(self.cfg, "decision_learning_student_backend", "") != "kev"):
            self.stats["kev_model_only_misconfigured"] += 1
            return None
        def remaining():
            return max(.001, end - time.monotonic())
        async def bounded(coro):
            return await asyncio.wait_for(coro, remaining())
        async def remote(coro):
            try:
                return await bounded(coro)
            except asyncio.CancelledError:
                raise
            except Exception:
                return None
        provider = ""
        prepared = None
        source_state = None
        student, teacher, chosen, sources = {}, {}, {}, {}
        model_info = {}
        student_status = "not_requested"
        student_latency_ms = None
        student_timeout_ms = None
        student_input = {}
        teacher_latency_ms = None
        provenance_rejected = False
        shadow_task = None
        prefetched_teacher = None
        hybrid_join = False
        kev_canary = False
        deferred_shadow = False
        student_bot_speaker_id = bot_speaker_id
        preparation_started = time.monotonic()
        preparation_latency_ms = None
        try:
            student_backend = getattr(self.cfg, "decision_learning_student_backend", "kev")
            client = (self.host.agentjev if student_backend == "agentjev" else
                      self.host.kev if student_backend == "kev" else self.host.laya)
            if self.collecting(session_id):
                anonymous = await bounded(self._anonymize_online(
                    {"state": state, "questions": questions,
                     "bot_speaker_id": student_bot_speaker_id}))
                state, questions = anonymous["state"], anonymous["questions"]
                student_bot_speaker_id = anonymous["bot_speaker_id"]
                self.start_worker()
            # Freeze the anonymized evidence before teacher/student calls.
            validate_snapshot(state, questions)
            source_state = json.loads(json.dumps(state, ensure_ascii=False))
            state = source_state
            preparation_latency_ms = (time.monotonic() - preparation_started) * 1000
            # Large all-task requests multiply inference paths and have repeatedly
            # missed the online deadline. Ask the trained join head alone, while
            # the teacher completes the remaining typed decisions in parallel.
            hybrid_join = (mode == "active" and student_backend == "agentjev" and
                           getattr(self.cfg, "agentjev_all_tasks_active", False) and
                           "join" in questions and len(questions) > 8)
            kev_canary = (not model_only and mode == "active" and student_backend == "kev" and
                          bool(getattr(self.cfg, "kev_checkpoint_id", "")) and
                          "join" in questions and "action" in questions and
                          any(key.startswith("target.") for key in questions) and
                          _kev_canary_selected(session_id, state,
                              getattr(self.cfg, "kev_canary_percent", 0)))
            if kev_canary:
                self.stats["kev_canary_selected_turns"] += 1
            student_questions = ({"join": questions["join"]} if hybrid_join else
                {key: question for key, question in questions.items()
                 if key in {"join", "action"} or key.startswith("target.")}
                if kev_canary else questions)

            async def fetch_teacher(batch):
                teacher_started = time.monotonic()
                selected_provider = (self.cfg.decision_provider_id or
                                     await bounded(self.host.llm.resolve_provider_id(session_id)))
                response = await bounded(self._teacher(selected_provider, state, batch))
                return selected_provider, response, (time.monotonic() - teacher_started) * 1000

            if hybrid_join:
                online = {key: question for key, question in questions.items()
                          if key not in OPTIONAL_TURN_QUESTIONS}
                prefetched_teacher = asyncio.create_task(fetch_teacher(online))
            if student_backend in ("agentjev", "kev"):
                prepared = {"state": state, "questions": questions}
            else:
                prepared = await remote(client.request_json("/prepare", {
                    "state": state, "questions": questions,
                    "task_versions": {task_id(key): TASK_VERSION for key in questions}},
                    timeout=min(self.cfg.laya_timeout, max(.05, limit * .25))))
                if prepared and isinstance(prepared.get("state"), (dict, list, str)) and prepared.get("questions") == questions:
                    try:
                        validate_snapshot(prepared["state"], questions)
                    except ValueError:
                        self.stats["invalid_prepared_snapshot"] += 1
                        prepared = None
                else:
                    prepared = None
                if prepared:
                    state = prepared["state"]
                    version = str(prepared.get("model_version", ""))
                    if version != self.model_version:
                        self.model_version = version
                        cache = getattr(self.host, "message_opinions", None)
                        if cache is not None:
                            cache.clear()
            async def predict_student():
                nonlocal student, model_info, student_status, student_latency_ms, student_timeout_ms, student_input
                student_status = "unavailable"
                if student_backend == "laya":
                    diagnostics = {}
                    envelope = await remote(client.request_json("/predict", {
                        "state": state, "questions": questions, "input_id": prepared.get("input_id"),
                        "expected_model_version": prepared.get("model_version")},
                        timeout=min(self.cfg.laya_timeout, max(.05, remaining() * .4)),
                        diagnostics=diagnostics))
                    student_status = diagnostics.get("status", "unavailable")
                    if envelope:
                        model_info = envelope.get("metadata", {})
                        if not isinstance(model_info, dict):
                            model_info = {}
                        student_status = "version_mismatch"
                        if model_info.get("model_version") == prepared.get("model_version"):
                            student_status = "invalid_answers"
                            student = _validated_answers(envelope, questions) or {}
                            for key in tuple(student):
                                answer, spec = student[key], questions[key]
                                if spec["type"] in ("choice", "score"):
                                    expected = (set(spec["criteria"]) if spec["type"] == "choice"
                                                else {str(i) for i in range(len(spec["criteria"]))})
                                    probs = answer.get("probabilities", {})
                                    if set(probs) != expected or answer_uncertainty(answer) is None:
                                        student.pop(key)
                                    elif spec["type"] == "choice" and probs[answer["choice"]] < max(probs.values()):
                                        student.pop(key)
                            student_status = "answered" if len(student) == len(questions) else "partial_answers" if student else "invalid_answers"
                    return
                student_state = state
                if student_bot_speaker_id is not None and isinstance(state, dict):
                    student_state = {**state, "bot_speaker_id": str(student_bot_speaker_id)}
                all_shadow = mode == "shadow" and (student_backend == "kev" or
                    getattr(self.cfg, "agentjev_all_tasks_shadow", False))
                backend_timeout = (self.cfg.kev_timeout if student_backend == "kev" else
                                   self.cfg.agentjev_timeout)
                student_timeout_ms = 1000 * (backend_timeout if all_shadow else
                    min(backend_timeout, max(.05, remaining() * .4),
                        1.5 if mode == "active" and len(student_questions) == 1 else
                        4.5 if mode == "active" else backend_timeout))
                diagnostics = {}
                student_started = time.monotonic()
                request = client.evaluate(state=student_state, questions=student_questions,
                    timeout=student_timeout_ms / 1000, diagnostics=diagnostics)
                if all_shadow:
                    try:
                        envelope = await request
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        envelope = None
                else:
                    envelope = await remote(request)
                student_latency_ms = (time.monotonic() - student_started) * 1000
                student_status = diagnostics.get("status", "unavailable")
                student_input = {field: diagnostics[field] for field in
                    ("state_format", "state_bytes", "question_count", "input_tokens",
                     "identity_check_ms", "server_model_ms") if field in diagnostics}
                if envelope:
                    student = envelope.get("answers", {})
                    version = envelope.get("model_version", "")
                    model_info = {"model_version": version,
                                  "checkpoint_sha256": envelope.get("checkpoint_sha256", "")}
                    usage = envelope.get("usage", {})
                    if isinstance(usage, dict):
                        for field in ("trained_question_ids", "untrained_question_ids"):
                            value = usage.get(field)
                            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                                model_info[field] = value
                        model_info["untrained_join_head_transfer"] = (
                            usage.get("untrained_join_head_transfer") is True)
                    # AgentJev's public API carries no task approval metadata.
                    # Active mode therefore cannot promote an uncalibrated model.
                    student_status = "answered" if student else "invalid_answers"
                    if version and version != self.model_version:
                        self.model_version = version
                        cache = getattr(self.host, "message_opinions", None)
                        if cache is not None:
                            cache.clear()

            if student_backend == "kev" and mode == "active" and not kev_canary and not model_only:
                prepared = None
                student_status = "canary_not_selected" if getattr(
                    self.cfg, "kev_canary_percent", 0) > 0 else "requires_release"
            if mode in ("shadow", "active"):
                if not prepared:
                    if student_backend != "kev" or mode != "active":
                        student_status = "not_prepared"
                elif mode == "shadow":
                    shadow_task = asyncio.create_task(predict_student())
                else:
                    await predict_student()
            if mode == "active":
                agentjev_takeover = (student_backend == "agentjev" and
                                     getattr(self.cfg, "agentjev_all_tasks_active", False))
                if model_only:
                    if (set(student) == set(questions) and
                            model_info.get("model_version") == getattr(self.cfg, "kev_checkpoint_id", "")):
                        for key, answer in student.items():
                            chosen[key], sources[key] = answer, "kev"
                        self.stats["kev_model_only_adopted_turns"] += 1
                    else:
                        self.stats["kev_model_only_abstained_turns"] += 1
                elif student_backend == "kev":
                    canary_core = _kev_canary_core(questions, student) if kev_canary else None
                    if (canary_core is not None and
                            model_info.get("model_version") == getattr(self.cfg, "kev_checkpoint_id", "")):
                        for key in canary_core:
                            chosen[key], sources[key] = student[key], "kev"
                        self.stats["kev_canary_adopted_turns"] += 1
                    elif student:
                        self.stats["kev_canary_rejected_turns"] += 1
                elif agentjev_takeover:
                    trained = model_info.get("trained_question_ids", ())
                    untrained = model_info.get("untrained_question_ids", ())
                    expected_checkpoint = getattr(self.cfg, "agentjev_active_checkpoint_sha256", "")
                    provenance_ok = (bool(expected_checkpoint) and
                                     model_info.get("checkpoint_sha256") == expected_checkpoint and
                                     str(model_info.get("model_version", "")).startswith("agentjev-cmdcode-base-set") and
                                     isinstance(trained, list) and isinstance(untrained, list) and
                                     set(trained).isdisjoint(untrained) and
                                     set(trained) <= {"join"} and
                                     set(trained) | set(untrained) == set(student_questions) == set(student) and
                                     (not untrained or (model_info.get("untrained_join_head_transfer") is True
                                                        and "join-head-transfer-untrained" in model_info["model_version"])))
                    if provenance_ok:
                        for key, answer in student.items():
                            chosen[key], sources[key] = answer, student_backend
                    elif student:
                        provenance_rejected = True
                        self.stats["agentjev_active_provenance_rejected"] += 1
                else:
                    approved = model_info.get("approved_tasks", [])
                    versions = model_info.get("task_versions", {})
                    thresholds = model_info.get("thresholds", {})
                    if not isinstance(approved, list):
                        approved = []
                    if not isinstance(versions, dict):
                        versions = {}
                    if not isinstance(thresholds, dict):
                        thresholds = {}
                    cohort = int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % 100
                    rollout = model_info.get("rollout_percent", 0)
                    rollout = rollout if type(rollout) in (int, float) else 0
                    for key, answer in student.items():
                        name = task_id(key)
                        options = len(questions[key].get("criteria", {})) if questions[key]["type"] != "noul" else 2
                        threshold = thresholds.get(f"{name}:{options}", thresholds.get(name))
                        uncertainty = answer_uncertainty(answer)
                        if (name in approved and versions.get(name) == TASK_VERSION and cohort < rollout
                                and isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
                                and 0 <= threshold <= .5 and uncertainty is not None and uncertainty <= threshold):
                            chosen[key], sources[key] = answer, student_backend
            missing = {k: q for k, q in questions.items() if k not in chosen}
            if mode == "active" and missing and self.cfg.decision_learning_jev_fallback and not model_only:
                items = list(missing.items())
                for start in range(0, len(items), 10):
                    batch = dict(items[start:start+10])
                    answers = await remote(self.host.jev.evaluate(state=state, questions=batch,
                        timeout=min(self.cfg.jev_timeout, max(.05, remaining() * .3))))
                    for key, answer in (answers or {}).items():
                        uncertainty = answer_uncertainty(answer)
                        if uncertainty is not None and uncertainty <= 1-self.cfg.jev_min_confidence:
                            chosen[key], sources[key] = answer, "jev"
                missing = {k: q for k, q in questions.items() if k not in chosen}
            online_missing = {key: question for key, question in missing.items()
                              if key not in OPTIONAL_TURN_QUESTIONS}
            if online_missing and not model_only:
                provider, teacher, teacher_latency_ms = (await prefetched_teacher if prefetched_teacher
                                                         else await fetch_teacher(online_missing))
                if teacher is None:
                    self.stats["teacher_invalid_response"] += 1
                    if self.stats["teacher_invalid_response"] in (1, 10):
                        logger.warning("Online decision teacher returned an invalid response: count=%d",
                                       self.stats["teacher_invalid_response"])
                teacher = teacher if teacher is not None else {}
                for key, answer in teacher.items():
                    if key in online_missing:
                        chosen[key], sources[key] = answer, "teacher"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats["failed"] += 1
            category = "timeout" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else type(exc).__name__
            self.stats["failed_" + category] += 1
            if self.stats["failed_" + category] in (1, 10):
                logger.warning("Online decision failed: category=%s count=%d", category,
                               self.stats["failed_" + category])
        finally:
            if prefetched_teacher is not None:
                if not prefetched_teacher.done():
                    prefetched_teacher.cancel()
                await asyncio.gather(prefetched_teacher, return_exceptions=True)
            if shadow_task is not None:
                if not shadow_task.done():
                    deferred_shadow = (mode == "shadow" and
                                       (student_backend == "kev" or
                                        (student_backend == "agentjev" and
                                         getattr(self.cfg, "agentjev_all_tasks_shadow", False)))
                                       and self.collecting(session_id) and source_state is not None)
                    student_status = "pending_background" if deferred_shadow else "teacher_finished_first"
                    if not deferred_shadow:
                        shadow_task.cancel()
                if not deferred_shadow:
                    await asyncio.gather(shadow_task, return_exceptions=True)
        if model_only:
            provider = "kev"
        if mode in ("shadow", "active"):
            self.stats["student_" + student_status] += 1
        if (self.closed or identity != self._identity(session_id)
                or (student_backend == "laya" and prepared and str(prepared.get("model_version", "")) != self.model_version)):
            self.stats["stale"] += 1
            if deferred_shadow:
                shadow_task.cancel()
                await asyncio.gather(shadow_task, return_exceptions=True)
            return None
        elapsed = time.monotonic() - started
        self.stats["requests"] += 1
        self.stats.update(sources.values())
        if mode == "active":
            self.stats["active_questions"] += len(questions)
            if hybrid_join:
                self.stats["teacher_parallel_tasks"] += sum(
                    source == "teacher" for key, source in sources.items() if key != "join")
                self.stats["teacher_fallback"] += sources.get("join") == "teacher"
            else:
                self.stats["teacher_fallback"] += sum(source == "teacher" for source in sources.values())
        for key in teacher.keys() & student.keys():
            kind = questions[key]["type"]
            field = {"choice": "choice", "noul": "noul", "score": "score"}[kind]
            a, b = teacher[key].get(field), student[key].get(field)
            differs = a != b if kind == "choice" else abs(a - b) >= .5
            if differs:
                self.disagreements.append({"task_id": task_id(key), "teacher": a, "student": b})
        decisions = {key: answer[field] for key, answer in chosen.items()
                     for field in ("choice", "score", "noul") if field in answer}
        recent_item = {"at": time.time(), "mode": mode, "session_id": str(session_id)[:256],
                            "strategy": ("kev_model_only" if model_only else
                                         "join_parallel_teacher" if hybrid_join else
                                         "kev_canary" if kev_canary else
                                         "teacher_only" if mode == "active" and student_backend == "kev"
                                         else "full_student"),
                            "tasks": list(questions), "sources": sources, "decisions": decisions,
                            "latency_ms": elapsed*1000, "student_latency_ms": student_latency_ms,
                            "preparation_latency_ms": preparation_latency_ms,
                            "student_timeout_ms": student_timeout_ms,
                            "student_input": student_input,
                            "teacher_latency_ms": teacher_latency_ms,
                            "provenance_rejected": provenance_rejected,
                            "model_version": model_info.get("model_version", ""),
                            "fallback": [k for k in questions if k not in chosen],
                            "teacher_fallback_tasks": [k for k, v in sources.items() if mode == "active" and v == "teacher"],
                            "student_status": student_status, "outcome": {}}
        if outcome_node is not None:
            from .outcome_recorder import read_outcome

            recent_item["outcome"] = read_outcome(outcome_node)
            previous_outcome = getattr(outcome_node, "_decision_learning_outcome", None)

            def observe_outcome(outcome):
                recent_item["outcome"] = {key: outcome[key] for key in
                    ("final_outcome", "delivered", "suppression_reason", "stage") if key in outcome}
                if callable(previous_outcome):
                    previous_outcome(outcome)

            outcome_node._decision_learning_outcome = observe_outcome
        self.recent.append(recent_item)
        if source_state is not None and self.collecting(session_id) and len(self.tasks) < 64:
            record_task = self._spawn(self._record(session_id, state, questions, teacher, student, sources,
                                                  provider, elapsed, model_info, outcome_node, collection_epoch,
                                                  source_state=source_state,
                                                  bot_speaker_id=student_bot_speaker_id,
                                                  tokenizer_prepared=student_backend == "laya" and bool(prepared),
                                                  student_status=student_status,
                                                  student_latency_ms=student_latency_ms,
                                                  student_timeout_ms=student_timeout_ms,
                                                  teacher_latency_ms=teacher_latency_ms,
                                                  preparation_latency_ms=preparation_latency_ms,
                                                  student_input=student_input,
                                                  model_only=model_only,
                                                  kev_canary_selected=kev_canary,
                                                  kev_canary_adopted=bool(kev_canary and sources.get("join") == "kev")))
            if deferred_shadow:
                async def finish_shadow():
                    try:
                        await asyncio.gather(shadow_task, return_exceptions=True)
                        sample_ids = await record_task
                        if (not sample_ids or self.closed or not self.collecting(session_id)
                                or self._collection_epochs[session_id] != collection_epoch):
                            return
                        for sample_id, key in sample_ids.items():
                            if key in student:
                                await self._io("update_student_prediction", sample_id, student[key],
                                    _student_training_status(key, student, model_info),
                                    model_info.get("model_version", ""))
                        self.stats["student_" + ("answered_background" if student else "unavailable_background")] += 1
                    finally:
                        if not shadow_task.done():
                            shadow_task.cancel()
                            await asyncio.gather(shadow_task, return_exceptions=True)
                self._spawn(finish_shadow())
        elif deferred_shadow:
            shadow_task.cancel()
            await asyncio.gather(shadow_task, return_exceptions=True)
        return chosen or None

    async def _record(self, session_id, state, questions, teacher, student, sources, provider, elapsed, model_info, outcome_node=None, collection_epoch=0, *, source_state=None, bot_speaker_id=None, tokenizer_prepared=True, student_status="not_requested", student_latency_ms=None, student_timeout_ms=None, teacher_latency_ms=None, preparation_latency_ms=None, student_input=None, model_only=False, kev_canary_selected=False, kev_canary_adopted=False):
        if self.closed or not self.enabled() or not self.collecting(session_id):
            return
        if not provider and not model_only:
            provider = self.cfg.decision_provider_id or await self.host.llm.resolve_provider_id(session_id)
        elif model_only:
            provider = "kev"
        sample_ids = {}
        request_id = uuid.uuid4().hex
        sample_request = not model_only and random.random() < self.cfg.decision_learning_sample_rate
        now = time.monotonic()
        if now - self._coverage_at >= 60:
            # This read opens its own SQLite connection. Do not hold the write
            # serialization lock while scanning coverage for annotation priority.
            self._coverage = (await asyncio.to_thread(self.store.coverage_summary)
                              if self.store is not None else await self._io("coverage_summary"))
            self._coverage_at = now
        priority, sampling_reasons = ((0, ()) if model_only else
            self._annotation_priority.choose(session_id, state, questions, student, self._coverage, now))
        targeted_request = priority > 0
        self.stats["annotation_targeted_requests"] += targeted_request
        self.stats["annotation_random_requests"] += sample_request
        for key, question in questions.items():
            if self._collection_epochs[session_id] != collection_epoch or not self.collecting(session_id):
                return
            training_status = _student_training_status(key, student, model_info)
            sid = await self._io("record_sample", session_id=session_id, task_id=task_id(key),
                state=state, candidates=question, teacher_label=teacher.get(key),
                teacher_model=getattr(teacher, "model", provider) or None,
                student_prediction=student.get(key), execution_source=sources.get(key, "rules"),
                task_version=TASK_VERSION,
                question_id=key, request_id=request_id, request_question_count=len(questions),
                student_training_status=training_status,
                student_model_version=model_info.get("model_version", "") if key in student else "",
                sampling_reasons=sampling_reasons, random_sample=sample_request,
                teacher_prompt_version=getattr(teacher, "prompt_version", TEACHER_PROMPT_VERSION),
                snapshot_version=SNAPSHOT_VERSION, source_state=source_state,
                bot_speaker_id=str(bot_speaker_id) if bot_speaker_id is not None else None,
                tokenizer_prepared=tokenizer_prepared,
                student_status=student_status,
                student_latency_ms=student_latency_ms,
                student_timeout_ms=student_timeout_ms,
                teacher_latency_ms=teacher_latency_ms,
                preparation_latency_ms=preparation_latency_ms,
                student_input=student_input or {},
                model_only=model_only,
                kev_canary_selected=kev_canary_selected,
                kev_canary_adopted=kev_canary_adopted,
                outcome={"latency_ms": elapsed*1000, "model_version": model_info.get("model_version", ""),
                         "student_training_status": training_status,
                         "question_id": key})
            sample_ids[sid] = key
            if model_only:
                continue
            if key in getattr(teacher, "abstentions", ()):
                await self._io("enqueue_label", sid, {"provider_id": provider, "question_id": key})
                await self._io("mark_review", sid, "insufficient_evidence")
                self.stats["teacher_insufficient_evidence"] += 1
            elif key not in teacher and (sources.get(key) != getattr(self.cfg, "decision_learning_student_backend", "kev")
                                         or sample_request or targeted_request):
                await self._io("enqueue_label", sid, {"provider_id": provider, "question_id": key},
                               priority=priority, reason=",".join(sampling_reasons) or "random_or_missing")
        await self._io("audit_request", request_id)
        if outcome_node is not None:
            from .outcome_recorder import read_outcome
            previous = getattr(outcome_node, "_decision_learning_outcome", None)
            sample_statuses = {sample_id: _student_training_status(key, student, model_info)
                               for sample_id, key in sample_ids.items()}
            async def save_outcome(outcome):
                for sample_id in sample_ids:
                    await self._io("update_outcome", sample_id, {**outcome,
                        "latency_ms": elapsed*1000, "model_version": model_info.get("model_version", ""),
                        "student_training_status": sample_statuses[sample_id]})
            def observed(outcome):
                if callable(previous):
                    previous(outcome)
                if not self.closed:
                    self._spawn(save_outcome(outcome))
            outcome_node._decision_learning_outcome = observed
            if read_outcome(outcome_node):
                await save_outcome(read_outcome(outcome_node))
        if time.time() - self._last_purge > 3600:
            await self._io("purge", retention_days=self.cfg.decision_learning_retention_days)
            self._last_purge = time.time()
        self.start_worker()
        return sample_ids

    def start_worker(self):
        if not self.closed and self.path.exists() and (self.maintenance is None or self.maintenance.done()):
            self.maintenance = self._spawn(self._maintain())
        if (self.enabled() and getattr(self.cfg, "decision_backend", "model") != "kev"
                and (self.worker is None or self.worker.done())):
            self.worker = self._spawn(self._label_workers())

    async def _maintain(self):
        while not self.closed:
            if time.time() - self._last_purge >= 3600:
                await self._io("purge", retention_days=self.cfg.decision_learning_retention_days)
                self._last_purge = time.time()
            await asyncio.sleep(60)

    async def _label_workers(self):
        workers = [asyncio.create_task(self._labels())
                   for _ in range(self.ANNOTATION_CONCURRENCY)]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _labels(self):
        consecutive_failures = 0
        annotation_timeout = max(30.0, self.cfg.decision_timeout)
        while self.enabled() and getattr(self.cfg, "decision_backend", "model") != "kev":
            job = None
            # Claim and reserve budget together: concurrent workers must not
            # pass the hourly limit while another claim awaits SQLite.
            async with self._label_claim_lock:
                now = time.monotonic()
                while self._label_times and self._label_times[0] < now - 3600:
                    self._label_times.popleft()
                if self.enabled() and len(self._label_times) < self.cfg.decision_learning_labels_per_hour:
                    job = await self._io("claim_label", session_ids=self.collection_sessions(),
                                         lease_seconds=max(90, annotation_timeout * 2 + 5))
                    if job:
                        self._label_times.append(time.monotonic())
            if not job:
                await asyncio.sleep(1)
                continue
            try:
                sample = job["sample"]
                payload = job["payload"]
                key = payload["question_id"]
                provider_id = payload["provider_id"]
                try:
                    validate_snapshot(sample["state"], {key: sample["candidates"]})
                except ValueError:
                    await self._io("mark_review", job["sample_id"], "invalid_snapshot")
                    self.stats["invalid_snapshot"] += 1
                    continue
                try:
                    answers = await asyncio.wait_for(self._teacher(provider_id, sample["state"],
                        {key: sample["candidates"]}, background=True), timeout=annotation_timeout)
                except Exception as exc:
                    if type(exc).__name__ != "ProviderNotFoundError":
                        raise
                    replacement = ""
                    for session in self.collection_sessions():
                        if await self._io("anonymous_id", session) == sample["session_id"]:
                            replacement = self.cfg.decision_provider_id or await self.host.llm.resolve_provider_id(session)
                            break
                    if not replacement or replacement == provider_id:
                        raise
                    provider_id = replacement
                    self.stats["label_provider_reresolved"] += 1
                    answers = await asyncio.wait_for(self._teacher(provider_id, sample["state"],
                        {key: sample["candidates"]}, background=True), timeout=annotation_timeout)
                if key in getattr(answers, "abstentions", ()):
                    await self._io("mark_review", job["sample_id"], "insufficient_evidence")
                    self.stats["teacher_insufficient_evidence"] += 1
                    consecutive_failures = 0
                    continue
                completed = await self._io("complete_label", job["sample_id"], job["token"], (answers or {}).get(key),
                                           teacher_model=getattr(answers, "model", provider_id),
                                           teacher_prompt_version=getattr(answers, "prompt_version", TEACHER_PROMPT_VERSION))
                if not completed:
                    self.stats["label_lease_lost"] += 1
                consecutive_failures = 0
            except asyncio.CancelledError:
                if job is not None:
                    await self._io("fail_label", job["sample_id"], job["token"], "cancelled",
                                   transient=True)
                raise
            except Exception as exc:
                self.stats["label_failed"] += 1
                category = "timeout" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else type(exc).__name__
                self.stats["label_failed_" + category] += 1
                if self.stats["label_failed_" + category] in (1, 10) or self.stats["label_failed_" + category] % 100 == 0:
                    logger.warning("Decision annotation failed: category=%s count=%d", category,
                                   self.stats["label_failed_" + category])
                if job is not None:
                    transient = category in {"timeout", "ProviderNotFoundError"}
                    retry_after = (min(3600, 30 * 2 ** min(job["attempts"] - 1, 7)) if transient
                                   else min(60, 2 ** min(job["attempts"] - 1, 5)))
                    await self._io("fail_label", job["sample_id"], job["token"], category,
                                   transient=transient, retry_after=retry_after)
                consecutive_failures += 1
                # Providers may not be ready during host startup. Do not burn
                # the full hourly budget in a burst of immediate failures.
                await asyncio.sleep(30 if category == "ProviderNotFoundError" else
                                    min(15, 2 ** min(consecutive_failures - 1, 4)))

    async def snapshot(self):
        local = {}
        if self.store is not None or self.path.exists():
            dataset_size = self.path.stat().st_size if self.path.exists() else 0
            large_dataset = dataset_size >= 64 * 1024 * 1024
            refresh_interval = 3600 if dataset_size >= 1024 * 1024 * 1024 else 300 if large_dataset else 15
            signature = self._dataset_signature() if large_dataset else None
            if time.monotonic() - self._summary_at >= refresh_interval:
                if large_dataset and signature == self._summary_signature:
                    self._summary_at = time.monotonic()
                elif large_dataset:
                    # A multi-GB dataset must never hold up the plugin page.
                    if self._summary_task is None or self._summary_task.done():
                        self._summary_task = asyncio.create_task(self._refresh_summary())
                else:
                    await self._refresh_summary()
            local = self._summary_cache
        student_backend = getattr(self.cfg, "decision_learning_student_backend", "kev")
        if student_backend == "laya" and time.monotonic() - self._status_at > 5:
            token = os.environ.get("LAYA_ADMIN_TOKEN", "")
            if token:
                status = await self.host.laya.request_json("/admin/status", method="GET", token=token, timeout=2)
                self._service_status = status or {}
            self._status_at = time.monotonic()
        elif student_backend in ("agentjev", "kev"):
            service = getattr(self.host, student_backend, None)
            self._service_status = service.snapshot() if service is not None else {}
        counts = local.get("tasks", {})
        total = self.stats["active_questions"]
        service = self._service_status
        approved = service.get("metadata", {}).get("approved_tasks", []) if student_backend == "laya" else []
        latencies = sorted(row["latency_ms"] for row in self.recent)
        student_latencies = sorted(row["student_latency_ms"] for row in self.recent
                                   if isinstance(row.get("student_latency_ms"), (int, float)))
        model_only = (getattr(self.cfg, "decision_backend", "") == "kev" and
                      self.cfg.decision_learning_mode == "active" and student_backend == "kev")
        return {"mode": self.cfg.decision_learning_mode,
                "decision_backend": getattr(self.cfg, "decision_backend", ""),
                "model_only": model_only,
                "student_backend": student_backend,
                "online_management": student_backend == "laya",
                "tasks": [{"task_id": key, "samples": counts.get(key, {}).get("samples", 0),
                           "status": "approved" if key in approved else "pending" if self.cfg.decision_learning_mode != "off" else "off"} for key in TASKS],
                "model_id": next((r["model_version"] for r in reversed(self.recent) if r["model_version"]), ""),
                "takeover_rate": self.stats[student_backend] / total if total else 0,
                "teacher_fallback_rate": self.stats["teacher_fallback"] / total if total else 0,
                "p95_ms": latencies[min(len(latencies)-1, int(len(latencies)*.95))] if latencies else None,
                "student_p95_ms": student_latencies[min(len(student_latencies)-1, int(len(student_latencies)*.95))]
                                  if student_latencies else None,
                "stats": dict(self.stats), "dataset": local, "recent": list(self.recent),
                "dataset_summary_pending": bool(self._summary_task is not None and not self._summary_task.done()),
                "jobs": service.get("jobs", []) if student_backend == "laya" else [],
                "student_service": service, "disagreements": list(self.disagreements),
                "collecting_sessions": len(self.collection_sessions())}

    def _dataset_signature(self):
        # WAL changes do not necessarily change the main database file.
        result = []
        for path in (self.path, self.path.with_name(self.path.name + "-wal")):
            try:
                stat = path.stat()
                result.append((stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                result.append(None)
        return tuple(result)

    async def _refresh_summary(self):
        async with self._summary_lock:
            if self.closed:
                return
            signature = self._dataset_signature()
            try:
                summary = (await asyncio.to_thread(self.store.summary, self._summary_stop)
                           if self.store is not None else await self._io("summary", self._summary_stop))
            except Exception as exc:
                # A failed scan should not be restarted by every page poll.
                self._summary_at = time.monotonic() - 240
                logger.warning("Decision dataset summary failed: %s", type(exc).__name__)
                return
            self._summary_cache = summary
            self._summary_signature = signature
            self._summary_at = time.monotonic()

    async def compare_teacher(self, limit=6):
        """Bounded same-snapshot comparison; never overwrite training labels."""
        if getattr(self.cfg, "decision_backend", "model") == "kev":
            raise ValueError("llm_decision_disabled")
        if not 1 <= limit <= 6 or self._comparison_lock.locked():
            raise ValueError("comparison unavailable or limit exceeded")
        from .turn_decisions import wts_questions, gate_questions
        legacy_questions = {**wts_questions(), **gate_questions()}
        async with self._comparison_lock:
            allowed = {await self._io("anonymous_id", session): session for session in self.collection_sessions()}
            rows = [row for row in await self._io("samples") if row["session_id"] in allowed]
            selected, seen = [], set()
            for row in reversed(rows):
                task = row["task_id"]
                if task not in (*legacy_questions, "join") or task in seen:
                    continue
                selected.append(row)
                seen.add(task)
                if len(selected) == limit:
                    break
            reports = []
            deadline = time.monotonic() + 105
            for row in selected:
                session = allowed[row["session_id"]]
                if not self.collecting(session) or self.closed or time.monotonic() >= deadline:
                    break
                provider = self.cfg.decision_provider_id or await self.host.llm.resolve_provider_id(session)
                key = row["task_id"]
                old_questions = {key: legacy_questions.get(key, row["candidates"])}
                new_questions = rubric_questions(old_questions)

                async def run(version, questions):
                    try:
                        answer = await asyncio.wait_for(self._teacher(provider, row["state"], questions,
                            background=True, comparison=True, prompt_version=version), max(.001, min(30, deadline-time.monotonic())))
                        return answer, None if answer is not None else "invalid_answer"
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        return None, "timeout"
                    except Exception as exc:
                        return None, type(exc).__name__

                (old, old_error), (new, new_error) = await asyncio.gather(run("legacy-v1", old_questions), run(TEACHER_PROMPT_VERSION, new_questions))
                kind = row["candidates"]["type"]
                old_label = old.get(key, {}).get(kind) if old is not None else None
                new_label = new.get(key, {}).get(kind) if new is not None else None
                reports.append({"sample_id": row["id"], "task_id": key, "legacy_label": old_label,
                                "new_label": new_label, "legacy_model": getattr(old, "model", None),
                                "new_model": getattr(new, "model", None),
                                "legacy_error": old_error, "new_error": new_error,
                                "legacy_status": "failed" if old is None else "insufficient_evidence" if key in getattr(old, "abstentions", ()) else "labeled",
                                "new_status": "failed" if new is None else "insufficient_evidence" if key in getattr(new, "abstentions", ()) else "labeled"})
            return {"legacy_version": "legacy-v1", "new_version": TEACHER_PROMPT_VERSION,
                    "completed_pairs": sum(r["legacy_status"] != "failed" and r["new_status"] != "failed" for r in reports),
                    "comparisons": reports, "changed": sum(r["legacy_label"] != r["new_label"] for r in reports
                    if r["legacy_status"] != "failed" and r["new_status"] != "failed"),
                    "human_review_required": True, "labels_overwritten": False}

    async def management(self, action, payload):
        if action == "models/compare_teacher":
            return await self.compare_teacher(int(payload.get("limit", 6)))
        if action == "models/compare_jev":
            from .integrations.typesafe import SystemOneClient
            rows = [r for r in await self._io("samples") if r.get("teacher_label") is not None][-20:]
            reports = []
            client = SystemOneClient(enabled=True, base_url=self.cfg.jev_base_url,
                api_key=os.environ.get(self.cfg.jev_api_key_env, ""), model=self.cfg.jev_model,
                timeout=self.cfg.jev_timeout)
            try:
                end = time.monotonic() + 20
                for row in rows:
                    if time.monotonic() >= end:
                        break
                    key = row["task_id"]
                    answer = await client.evaluate(state=row["state"], questions={key: row["candidates"]},
                        timeout=max(.05, min(self.cfg.jev_timeout, end-time.monotonic())))
                    reports.append({"sample_id": row["id"], "task_id": key,
                                    "teacher": row["teacher_label"], "jev": (answer or {}).get(key)})
                return {"comparisons": reports, "model": client.snapshot().get("response_model", self.cfg.jev_model)}
            finally:
                await client.close()
        if action == "samples/export":
            cursor = str(payload.get("cursor", "0"))
            if not cursor.isdigit():
                raise ValueError("invalid_cursor")
            offset, limit = int(cursor), int(payload.get("limit", 1000))
            if offset < 0 or not 1 <= limit <= 1000:
                raise ValueError("invalid_page")
            return await self._io("sample_page", session_id=payload.get("session_key"), offset=offset, limit=limit)
        if action == "samples/delete":
            session = payload.get("session_key")
            if not isinstance(session, str) or not session:
                raise ValueError("session_id_required")
            self._collection_epochs[session] += 1
            return {"deleted": await self._io("delete_session", session)}
        if (getattr(self.cfg, "decision_learning_student_backend", "kev") == "agentjev"
                and action in ("jobs/create", "jobs/status", "jobs/cancel", "models/evaluate",
                               "models/promote", "models/rollback", "models/rollout")):
            raise ValueError("agentjev_management_not_configured")
        if action in ("jobs/create", "models/rollout"):
            filename = "decisions-" + uuid.uuid4().hex + ".jsonl"
            directory = Path(os.environ.get("DECISION_DATASETS_DIR", "/decision-datasets"))
            await self._io("export", directory / filename, teacher_only=action == "jobs/create",
                           task_version=TASK_VERSION if action == "jobs/create" else None)
            payload = {**payload, "dataset": filename}
        paths = {"jobs/create": ("POST", "/admin/jobs"), "jobs/status": ("GET", "/admin/jobs"),
                 "models/evaluate": ("POST", "/admin/evaluate"), "models/promote": ("POST", "/admin/promote"),
                 "models/rollback": ("POST", "/admin/rollback"), "models/rollout": ("POST", "/admin/rollout")}
        if action in ("jobs/status", "jobs/cancel") and payload.get("job_id"):
            job_id = str(payload["job_id"])
            if not all(c.isalnum() or c in "-_" for c in job_id) or len(job_id) > 100:
                raise ValueError("invalid_job_id")
            path = "/admin/jobs/" + job_id + ("/cancel" if action == "jobs/cancel" else "")
            method = "POST" if action == "jobs/cancel" else "GET"
        elif action in paths:
            method, path = paths[action]
        else:
            raise ValueError("unknown_action")
        token = os.environ.get("LAYA_ADMIN_TOKEN", "")
        if not token:
            raise ValueError("LAYA_ADMIN_TOKEN_missing")
        result = await self.host.laya.request_json(path, payload if method == "POST" else None,
                                                 method=method, timeout=30, token=token)
        if result is None:
            raise ValueError("laya_management_unavailable")
        return result

    async def close(self):
        self.closed = True
        self._summary_stop.set()
        if self._summary_task is not None:
            self._summary_task.cancel()
            await asyncio.gather(self._summary_task, return_exceptions=True)
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.store is not None:
            await self._io("close")
