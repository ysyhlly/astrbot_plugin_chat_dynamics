"""Durable jobs and a separate GPU worker; shared flock excludes inference."""

import importlib.util
import json
import os
from pathlib import Path
import re
import time
import uuid
from contextlib import contextmanager

from .backend import LayaBackend, canonical, task_name, validate_snapshot
from .training import calibrate, label_index, train


def core_module(name):
    path = Path(__file__).resolve().parents[2] / "core" / f"decision_{name}.py"
    spec = importlib.util.spec_from_file_location(f"laya_decision_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(canonical(data), encoding="utf-8")
    os.replace(temp, path)


@contextmanager
def gpu_lock(root, *, blocking=True, name="gpu.lock"):
    """Linux flock releases automatically after process/container failure."""
    import fcntl

    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Jobs:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid job id")
        return self.root / f"{job_id}.json"

    def list(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*.json"))]

    def get(self, job_id):
        return json.loads(self.path(job_id).read_text(encoding="utf-8"))

    def create(self, dataset, model_id, epochs=3, *, seed=42):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", model_id):
            raise ValueError("invalid model id")
        if model_id in ("base", "active.json"):
            raise ValueError("reserved model id")
        if not 1 <= int(epochs) <= 50:
            raise ValueError("epochs must be 1..50")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 1 <= seed <= 2**31:
            raise ValueError("seed must be an integer in 1..2**31")
        job = dict(
            id=uuid.uuid4().hex,
            dataset=dataset,
            model_id=model_id,
            epochs=int(epochs),
            seed=seed,
            state="queued",
            created_at=time.time(),
        )
        atomic_json(self.path(job["id"]), job)
        return job

    def cancel(self, job_id):
        job = self.get(job_id)
        self.path(job_id).with_suffix(".cancel").touch()
        if job["state"] == "queued":
            job.update(state="cancelled", finished_at=time.time())
            atomic_json(self.path(job_id), job)
        return dict(job, cancel_requested=True)

    def cancelled(self, job_id):
        return self.path(job_id).with_suffix(".cancel").exists()


def evaluate(backend, rows, *, predictor=None, cancel=lambda: False):
    """Evaluate complete same-input groups; time the entire decision call."""
    from collections import defaultdict

    groups, records, kinds, required_labels = defaultdict(list), defaultdict(list), {}, {}
    for row in rows:
        groups[
            (row["session_id"], row.get("metadata", {}).get("request_id", row["id"]), canonical(row["state"]))
        ].append(row)
    incomplete_requests = 0
    request_records = []
    for group in groups.values():
        if cancel():
            raise InterruptedError("evaluation cancelled")
        questions = {row.get("metadata", {}).get("question_id", row["task_id"]): row["candidates"] for row in group}
        counts = {row.get("metadata", {}).get("request_question_count") for row in group}
        if counts != {len(questions)} or len(questions) != len(group):
            # Partially labeled requests must not masquerade as full-request latency.
            incomplete_requests += 1
            continue
        started = time.perf_counter()
        answers = (predictor or backend.predict)(group[0]["state"], questions)
        latency = (time.perf_counter() - started) * 1000
        for row in group:
            qid, q = row.get("metadata", {}).get("question_id", row["task_id"]), row["candidates"]
            task = task_name(qid)
            answer = answers[qid]
            kind = q["type"]
            kinds[task] = kind
            label = label_index(row)
            prediction = (
                list(q["criteria"]).index(answer["choice"])
                if kind == "choice"
                else (int(answer["noul"] >= 0.5) if kind == "noul" else answer["score"])
            )
            options = 2 if kind == "noul" else len(q["criteria"])
            required_labels[task] = list(range(options))
            threshold = backend.calibration.get(f"{task}:{options}", {}).get("threshold", 1.01)
            record = dict(
                task_id=task,
                question_id=qid,
                request_id=canonical([row["session_id"], row.get("metadata", {}).get("request_id")])
                if row.get("metadata", {}).get("request_id")
                else None,
                request_question_count=row.get("metadata", {}).get("request_question_count"),
                label=label,
                prediction=prediction,
                confidence=answer["confidence"],
                accepted=answer["confidence"] >= threshold,
                latency_ms=latency,
            )
            if kind == "score":
                record["prediction_class"] = int(max(answer["probabilities"], key=answer["probabilities"].get))
            if task == "action":
                record["prediction_action"] = answer["choice"]
            metadata = row.get("metadata", {})
            if "human_label" in metadata:
                reviewed = dict(row, teacher_label=metadata["human_label"])
                record.update(human_label=label_index(reviewed), teacher_prediction=label)
            records[task].append(record)
            request_records.append(record)
    evaluation = core_module("evaluation")
    evaluate_task = evaluation.evaluate_task
    return {
        "incomplete_requests": incomplete_requests,
        "target_sets": {**evaluation.evaluate_target_sets(request_records), "population": "complete_requests_only"},
        "latency_scope": "http_prepare_and_predict_complete_sampled_request"
        if predictor
        else "local_forward_complete_sampled_request",
        "tasks": {
            task: evaluate_task(
                values,
                critical=task in ("join", "target", "recipient"),
                kind=kinds[task],
                required_labels=required_labels[task],
            )
            for task, values in records.items()
        },
    }


@contextmanager
def http_predictor(backend):
    """Benchmark the production FastAPI contract over a real loopback socket."""
    import socket
    import tempfile
    import threading
    import urllib.request
    import uvicorn
    from .app import create_app

    with tempfile.TemporaryDirectory(prefix="laya-benchmark-") as directory:
        root = Path(directory)
        app = create_app(
            backend=backend,
            state_root=root / "state",
            models_root=root / "models",
            datasets_root=root / "datasets",
            admin_token="",
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("benchmark HTTP server failed to start")
                time.sleep(0.01)

            def post(path, payload):
                request = urllib.request.Request(
                    base + path, data=canonical(payload).encode(), headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)

            def predict(state, questions):
                prepared = post("/prepare", {"state": state, "questions": questions})
                return post("/predict", prepared)["answers"]

            yield predict
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            sock.close()


def training_partitions(rows):
    """Validation is carved exclusively out of the original training partition."""
    dataset = core_module("dataset")
    splits = dataset.split_samples(rows)
    if any(not splits[name] for name in ("train", "calibration", "test")):
        raise ValueError("need disjoint train/calibration/test groups")
    inner = dataset.split_samples(splits["train"])
    splits["train"] = inner["train"] + inner["calibration"]
    splits["validation"] = inner["test"]
    if not splits["train"] or not splits["validation"]:
        raise ValueError("training partition needs independent groups for validation")
    return splits


def dataset_provenance(rows):
    """Reject mixed contracts before partitioning or loading a GPU model."""
    versions, prompt_versions = {}, {}
    for row in rows:
        task = task_name(row["task_id"])
        raw_version = row.get("task_version", "1")
        if isinstance(raw_version, bool) or not isinstance(raw_version, (str, int)) or not str(raw_version):
            raise ValueError(f"invalid task version for {task}")
        version = str(raw_version)
        if task in versions and versions[task] != version:
            raise ValueError(f"mixed task versions for {task}")
        versions[task] = version
        prompt = row.get("metadata", {}).get("teacher_prompt_version") or "legacy-v1"
        if not isinstance(prompt, str) or len(prompt) > 128:
            raise ValueError("invalid teacher prompt version")
        prompt_versions[prompt] = prompt_versions.get(prompt, 0) + 1
    return {"task_versions": versions, "teacher_prompt_versions": prompt_versions}


def run_job(job, jobs, models, datasets):
    data_path = (Path(datasets) / job["dataset"]).resolve()
    if Path(datasets).resolve() not in data_path.parents or not data_path.is_file():
        raise ValueError("dataset must be a file inside dataset volume")
    rows = [json.loads(line) for line in data_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(not row.get("teacher_model") or row.get("teacher_label") is None for row in rows):
        raise ValueError("training requires genuine teacher labels and model provenance")
    provenance = dataset_provenance(rows)
    for row in rows:
        validate_snapshot(row["state"], {
            row.get("metadata", {}).get("question_id", row["task_id"]): row["candidates"]})
    splits = training_partitions(rows)
    split_report = core_module("dataset").split_report(rows, splits)
    staging = Path(models) / "staging" / job["id"]
    job.update(state="running", started_at=time.time())

    def progress(**kwargs):
        job.update(progress=kwargs)
        atomic_json(jobs.path(job["id"]), job)

    progress()
    train(
        Path(models) / "base",
        splits["train"],
        staging,
        epochs=job["epochs"],
        seed=job.get("seed", 42),
        validation_rows=splits["validation"],
        cancel=lambda: jobs.cancelled(job["id"]),
        progress=progress,
    )
    atomic_json(staging / "split_report.json", split_report)
    atomic_json(staging / "provenance.json", provenance)
    import gc
    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    if jobs.cancelled(job["id"]):
        raise InterruptedError("cancelled")
    backend = LayaBackend(staging)
    backend.calibration = calibrate(backend, splits["calibration"], cancel=lambda: jobs.cancelled(job["id"]))
    atomic_json(staging / "calibration.json", backend.calibration)
    with http_predictor(backend) as predictor:
        report = evaluate(backend, splits["test"], predictor=predictor, cancel=lambda: jobs.cancelled(job["id"]))
    atomic_json(staging / "evaluation.json", report)
    atomic_json(staging / "task_versions.json", provenance["task_versions"])
    registry = core_module("registry").ModelRegistry(Path(models) / "registry")
    registry.register(job["model_id"], staging, {"job_id": job["id"], "seed": job.get("seed", 42), **provenance})
    job.update(state="completed", finished_at=time.time(), report=report)
    atomic_json(jobs.path(job["id"]), job)


def worker():
    root = Path(os.environ.get("LAYA_STATE", "/state"))
    with gpu_lock(root, blocking=False, name="worker.lock"):
        _worker_loop(root)


def _worker_loop(root):
    jobs = Jobs(root / "jobs")
    with gpu_lock(root, name="jobs.lock"):
        # Exclusive worker startup proves previous running jobs lost their worker.
        for job in jobs.list():
            if job["state"] == "running":
                job.update(state="interrupted", error="worker restarted; submit a new immutable run")
                atomic_json(jobs.path(job["id"]), job)
    while True:
        for job in jobs.list():
            if job["state"] != "queued":
                continue
            # Hold GPU lease until models and exception tracebacks are gone.
            with gpu_lock(root):
                _execute_job(job, jobs)
                import gc
                import torch

                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        time.sleep(2)


def _execute_job(job, jobs):
    try:
        if jobs.cancelled(job["id"]):
            raise InterruptedError("cancelled")
        run_job(job, jobs, os.environ.get("LAYA_MODELS", "/models"), os.environ.get("LAYA_DATASETS", "/datasets"))
    except BaseException as exc:
        job.update(
            state="cancelled" if isinstance(exc, InterruptedError) else "failed",
            error=str(exc),
            finished_at=time.time(),
        )
        atomic_json(jobs.path(job["id"]), job)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
