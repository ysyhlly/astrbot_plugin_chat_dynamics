"""Checksummed immutable model registry with explicit promotion and rollback."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path


class ModelRegistry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _model(self, model_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", model_id) or model_id in {".", ".."}:
            raise ValueError("Invalid model identifier")
        return self.root / model_id

    def register(self, model_id, source_dir, metadata=None):
        destination = self._model(model_id)
        if destination.exists():
            raise ValueError("Model versions are immutable")
        source = Path(source_dir).resolve()
        files = [p for p in source.rglob("*") if p.is_file() and p.name != "manifest.json"]
        if not files or any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Model must contain regular artifact files")
        manifest = dict(
            model_id=model_id,
            created_at=time.time(),
            metadata=metadata or {},
            files={p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        )
        temp = self.root / (".register-" + uuid.uuid4().hex)
        try:
            shutil.copytree(source, temp)
            (temp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            temp.rename(destination)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
        return manifest

    def validate(self, model_id):
        directory = self._model(model_id)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("model_id") != model_id:
            raise ValueError("Manifest identity mismatch")
        actual = {
            p.relative_to(directory).as_posix()
            for p in directory.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        if actual != set(manifest["files"]):
            raise ValueError("Artifact inventory changed")
        for name, expected in manifest["files"].items():
            path = directory / name
            if (
                directory not in path.resolve().parents
                or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("Artifact checksum mismatch")
        return manifest

    def status(self):
        path = self.root / "active.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _save(self, value):
        temp = self.root / (".active-" + uuid.uuid4().hex)
        temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(temp, self.root / "active.json")
        return value

    def promote(self, model_id, report=None):
        self.validate(model_id)
        stored = json.loads((self._model(model_id) / "evaluation.json").read_text(encoding="utf-8"))
        if report is not None and report != stored:
            raise ValueError("Report must match immutable evaluation artifact")
        tasks = stored.get("tasks", {})
        eligible = [task for task, metrics in tasks.items() if self._passed(metrics, task)
                    and (task != "target" or self._target_sets_passed(stored.get("target_sets", {})))]
        if not eligible:
            raise ValueError("No tasks passed promotion gates")
        old = self.status()
        if old.get("model_id") == model_id:
            raise ValueError("Already active")
        return self._save(
            dict(
                model_id=model_id,
                previous=old or None,
                tasks=eligible,
                rollout_percent=10,
                stage_started_at=time.time(),
                comparison_baseline=0,
            )
        )

    @staticmethod
    def _target_sets_passed(metrics):
        try:
            return (metrics.get("eligible") is True and not metrics.get("reasons")
                    and metrics["count"] >= 500 and metrics["invalid_plans"] == 0
                    and .8 <= metrics["coverage"] <= 1
                    and 0 <= metrics["set_error_ci"][1] <= .05
                    and 0 <= metrics["accepted_error_ci"][1] <= .05)
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _passed(metrics, task=""):
        """Do not accept a hand-written eligible flag without measured evidence."""
        try:
            required = ["count", "ece", "coverage", "accepted_error", "p95_ms"]
            if any(not math.isfinite(float(metrics[k])) for k in required):
                return False
            if metrics.get("eligible") is not True or metrics.get("reasons"):
                return False
            if metrics["count"] < 500 or not metrics["class_counts"] or min(metrics["class_counts"].values()) < 50:
                return False
            if any(
                metrics["class_counts"].get(label, metrics["class_counts"].get(str(label), 0)) < 50
                for label in metrics.get("required_labels", [])
            ):
                return False
            if not 0 <= metrics["ece"] <= 0.05 or not 0.8 <= metrics["coverage"] <= 1:
                return False
            if not 0 <= metrics["accepted_error"] <= 0.05 or not 0 <= metrics["p95_ms"] <= 500:
                return False
            if (metrics["accepted_request_count"] < 20 or not 0 <= metrics["accepted_error_ci"][1] <= .05
                    or not 0 <= metrics["accepted_request_error_ci"][1] <= .05):
                return False
            if metrics.get("kind") == "score":
                if not 0 <= metrics["mae"] <= 0.5:
                    return False
            elif not 0.9 <= metrics["macro_f1"] <= 1:
                return False
            critical = metrics.get("critical") or task.split(".")[0].split(":")[0] in {"join", "target", "recipient"}
            if critical and (metrics["human_count"] < 500 or metrics["critical_error_delta_ci"][1] > 0.02):
                return False
            return True
        except (KeyError, TypeError, ValueError):
            return False

    def rollback(self):
        previous = self.status().get("previous")
        if not previous:
            raise ValueError("No previous stable model")
        self.validate(previous["model_id"])
        return self._save(previous)

    def advance_rollout(self, comparisons, elapsed_seconds=None):
        status = self.status()
        if not status:
            raise ValueError("No active model")
        elapsed = time.time() - status["stage_started_at"]
        # An explicit duration may narrow the observed duration, never fabricate it.
        if elapsed_seconds is not None:
            elapsed = min(elapsed, elapsed_seconds)
        if elapsed < 86400 or comparisons - status.get("comparison_baseline", 0) < 500:
            raise ValueError("Rollout requires 24 hours and 500 new valid comparisons")
        current = status["rollout_percent"]
        if current == 100:
            return status
        status.update(
            rollout_percent=50 if current == 10 else 100, stage_started_at=time.time(), comparison_baseline=comparisons
        )
        return self._save(status)
