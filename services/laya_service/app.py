"""Private-network API with authenticated administration and version-bound inputs."""

from contextlib import asynccontextmanager
import gc
import hmac
import json
import math
import os
from pathlib import Path
import threading
from collections import Counter
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .backend import LayaBackend, input_digest, prepare_state
from .jobs import Jobs, core_module, gpu_lock


def create_app(*, backend=None, state_root=None, models_root=None, datasets_root=None, admin_token=None):
    state_root = Path(state_root or os.environ.get("LAYA_STATE", "/state"))
    models_root = Path(models_root or os.environ.get("LAYA_MODELS", "/models"))
    datasets_root = Path(datasets_root or os.environ.get("LAYA_DATASETS", "/datasets"))
    token = admin_token if admin_token is not None else os.environ.get("LAYA_ADMIN_TOKEN", "")
    jobs = Jobs(state_root / "jobs")
    registry = core_module("registry").ModelRegistry(models_root / "registry")
    lock = threading.RLock()
    state = {
        "backend": backend,
        "error": None,
        "training": False,
        "tokenizer": None,
        "config": None,
        "tokenizer_version": None,
    }
    stop = threading.Event()
    prepare_rejections = Counter()

    def supervisor():
        lease = None
        while not stop.is_set():
            with lock:
                try:
                    queued = any(j["state"] in ("queued", "running") for j in jobs.list())
                    active = registry.status().get("model_id", "base")
                    current = state["backend"]
                    if queued or (current and current.version != active):
                        state["backend"] = None
                        del current
                        gc.collect()
                        import torch

                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                        if lease:
                            lease.__exit__(None, None, None)
                            lease = None
                    state["training"] = queued
                    if not queued and state["backend"] is None:
                        lease = gpu_lock(state_root, blocking=False)
                        lease.__enter__()
                        path = models_root / "base" if active == "base" else models_root / "registry" / active
                        if active != "base":
                            registry.validate(active)
                        state["backend"] = LayaBackend(path, os.environ.get("LAYA_DEVICE", "cuda"))
                        state["tokenizer"] = state["backend"].agent.tok
                        state["config"] = state["backend"].agent.cfg
                        state["tokenizer_version"] = state["backend"].version
                        state["error"] = None
                except Exception as exc:
                    state["error"] = str(exc)
                    if lease:
                        lease.__exit__(None, None, None)
                        lease = None
            stop.wait(1)
        with lock:
            state["backend"] = None
            if lease:
                lease.__exit__(None, None, None)

    @asynccontextmanager
    async def lifespan(app):
        thread = None
        if backend is None:
            thread = threading.Thread(target=supervisor, daemon=True)
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=30)

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        if request.url.path == "/prepare":
            category = {
                "invalid typed question": "invalid_typed_question",
                "critical context exceeds tokenizer budget": "critical_context_over_budget",
                "unstructured state exceeds tokenizer budget; send a structured snapshot": "unstructured_state_over_budget",
                "question exceeds tokenizer head budget": "question_head_over_budget",
            }.get(str(exc), "invalid_request")
            with lock:
                prepare_rejections[category] += 1
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def admin(authorization: str = Header(default="")):
        if not token or not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "administrator bearer token required")

    def ready_backend():
        if state["backend"] is None:
            raise HTTPException(503, "training in progress" if state["training"] else "model unavailable")
        return state["backend"]

    def metadata():
        current = state["backend"]
        active = registry.status()
        versions = {}
        if current and hasattr(current, "path"):
            path = current.path / "task_versions.json"
            if path.exists():
                versions = json.loads(path.read_text(encoding="utf-8"))
        calibration = current.calibration if current else {}
        return dict(
            model_version=current.version if current else None,
            task_versions=versions,
            approved_tasks=active.get("tasks", []) if current and active.get("model_id") == current.version else [],
            thresholds={k: 1.0 - v["threshold"] for k, v in calibration.items()},
            rollout_percent=active.get("rollout_percent", 0),
            calibrated=bool(calibration),
            device=current.device if current else None,
            training=state["training"],
        )

    @app.get("/healthz")
    def health():
        return {"alive": True}

    @app.get("/readyz")
    def ready():
        with lock:
            ready_backend()
            return metadata()

    @app.get("/metadata")
    def get_metadata():
        with lock:
            return metadata()

    @app.post("/prepare")
    def prepare(payload: dict):
        with lock:
            if "state" not in payload or "questions" not in payload:
                raise ValueError("state and questions required")
            current = state["backend"]
            if current:
                text = current.prepare(payload["state"], payload["questions"])
                version = current.version
            elif state["tokenizer"]:
                cfg = state["config"]
                text = prepare_state(
                    state["tokenizer"],
                    payload["state"],
                    payload["questions"],
                    cfg.get("max_len", 1024),
                    cfg.get("head_max_len", 192),
                )
                version = state["tokenizer_version"]
            else:
                raise HTTPException(503, "tokenizer unavailable")
            questions = payload["questions"]
            return dict(
                state=text,
                questions=questions,
                input_id=input_digest(text, questions, version),
                model_version=version,
                task_versions=metadata()["task_versions"],
            )

    @app.post("/predict")
    def predict(payload: dict):
        with lock:
            current = ready_backend()
            if "state" not in payload or "questions" not in payload:
                raise ValueError("state and questions required")
            expected = payload.get("expected_model_version", payload.get("model_version"))
            if expected is not None and expected != current.version:
                raise HTTPException(409, "model version changed; prepare again")
            text, questions = payload["state"], payload["questions"]
            if payload.get("input_id"):
                if payload["input_id"] != input_digest(text, questions, current.version):
                    raise HTTPException(409, "prepared input does not match")
                if current.prepare(text, questions) != text:
                    raise ValueError("input was not tokenizer bounded")
            else:
                text = current.prepare(text, questions)
            started = time.perf_counter()
            answers = current.predict(text, questions)
            return dict(answers=answers, metadata=dict(metadata(), latency_ms=(time.perf_counter() - started) * 1000))

    @app.get("/admin/status", dependencies=[Depends(admin)])
    def status():
        return dict(
            metadata=metadata(),
            registry=registry.status(),
            jobs=jobs.list(),
            error=state["error"],
            prepare_rejections=dict(prepare_rejections),
        )

    @app.get("/admin/jobs", dependencies=[Depends(admin)])
    def list_jobs():
        return {"jobs": jobs.list()}

    @app.post("/admin/jobs", dependencies=[Depends(admin)])
    def create_job(payload: dict):
        name = str(payload.get("dataset", ""))
        path = (datasets_root / name).resolve()
        if datasets_root.resolve() not in path.parents or not path.is_file():
            raise ValueError("dataset must exist within dataset volume")
        return jobs.create(name, payload.get("model_id", ""), payload.get("epochs", 3), seed=payload.get("seed", 42))

    @app.get("/admin/jobs/{job_id}", dependencies=[Depends(admin)])
    def job(job_id: str):
        try:
            return jobs.get(job_id)
        except FileNotFoundError:
            raise HTTPException(404, "job not found")

    @app.post("/admin/jobs/{job_id}/cancel", dependencies=[Depends(admin)])
    def cancel(job_id: str):
        try:
            return jobs.cancel(job_id)
        except FileNotFoundError:
            raise HTTPException(404, "job not found")

    @app.post("/admin/evaluate", dependencies=[Depends(admin)])
    def evaluation(payload: dict):
        model_id = payload.get("model_id", "")
        registry.validate(model_id)
        return json.loads((registry._model(model_id) / "evaluation.json").read_text(encoding="utf-8"))

    @app.post("/admin/promote", dependencies=[Depends(admin)])
    def promote(payload: dict):
        with lock:
            return registry.promote(payload.get("model_id", ""))

    @app.post("/admin/rollback", dependencies=[Depends(admin)])
    def rollback():
        with lock:
            return registry.rollback()

    @app.post("/admin/rollout", dependencies=[Depends(admin)])
    def rollout(payload: dict):
        path = (datasets_root / str(payload.get("dataset", ""))).resolve()
        if datasets_root.resolve() not in path.parents or not path.is_file():
            raise ValueError("comparison dataset must exist within dataset volume")
        with lock:
            active = registry.status()
            if not active:
                raise ValueError("no active model")
            comparisons = set()
            accepted = {task: {} for task in active.get("tasks", [])}
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                outcome, metadata_row = row.get("outcome") or {}, row.get("metadata") or {}
                if (
                    row.get("teacher_model")
                    and row.get("teacher_label") is not None
                    and row.get("student_prediction") is not None
                    and outcome.get("model_version") == active["model_id"]
                    and float(row.get("created_at", 0)) >= active["stage_started_at"]
                    and row.get("task_id") in active.get("tasks", [])
                    and metadata_row.get("request_id")
                ):
                    comparisons.add((row["session_id"], metadata_row["request_id"]))
                    if row.get("execution_source") == "laya":
                        kind = row.get("candidates", {}).get("type")
                        teacher = row["teacher_label"].get(kind)
                        student = row["student_prediction"].get(kind)
                        if kind == "choice":
                            options = row["candidates"].get("criteria", {})
                            if teacher not in options or student not in options:
                                raise ValueError("invalid choice comparison")
                            wrong = teacher != student
                        elif kind in ("noul", "score"):
                            if not all(
                                isinstance(value, (int, float)) and math.isfinite(value) for value in (teacher, student)
                            ):
                                raise ValueError("invalid numeric comparison")
                            if kind == "noul":
                                if not 0 <= teacher <= 1 or not 0 <= student <= 1:
                                    raise ValueError("invalid binary comparison")
                                wrong = (teacher >= 0.5) != (student >= 0.5)
                            else:
                                wrong = abs(teacher - student) > 0.5
                        else:
                            raise ValueError("unknown comparison task type")
                        identity = (
                            row["session_id"],
                            metadata_row["request_id"],
                            metadata_row.get("question_id", row["task_id"]),
                        )
                        accepted[row["task_id"]][identity] = bool(wrong)
            for task, outcomes in accepted.items():
                if len(outcomes) < 20:
                    raise ValueError(f"rollout requires 20 accepted labeled comparisons for {task}")
                if sum(outcomes.values()) / len(outcomes) > 0.05:
                    raise ValueError(f"accepted decision error exceeds 5% for {task}")
            # Registry expects lifetime count; this export is filtered to this stage.
            return registry.advance_rollout(active.get("comparison_baseline", 0) + len(comparisons))

    return app


def app_factory():
    return create_app()
