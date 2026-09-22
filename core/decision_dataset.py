"""Persistent teacher-only decision samples and durable annotation leases.

Caller owns opt-in authorization. IDs are HMAC pseudonyms, not reversible hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from datetime import datetime, timezone


try:
    from .decision_catalog import TASK_LABELS, DYNAMIC_TASKS
except ImportError:
    # Standalone service/CLI loaders load this module without the plugin package.
    import importlib.util

    _catalog_spec = importlib.util.spec_from_file_location(
        "decision_catalog", Path(__file__).with_name("decision_catalog.py")
    )
    _catalog_module = importlib.util.module_from_spec(_catalog_spec)
    _catalog_spec.loader.exec_module(_catalog_module)
    TASK_LABELS = _catalog_module.TASK_LABELS
    DYNAMIC_TASKS = _catalog_module.DYNAMIC_TASKS


def _coverage_label(answer, question, *, hard):
    if answer is None or not isinstance(question, dict):
        return None
    kind = question.get("type")
    if isinstance(answer, dict) and answer.get("type", kind) != kind:
        return None
    value = answer.get(kind) if isinstance(answer, dict) else answer
    if kind == "choice":
        return value if isinstance(value, str) and value in question.get("criteria", {}) else None
    if kind == "noul":
        if not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
            return None
        if hard and value not in (0, 1):
            return None
        return "true" if value >= 0.5 else "false"
    if kind == "score":
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            return None
        if not 0 <= value <= len(question.get("criteria", [])) - 1 or (hard and value != int(value)):
            return None
        return str(round(value))
    return None


class DecisionDataset:
    def __init__(self, path: str | Path, secret: str | bytes | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS samples (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, created REAL NOT NULL,
                    payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS sample_session ON samples(session);
                CREATE INDEX IF NOT EXISTS sample_request ON samples(json_extract(payload,'$.metadata.request_id')) WHERE json_valid(payload);
                CREATE TABLE IF NOT EXISTS labels (
                    sample TEXT PRIMARY KEY REFERENCES samples(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'pending', lease_until REAL DEFAULT 0,
                    token TEXT, attempts INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS derivatives (
                    path TEXT PRIMARY KEY, sample_ids TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            supplied = secret.encode() if isinstance(secret, str) else secret
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('secret_hex',?)", ((supplied or secrets.token_bytes(32)).hex(),)
            )
            stored = bytes.fromhex(db.execute("SELECT value FROM settings WHERE key='secret_hex'").fetchone()[0])
            if supplied is not None and not hmac.compare_digest(supplied, stored):
                raise ValueError("Secret does not match existing dataset")
            self.secret = stored
            if "payload" not in {r[1] for r in db.execute("PRAGMA table_info(labels)")}:
                db.execute("ALTER TABLE labels ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
            columns = {r[1] for r in db.execute("PRAGMA table_info(labels)")}
            if "priority" not in columns:
                db.execute("ALTER TABLE labels ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "reason" not in columns:
                db.execute("ALTER TABLE labels ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('claim_count','0')")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def anonymous_id(self, value: Any) -> str:
        if re.fullmatch(r"anon_[a-f0-9]{32}", str(value)):
            return str(value)
        return "anon_" + hmac.new(self.secret, str(value).encode(), hashlib.sha256).hexdigest()[:32]

    def anonymize(self, value):
        """Prepare the exact privacy-preserving snapshot shared by teacher/student."""
        return self._anonymize(value)

    def close(self):
        """Connections are scoped per operation; provided for runtime lifecycle parity."""

    def _anonymize(self, value, mapping=None):
        mapping = {} if mapping is None else mapping

        def discover(obj):
            if isinstance(obj, dict):
                for key, item in obj.items():
                    if key not in {"provider_id", "question_id", "task_id", "model_id", "sample_id", "dataset_id"} and (
                        key.endswith("_id")
                        or key.endswith("_ids")
                        or key in {"umo", "author", "sender", "recipient", "session", "reply_to"}
                    ):
                        for identity in item if isinstance(item, list) else [item]:
                            if isinstance(identity, (str, int)) and str(identity):
                                mapping[str(identity)] = self.anonymous_id(identity)
                    discover(item)
            elif isinstance(obj, list):
                for item in obj:
                    discover(item)

        discover(value)

        def replace(obj):
            if isinstance(obj, dict):
                return {mapping.get(str(k), k): replace(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [replace(v) for v in obj]
            if isinstance(obj, (str, int)) and str(obj) in mapping:
                return mapping[str(obj)]
            if isinstance(obj, str):
                # Replace identifiers embedded in rendered snapshots too.
                for raw in sorted(mapping, key=len, reverse=True):
                    obj = re.sub(r"(?<!\w)" + re.escape(raw) + r"(?!\w)", mapping[raw], obj)
            return obj

        return replace(value)

    def record_sample(
        self,
        session_id,
        task_id,
        state,
        candidates,
        *,
        teacher_label=None,
        teacher_model=None,
        student_prediction=None,
        execution_source="teacher",
        task_version="1",
        outcome=None,
        created_at=None,
        **metadata,
    ):
        if teacher_label is not None and not teacher_model:
            raise ValueError("Teacher label requires teacher model provenance")
        sample_id = uuid.uuid4().hex
        payload = dict(
            id=sample_id,
            session_id=str(session_id),
            task_id=task_id,
            task_version=str(task_version),
            state=state,
            candidates=candidates,
            teacher_label=teacher_label,
            teacher_model=teacher_model,
            student_prediction=student_prediction,
            execution_source=execution_source,
            outcome=outcome,
            metadata=metadata,
            created_at=time.time() if created_at is None else created_at,
        )
        # Sample/task versions are technical identifiers, not personal IDs.
        sensitive = self._anonymize(
            {
                k: payload[k]
                for k in ("session_id", "state", "candidates", "teacher_label", "student_prediction", "metadata")
            }
        )
        payload.update(sensitive)
        with self.connect() as db:
            db.execute(
                "INSERT INTO samples VALUES (?,?,?,?)",
                (sample_id, payload["session_id"], payload["created_at"], json.dumps(payload)),
            )
        return sample_id

    def samples(self, session_id=None):
        with self.connect() as db:
            if session_id is None:
                rows = db.execute("SELECT payload FROM samples ORDER BY created,id")
            else:
                rows = db.execute(
                    "SELECT payload FROM samples WHERE session=? ORDER BY created,id", (self.anonymous_id(session_id),)
                )
            return [json.loads(row[0]) for row in rows]

    def sample_page(self, session_id=None, *, offset=0, limit=1000):
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid sample page")
        with self.connect() as db:
            if session_id is None:
                rows = db.execute(
                    "SELECT payload FROM samples ORDER BY created,id LIMIT ? OFFSET ?", (limit + 1, offset)
                )
            else:
                rows = db.execute(
                    "SELECT payload FROM samples WHERE session=? ORDER BY created,id LIMIT ? OFFSET ?",
                    (self.anonymous_id(session_id), limit + 1, offset),
                )
            rows = [json.loads(row[0]) for row in rows]
        return {"records": rows[:limit], "next_cursor": str(offset + limit) if len(rows) > limit else None}

    def summary(self):
        def empty(labels):
            return {
                "samples": 0,
                "teacher_labels": 0,
                "student_predictions": 0,
                "valid_teacher_labels": 0,
                "class_counts": dict.fromkeys(labels, 0),
                "valid_pairs": 0,
                "sessions": 0,
                "days": {},
            }

        tasks = {name: empty(labels) for name, labels in TASK_LABELS.items()}
        quality_flags = {}
        teacher_prompt_versions = {}
        sessions = {}
        for sample in self.samples():
            name = sample["task_id"]
            counts = tasks.setdefault(name, empty([]))
            counts["samples"] += 1
            for flag in set(sample.get("metadata", {}).get("quality_flags", [])):
                quality_flags[flag] = quality_flags.get(flag, 0) + 1
                flags = counts.setdefault("quality_flags", {})
                flags[flag] = flags.get(flag, 0) + 1
            counts["teacher_labels"] += sample["teacher_label"] is not None
            if sample["teacher_label"] is not None:
                version = sample.get("metadata", {}).get("teacher_prompt_version") or "legacy-v1"
                teacher_prompt_versions[version] = teacher_prompt_versions.get(version, 0) + 1
            counts["student_predictions"] += sample["student_prediction"] is not None
            sessions.setdefault(name, set()).add(sample["session_id"])
            day = datetime.fromtimestamp(sample["created_at"], timezone.utc).date().isoformat()
            counts["days"][day] = counts["days"].get(day, 0) + 1
            question = sample["candidates"]
            if isinstance(question, dict):
                labels = (
                    list(question.get("criteria", {}))
                    if question.get("type") == "choice"
                    else [str(i) for i in range(len(question.get("criteria", [])))]
                    if question.get("type") == "score"
                    else []
                )
                for label in labels:
                    counts["class_counts"].setdefault(label, 0)
            teacher = (
                _coverage_label(sample["teacher_label"], question, hard=True) if sample.get("teacher_model") else None
            )
            student = _coverage_label(sample["student_prediction"], question, hard=False)
            if teacher is not None:
                counts["valid_teacher_labels"] += 1
                counts["class_counts"][teacher] = counts["class_counts"].get(teacher, 0) + 1
                counts["valid_pairs"] += student is not None
        for name, counts in tasks.items():
            counts["sessions"] = len(sessions.get(name, ()))
            counts["dynamic"] = name in DYNAMIC_TASKS
            counts["observed_labels"] = [label for label, count in counts["class_counts"].items() if count > 0]
            counts["collection_gap"] = {
                "samples": max(0, 500 - counts["valid_teacher_labels"]),
                "classes": {}
                if counts["dynamic"]
                else {label: max(0, 50 - count) for label, count in counts["class_counts"].items()},
                "basis": "raw_collection_not_test",
            }
        with self.connect() as db:
            queue = dict(db.execute("SELECT status,count(*) FROM labels GROUP BY status"))
        return {
            "tasks": tasks,
            "queue": queue,
            "total": sum(t["samples"] for t in tasks.values()),
            "quality_flags": quality_flags,
            "teacher_prompt_versions": teacher_prompt_versions,
        }

    def update_outcome(self, sample_id, outcome):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM samples WHERE id=?", (sample_id,)).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["outcome"] = self._anonymize(outcome)
                db.execute("UPDATE samples SET payload=? WHERE id=?", (json.dumps(payload), sample_id))

    def update_human_label(self, sample_id, label):
        """Attach independently reviewed hard truth, preserving teacher provenance."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM samples WHERE id=?", (sample_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown sample identifier")
            payload = json.loads(row[0])
            question = payload["candidates"]
            kind = question["type"]
            value = label.get(kind) if isinstance(label, dict) else label
            if kind == "choice":
                valid = isinstance(value, str) and value in question["criteria"]
            elif kind == "noul":
                valid = isinstance(value, (bool, int)) and value in (0, 1)
            elif kind == "score":
                valid = type(value) is int and 0 <= value < len(question["criteria"])
            else:
                valid = False
            if not valid:
                raise ValueError("Human label does not match question candidates")
            payload.setdefault("metadata", {}).update(human_label={kind: value}, human_reviewed_at=time.time())
            db.execute("UPDATE samples SET payload=? WHERE id=?", (json.dumps(payload), sample_id))
        return True

    def enqueue_label(self, sample_id, payload=None, *, priority=0, reason=""):
        if type(priority) is not int or not -(2**31) <= priority < 2**31:
            raise ValueError("Priority must be a 32-bit integer")
        if not isinstance(reason, str) or len(reason) > 256:
            raise ValueError("Reason must be at most 256 characters")
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO labels(sample,payload,priority,reason) VALUES (?,?,?,?)",
                (sample_id, json.dumps(self._anonymize(payload or {})), priority, reason),
            )

    def claim_label(self, *, lease_seconds=60, max_attempts=5, session_ids=None):
        now = time.time()
        allowed = None if session_ids is None else tuple({self.anonymous_id(s) for s in session_ids})
        if allowed == ():
            return None
        query = (
            "SELECT sample FROM labels JOIN samples ON samples.id=labels.sample "
            "WHERE status NOT IN ('done','review') AND lease_until<=? AND attempts<?"
        )
        parameters = (now, max_attempts)
        if allowed is not None:
            query += " AND samples.session IN (" + ",".join("?" for _ in allowed) + ")"
            parameters += allowed
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = int(db.execute("SELECT value FROM settings WHERE key='claim_count'").fetchone()[0])
            query += (
                " ORDER BY labels.rowid LIMIT 1"
                if (count + 1) % 5 == 0
                else " ORDER BY priority DESC,labels.rowid LIMIT 1"
            )
            row = db.execute(query, parameters).fetchone()
            if not row:
                return None
            db.execute("UPDATE settings SET value=? WHERE key='claim_count'", (str(count + 1),))
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE labels SET status='leased',lease_until=?,token=?,attempts=attempts+1 WHERE sample=?",
                (now + lease_seconds, token, row[0]),
            )
            payload = json.loads(db.execute("SELECT payload FROM samples WHERE id=?", row).fetchone()[0])
            job_payload = json.loads(db.execute("SELECT payload FROM labels WHERE sample=?", row).fetchone()[0])
            priority, reason = db.execute("SELECT priority,reason FROM labels WHERE sample=?", row).fetchone()
            return {
                "sample": payload,
                "sample_id": row[0],
                "token": token,
                "payload": job_payload,
                "priority": priority,
                "reason": reason,
            }

    def audit_request(self, request_id):
        """Flag teacher contradictions for review without changing any labels."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Request identifier is required")
        accepted = (request_id, self.anonymous_id(request_id))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM samples WHERE json_valid(payload) AND json_extract(payload,'$.metadata.request_id') IN (?,?)",
                    accepted,
                )
            ]
            flags = set()
            labels = {}
            for row in rows:
                teacher = (
                    _coverage_label(row.get("teacher_label"), row["candidates"], hard=True)
                    if row.get("teacher_model")
                    else None
                )
                student = _coverage_label(row.get("student_prediction"), row["candidates"], hard=False)
                labels.setdefault(row["task_id"], []).append(teacher)
                if (
                    row["task_id"] in {"join", "action", "target", "recipient"}
                    and teacher is not None
                    and student is not None
                    and teacher != student
                ):
                    flags.add("critical_teacher_student_disagreement")
            join = labels.get("join", [])
            actions = [label for label in labels.get("action", []) if label is not None]
            affirmative = any(label != "ignore" for label in actions)
            if ("false" in join and affirmative) or ("true" in join and "ignore" in actions):
                flags.add("join_action_conflict")
            # Missing labels are incomplete annotation, not evidence of contradiction.
            targets = labels.get("target", [])
            expected_counts = [r.get("metadata", {}).get("request_question_count") for r in rows]
            complete = bool(rows) and all(type(n) is int and n == len(rows) for n in expected_counts)
            if complete and affirmative and targets and all(label == "false" for label in targets):
                flags.add("affirmative_without_target")
            known = {"join_action_conflict", "affirmative_without_target", "critical_teacher_student_disagreement"}
            for row in rows:
                metadata = row.setdefault("metadata", {})
                metadata["quality_flags"] = sorted((set(metadata.get("quality_flags", [])) - known) | flags)
                db.execute("UPDATE samples SET payload=? WHERE id=?", (json.dumps(row), row["id"]))
        return {"request_id": request_id, "flags": sorted(flags), "samples": len(rows)}

    def mark_review(self, sample_id, reason):
        """Quarantine insufficient evidence without fabricating a teacher label."""
        if not isinstance(reason, str) or len(reason) > 256:
            raise ValueError("Review reason must be at most 256 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM samples WHERE id=?", (sample_id,)).fetchone()
            if row is None:
                return False
            payload = json.loads(row[0])
            metadata = payload.setdefault("metadata", {})
            metadata["quality_flags"] = sorted(set(metadata.get("quality_flags", [])) | {"insufficient_evidence"})
            metadata["review_reason"] = reason
            db.execute("UPDATE samples SET payload=? WHERE id=?", (json.dumps(payload), sample_id))
            db.execute(
                "INSERT INTO labels(sample,status,reason) VALUES (?,'review',?) "
                "ON CONFLICT(sample) DO UPDATE SET status='review',reason=excluded.reason,token=NULL,lease_until=0",
                (sample_id, reason),
            )
        return True

    def complete_label(self, sample_id, token, label, teacher_model, *, teacher_prompt_version="legacy-v1"):
        if not teacher_model or label is None:
            raise ValueError("Only actual teacher labels may complete annotation")
        if (
            not isinstance(teacher_prompt_version, str)
            or not teacher_prompt_version
            or len(teacher_prompt_version) > 128
        ):
            raise ValueError("Teacher prompt version is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT samples.payload FROM samples JOIN labels ON samples.id=labels.sample WHERE id=? AND token=? AND status='leased' AND lease_until>?",
                (sample_id, token, time.time()),
            ).fetchone()
            if not row:
                return False
            payload = json.loads(row[0])
            payload.update(teacher_label=self._anonymize(label), teacher_model=teacher_model)
            payload.setdefault("metadata", {})["teacher_prompt_version"] = teacher_prompt_version
            db.execute("UPDATE samples SET payload=? WHERE id=?", (json.dumps(payload), sample_id))
            db.execute("UPDATE labels SET status='done' WHERE sample=?", (sample_id,))
        request_id = payload.get("metadata", {}).get("request_id")
        if request_id:
            self.audit_request(request_id)
        return True

    def export(self, path, *, teacher_only=True, task_version=None):
        rows = [
            s
            for s in self.samples()
            if (not teacher_only or s["teacher_label"] is not None)
            and (task_version is None or str(s.get("task_version")) == str(task_version))
        ]
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO derivatives VALUES (?,?)", (str(target), json.dumps([r["id"] for r in rows]))
            )
        return len(rows)

    def _delete(self, clause, args):
        with self.connect() as db:
            ids = {r[0] for r in db.execute("SELECT id FROM samples WHERE " + clause, args)}
            for path, members in db.execute("SELECT path,sample_ids FROM derivatives").fetchall():
                if ids.intersection(json.loads(members)):
                    Path(path).unlink(missing_ok=True)
                    db.execute("DELETE FROM derivatives WHERE path=?", (path,))
            db.execute("DELETE FROM samples WHERE " + clause, args)
            return len(ids)

    def delete_session(self, session_id):
        return self._delete("session=?", (self.anonymous_id(session_id),))

    def purge(self, *, retention_days=30, now=None):
        return self._delete("created<?", ((time.time() if now is None else now) - retention_days * 86400,))


def _pure_persona_sample(row):
    """Only the known context-free persona contract can bypass chat episodes."""
    state = row.get("state")
    if isinstance(state, str):
        decoder = json.JSONDecoder()
        remaining = state.strip()
        objects = []
        try:
            while remaining and len(objects) < 2:
                value, end = decoder.raw_decode(remaining)
                if not isinstance(value, dict):
                    return False
                objects.append(value)
                remaining = remaining[end:].strip()
        except (ValueError, RecursionError):
            return False
        if remaining or not objects:
            return False
        state = {}
        for value in objects:
            if set(state) & set(value):
                return False
            state.update(value)
    return (
        str(row.get("task_id", "")).startswith("persona.")
        and isinstance(state, dict)
        and isinstance(state.get("character_card"), str)
        and set(state) <= {"character_card", "persona_fingerprint"}
    )


def _sample_groups(samples, *, episode_gap_seconds=1800):
    """Union continuous episodes and near duplicates; hold newest groups out.

    A session starts a new episode after at least 30 minutes without a sample.
    Character trigram Jaccard >= .85 joins duplicate snapshots across episodes.
    Fewer than three independent groups cannot populate all three partitions.
    """
    if episode_gap_seconds <= 0:
        raise ValueError("Episode gap must be positive")
    rows = sorted(samples, key=lambda row: (row["created_at"], row.get("id", "")))
    parent = list(range(len(rows)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    grams = []
    frequencies = {}
    for row in rows:
        text = re.sub(r"\W+", "", json.dumps(row["state"], ensure_ascii=False, sort_keys=True).lower())
        gram = frozenset(text[j : j + 3] for j in range(max(1, len(text) - 2)))
        grams.append(gram)
        for token in gram:
            frequencies[token] = frequencies.get(token, 0) + 1
    postings = {}
    identical = {}
    for i, row in enumerate(rows):
        session = row["session_id"]
        domain = "persona" if _pure_persona_sample(row) else "conversation"
        if domain == "conversation":
            if session in seen and row["created_at"] - rows[seen[session]]["created_at"] < episode_gap_seconds:
                parent[root(i)] = root(seen[session])
            seen[session] = i
        gram = grams[i]
        identity = (domain, gram)
        if identity in identical:
            parent[root(i)] = root(identical[identity])
            continue
        # Any Jaccard >= .85 match overlaps at least ceil(.85 * |gram|)
        # tokens. Thus it must intersect this prefix, even in the worst case.
        # Rare-first order limits candidate fanout without approximate hashing.
        prefix_size = len(gram) - (85 * len(gram) + 99) // 100 + 1
        prefix = sorted(gram, key=lambda token: (frequencies[token], token))[:prefix_size]
        candidates = set()
        for token in prefix:
            candidates.update(postings.get((domain, token), ()))
        for j in sorted(candidates):
            if root(i) == root(j):
                continue
            previous = grams[j]
            if 100 * min(len(gram), len(previous)) < 85 * max(len(gram), len(previous)):
                continue
            overlap = len(gram & previous)
            if 100 * overlap >= 85 * (len(gram) + len(previous) - overlap):
                parent[root(i)] = root(j)
        identical[identity] = i
        for token in gram:
            postings.setdefault((domain, token), []).append(i)
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(root(i), []).append(row)
    ordered = sorted(groups.values(), key=lambda g: max(r["created_at"] for r in g))
    return ordered


def split_samples(samples, *, episode_gap_seconds=1800):
    """Return exact episode/near-duplicate groups in chronological partitions."""
    ordered = _sample_groups(samples, episode_gap_seconds=episode_gap_seconds)
    n = len(ordered)
    train_end = int(n * 0.7)
    calibration_end = int(n * 0.85)
    if n >= 3:
        train_end = min(max(1, train_end), n - 2)
        calibration_end = min(max(train_end + 1, calibration_end), n - 1)
    return {
        name: [r for group in subset for r in group]
        for name, subset in (
            ("train", ordered[:train_end]),
            ("calibration", ordered[train_end:calibration_end]),
            ("test", ordered[calibration_end:]),
        )
    }


def split_report(samples, partitions, *, episode_gap_seconds=1800):
    """Aggregate split evidence only; never return snapshots or personal IDs."""
    rows = list(samples)
    groups = _sample_groups(rows, episode_gap_seconds=episode_gap_seconds)
    group_for_id = {row["id"]: index for index, group in enumerate(groups) for row in group}
    result = {
        "samples": len(rows),
        "groups": len(groups),
        "max_group_size": max(map(len, groups), default=0),
        "sessions": len({row["session_id"] for row in rows}),
        "partitions": {},
    }
    for name in partitions:
        subset = list(partitions.get(name, []))
        tasks = {}
        sizes = {}
        dates = []
        for row in subset:
            group = group_for_id[row["id"]]
            sizes[group] = sizes.get(group, 0) + 1
            task = tasks.setdefault(row["task_id"], {"samples": 0, "class_counts": {}})
            task["samples"] += 1
            label = _coverage_label(row.get("teacher_label"), row.get("candidates", {}), hard=True)
            if label is not None and row.get("teacher_model"):
                task["class_counts"][label] = task["class_counts"].get(label, 0) + 1
            dates.append(datetime.fromtimestamp(row["created_at"], timezone.utc).date().isoformat())
        result["partitions"][name] = {
            "samples": len(subset),
            "groups": len(sizes),
            "max_group_size": max(sizes.values(), default=0),
            "sessions": len({row["session_id"] for row in subset}),
            "tasks": tasks,
            "date_start": min(dates, default=None),
            "date_end": max(dates, default=None),
        }
    return result
