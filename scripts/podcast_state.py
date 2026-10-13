#!/usr/bin/env python3
"""Transactional execution ledger for the podcast worker.

One append-only ``attempts`` journal stores action starts/outcomes, decisions,
fallback use and normalized failures. A process lock excludes concurrent worker
ticks; optimistic job revisions protect every state mutation. The separate
model arbiter retains ownership of GPU/model-resource locking and leases.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
POLICY_PATH = ROOT / "references" / "current" / "state-policy-v1.json"
DEFAULT_STATE_ROOT = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))

ACTIVE_STAGES = (
    "queued", "resolving", "acquiring_audio", "transcribing",
    "validating_transcript", "extracting", "consolidating", "planning",
    "drafting_sections", "drafting_frame", "rendering", "delivering",
)
TERMINAL_STAGES = ("complete", "blocked_needs_human", "failed_terminal", "cancelled")
ALL_STAGES = ACTIVE_STAGES + TERMINAL_STAGES
FORWARD = {stage: ACTIVE_STAGES[index + 1] for index, stage in enumerate(ACTIVE_STAGES[:-1])}
FORWARD["delivering"] = "complete"
FINAL_TERMINAL = {"complete", "failed_terminal", "cancelled"}
JOURNAL_KINDS = {"start", "success", "failure", "fallback_used", "decision"}


class StateKernelError(RuntimeError):
    """Base state-kernel failure."""


class NotFound(StateKernelError):
    pass


class RevisionConflict(StateKernelError):
    pass


class InvalidTransition(StateKernelError):
    pass


class DuplicateDeterministicAttempt(StateKernelError):
    pass


class CircuitBreakerOpen(StateKernelError):
    pass


class WorkerLockUnavailable(StateKernelError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def payload_hash(value: bytes | str | dict[str, Any] | list[Any]) -> str:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_policy() -> dict[str, Any]:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if not policy.get("failure_classes") or not policy.get("operation_attempt_ceilings"):
        raise StateKernelError("Incomplete state policy")
    return policy


class SingleWorkerLock:
    """Non-blocking process-level lock held for one complete sequential tick."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stream = None

    def acquire(self) -> "SingleWorkerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            stream.close()
            raise WorkerLockUnavailable(f"Another podcast worker holds {self.path}") from error
        stream.seek(0)
        stream.truncate()
        stream.write(f"pid={__import__('os').getpid()} acquired_at={iso()}\n")
        stream.flush()
        self._stream = stream
        return self

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "SingleWorkerLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


@contextmanager
def worker_session(state_root: str | Path = DEFAULT_STATE_ROOT) -> Iterator["PodcastStore"]:
    """Required state entry point for one complete podcast worker tick."""
    root = Path(state_root)
    with SingleWorkerLock(root / "worker.lock"):
        with PodcastStore(root / "podcast.sqlite3") as store:
            yield store


class PodcastStore:
    """SQLite repository with revisions and one append-only typed journal."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=timeout_seconds, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
        self.policy = load_policy()
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PodcastStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def migrate(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, filename TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {
            row["version"]: (row["filename"], row["sha256"])
            for row in self.connection.execute("SELECT version,filename,sha256 FROM schema_migrations")
        }
        for migration in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = int(migration.name.split("_", 1)[0])
            raw = migration.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            if version in applied:
                if applied[version] != (migration.name, checksum):
                    raise StateKernelError(f"Applied migration {version} differs from {migration.name}")
                continue
            filename_literal = migration.name.replace("'", "''")
            checksum_literal = checksum.replace("'", "''")
            applied_at_literal = iso().replace("'", "''")
            script = (
                "BEGIN IMMEDIATE;\n" + raw.decode("utf-8") + "\n"
                + "INSERT INTO schema_migrations(version,filename,sha256,applied_at) VALUES ("
                + f"{version},'{filename_literal}','{checksum_literal}','{applied_at_literal}');\nCOMMIT;"
            )
            try:
                self.connection.executescript(script)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def _job(self, db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"Unknown podcast job: {job_id}")
        return row

    @staticmethod
    def _check_revision(job: sqlite3.Row, expected_revision: int) -> None:
        if job["revision"] != expected_revision:
            raise RevisionConflict(
                f"Revision conflict for {job['job_id']}: expected {expected_revision}, current {job['revision']}"
            )

    def _journal(self, db: sqlite3.Connection, *, kind: str, job_id: str, revision: int,
                 stage_from: str | None = None, stage_to: str | None = None,
                 attempt_id: str | None = None, packet_id: str = "", packet_version: int = 0,
                 operation: str | None = None, operation_class: str | None = None,
                 request_sha256: str | None = None, attempt_number: int | None = None,
                 deterministic: bool | None = None, generation_started: bool | None = None,
                 model: str | None = None, prompt_budget: int | None = None,
                 completion_budget: int | None = None, prompt_tokens: int | None = None,
                 completion_tokens: int | None = None, elapsed_ms: int | None = None,
                 finish_reason: str | None = None, failure_class: str | None = None,
                 failure_code: str | None = None, diagnostic: str | None = None,
                 retryable: bool | None = None, payload: dict[str, Any] | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
        if kind not in JOURNAL_KINDS:
            raise ValueError(f"Unknown journal kind: {kind}")
        cursor = db.execute(
            "INSERT INTO attempts(attempt_id,kind,job_id,revision,stage_from,stage_to,packet_id,packet_version,"
            "operation,operation_class,payload_sha256,attempt_number,deterministic,generation_started,model,"
            "prompt_budget,completion_budget,prompt_tokens,completion_tokens,elapsed_ms,finish_reason,failure_class,"
            "failure_code,diagnostic,retryable,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, kind, job_id, revision, stage_from, stage_to, packet_id, packet_version,
             operation, operation_class, request_sha256, attempt_number,
             None if deterministic is None else int(deterministic),
             None if generation_started is None else int(generation_started), model, prompt_budget,
             completion_budget, prompt_tokens, completion_tokens, elapsed_ms, finish_reason, failure_class,
             failure_code, diagnostic, None if retryable is None else int(retryable),
             json.dumps(payload or {}, sort_keys=True, separators=(",", ":")), iso(now)),
        )
        return dict(db.execute("SELECT * FROM attempts WHERE journal_id=?", (cursor.lastrowid,)).fetchone())

    def create_job(self, job_id: str, source_identity: str, *, now: datetime | None = None) -> dict[str, Any]:
        if not job_id or not source_identity:
            raise ValueError("job_id and source_identity are required")
        timestamp = iso(now)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO jobs(job_id,source_identity,stage,created_at,updated_at) VALUES(?,?,'queued',?,?)",
                (job_id, source_identity, timestamp, timestamp),
            )
            self._journal(db, kind="decision", job_id=job_id, revision=0, stage_to="queued",
                          operation="job_created", now=now)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"Unknown podcast job: {job_id}")
        return dict(row)

    def list_journal(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM attempts WHERE job_id=? ORDER BY journal_id", (job_id,)
        )]

    def list_attempts(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM attempts WHERE job_id=? AND kind='start' ORDER BY journal_id", (job_id,)
        )]

    def list_failures(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM attempts WHERE job_id=? AND kind='failure' ORDER BY journal_id", (job_id,)
        )]

    def list_open_attempts(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT s.* FROM attempts s WHERE s.job_id=? AND s.kind='start' AND NOT EXISTS ("
            "SELECT 1 FROM attempts o WHERE o.attempt_id=s.attempt_id AND o.kind IN ('success','failure')) "
            "ORDER BY s.journal_id", (job_id,)
        )]

    def list_artifacts(self, job_id: str, artifact_type: str | None = None) -> list[dict[str, Any]]:
        if artifact_type is None:
            rows = self.connection.execute(
                "SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at,artifact_id", (job_id,)
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM artifacts WHERE job_id=? AND artifact_type=? ORDER BY created_at,artifact_id",
                (job_id, artifact_type),
            )
        return [dict(row) for row in rows]

    def get_packet(self, job_id: str, packet_id: str, packet_version: int = 1) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM packets WHERE job_id=? AND packet_id=? AND packet_version=?",
            (job_id, packet_id, packet_version),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_packets(self, job_id: str, stage: str | None = None) -> list[dict[str, Any]]:
        if stage is None:
            rows = self.connection.execute(
                "SELECT * FROM packets WHERE job_id=? ORDER BY packet_id,packet_version", (job_id,)
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM packets WHERE job_id=? AND stage=? ORDER BY packet_id,packet_version",
                (job_id, stage),
            )
        return [dict(row) for row in rows]

    @staticmethod
    def _verify_artifacts(artifacts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        verified: list[dict[str, Any]] = []
        for artifact in artifacts or []:
            path = Path(artifact["path"])
            if not path.is_file():
                raise StateKernelError(f"Artifact does not exist: {path}")
            if file_sha256(path) != artifact["sha256"] or path.stat().st_size != artifact["byte_size"]:
                raise StateKernelError(f"Artifact verification failed: {path}")
            verified.append(artifact)
        return verified

    @staticmethod
    def _insert_artifacts(db: sqlite3.Connection, job_id: str, producer_attempt_id: str | None,
                          artifacts: list[dict[str, Any]], now: datetime | None) -> None:
        for artifact in artifacts:
            db.execute(
                "INSERT INTO artifacts(artifact_id,job_id,artifact_type,path,sha256,byte_size,"
                "producer_attempt_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    artifact["artifact_id"], job_id, artifact["artifact_type"],
                    artifact["path"], artifact["sha256"], artifact["byte_size"], producer_attempt_id, iso(now),
                ),
            )

    def transition_job(self, job_id: str, target: str, *, expected_revision: int,
                       reason: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        if target not in ALL_STAGES:
            raise InvalidTransition(f"Unknown target stage: {target}")
        with self.transaction() as db:
            job = self._job(db, job_id)
            self._check_revision(job, expected_revision)
            source = job["stage"]
            allowed: set[str] = set()
            if source == "blocked_needs_human" and job["resume_stage"]:
                allowed.add(job["resume_stage"])
            elif source not in FINAL_TERMINAL:
                if source in FORWARD:
                    allowed.add(FORWARD[source])
                allowed.update({"blocked_needs_human", "failed_terminal", "cancelled"})
            if target not in allowed:
                raise InvalidTransition(f"Transition {source} -> {target} is not declared")
            revision = job["revision"] + 1
            resume_stage = source if target == "blocked_needs_human" else None
            db.execute(
                "UPDATE jobs SET stage=?,resume_stage=?,revision=?,updated_at=? WHERE job_id=?",
                (target, resume_stage, revision, iso(now), job_id),
            )
            self._journal(db, kind="decision", job_id=job_id, revision=revision,
                          stage_from=source, stage_to=target, operation="job_transition",
                          payload={"reason": reason} if reason else {}, now=now)
        return self.get_job(job_id)

    def record_decision(self, job_id: str, *, operation: str, expected_revision: int,
                        payload: dict[str, Any] | None = None, fallback_used: bool = False,
                        artifacts: list[dict[str, Any]] | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
        verified_artifacts = self._verify_artifacts(artifacts)
        with self.transaction() as db:
            job = self._job(db, job_id)
            self._check_revision(job, expected_revision)
            revision = job["revision"] + 1
            db.execute("UPDATE jobs SET revision=?,updated_at=? WHERE job_id=?", (revision, iso(now), job_id))
            self._insert_artifacts(db, job_id, None, verified_artifacts, now)
            return self._journal(
                db, kind="fallback_used" if fallback_used else "decision", job_id=job_id,
                revision=revision, stage_from=job["stage"], stage_to=job["stage"],
                operation=operation,
                payload={**(payload or {}), "artifact_ids": [item["artifact_id"] for item in verified_artifacts]},
                now=now,
            )

    def record_packets(self, job_id: str, packets: list[dict[str, Any]], *,
                       expected_revision: int, now: datetime | None = None) -> list[dict[str, Any]]:
        """Atomically publish an immutable packet plan; exact replay is a no-op."""
        if not packets:
            raise ValueError("At least one packet is required")
        with self.transaction() as db:
            job = self._job(db, job_id)
            self._check_revision(job, expected_revision)
            existing_rows: list[dict[str, Any]] = []
            missing: list[dict[str, Any]] = []
            for packet in packets:
                row = db.execute(
                    "SELECT * FROM packets WHERE job_id=? AND packet_id=? AND packet_version=?",
                    (job_id, packet["packet_id"], packet["packet_version"]),
                ).fetchone()
                if row is None:
                    missing.append(packet)
                    continue
                existing = dict(row)
                expected = {
                    "stage": packet["stage"],
                    "source_range_json": json.dumps(packet["source_range"], sort_keys=True, separators=(",", ":")),
                    "source_tokens": int(packet.get("source_tokens", 0)),
                    "primary_units": int(packet.get("primary_units", 0)),
                    "overlap_units": int(packet.get("overlap_units", 0)),
                    "prompt_tokens_estimate": int(packet.get("prompt_tokens_estimate", 0)),
                    "output_tokens_predicted": int(packet.get("output_tokens_predicted", 0)),
                    "payload_sha256": packet["payload_sha256"],
                }
                if any(existing[key] != value for key, value in expected.items()):
                    raise StateKernelError(
                        f"Packet identity collision: {packet['packet_id']} v{packet['packet_version']}"
                    )
                existing_rows.append(existing)
            if not missing:
                return sorted(existing_rows, key=lambda row: (row["packet_id"], row["packet_version"]))
            if existing_rows:
                raise StateKernelError("Packet plan is only partially present; explicit reconciliation is required")
            created_at = iso(now)
            for packet in missing:
                db.execute(
                    "INSERT INTO packets(packet_id,packet_version,job_id,stage,source_range_json,source_tokens,"
                    "primary_units,overlap_units,prompt_tokens_estimate,output_tokens_predicted,payload_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        packet["packet_id"], packet["packet_version"], job_id, packet["stage"],
                        json.dumps(packet["source_range"], sort_keys=True, separators=(",", ":")),
                        int(packet.get("source_tokens", 0)), int(packet.get("primary_units", 0)),
                        int(packet.get("overlap_units", 0)), int(packet.get("prompt_tokens_estimate", 0)),
                        int(packet.get("output_tokens_predicted", 0)), packet["payload_sha256"], created_at,
                    ),
                )
            revision = job["revision"] + 1
            db.execute("UPDATE jobs SET revision=?,updated_at=? WHERE job_id=?", (revision, created_at, job_id))
            self._journal(
                db, kind="decision", job_id=job_id, revision=revision,
                stage_from=job["stage"], stage_to=job["stage"], operation="packet_plan_published",
                payload={"packet_count": len(missing), "packet_ids": [packet["packet_id"] for packet in missing]},
                now=now,
            )
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM packets WHERE job_id=? ORDER BY packet_id,packet_version", (job_id,)
        )]

    def start_attempt(self, job_id: str, *, stage: str, operation: str, operation_class: str,
                      payload_sha256: str, packet_id: str = "", packet_version: int = 0,
                      deterministic: bool = True, generation_started: bool = True,
                      model: str | None = None, prompt_budget: int | None = None,
                      completion_budget: int | None = None, expected_revision: int,
                      now: datetime | None = None) -> dict[str, Any]:
        if operation_class not in self.policy["operation_attempt_ceilings"]:
            raise ValueError(f"Unknown operation class: {operation_class}")
        if len(payload_sha256) != 64:
            raise ValueError("payload_sha256 must be a 64-character hex digest")
        with self.transaction() as db:
            job = self._job(db, job_id)
            self._check_revision(job, expected_revision)
            if job["stage"] != stage or stage in TERMINAL_STAGES:
                raise InvalidTransition(f"Attempt stage {stage} does not match active job stage {job['stage']}")
            if deterministic and generation_started:
                duplicate = db.execute(
                    "SELECT attempt_id FROM attempts WHERE kind='start' AND job_id=? AND stage_to=? AND packet_id=? "
                    "AND packet_version=? AND operation=? AND payload_sha256=? AND deterministic=1 AND generation_started=1",
                    (job_id, stage, packet_id, packet_version, operation, payload_sha256),
                ).fetchone()
                if duplicate:
                    raise DuplicateDeterministicAttempt(
                        f"Identical deterministic generation already journalled: {duplicate['attempt_id']}"
                    )
            count = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE kind='start' AND job_id=? AND stage_to=? AND packet_id=? "
                "AND packet_version=? AND operation_class=?",
                (job_id, stage, packet_id, packet_version, operation_class),
            ).fetchone()[0]
            ceiling = self.policy["operation_attempt_ceilings"][operation_class]
            if count >= ceiling:
                raise CircuitBreakerOpen(f"Attempt ceiling reached for {operation_class}: {count}/{ceiling}")
            attempt_id = str(uuid.uuid4())
            revision = job["revision"] + 1
            db.execute("UPDATE jobs SET revision=?,updated_at=? WHERE job_id=?", (revision, iso(now), job_id))
            return self._journal(
                db, kind="start", attempt_id=attempt_id, job_id=job_id, revision=revision,
                stage_from=stage, stage_to=stage, packet_id=packet_id, packet_version=packet_version,
                operation=operation, operation_class=operation_class, request_sha256=payload_sha256,
                attempt_number=count + 1, deterministic=deterministic, generation_started=generation_started,
                model=model, prompt_budget=prompt_budget, completion_budget=completion_budget, now=now,
            )

    def _start_row(self, db: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM attempts WHERE attempt_id=? AND kind='start'", (attempt_id,)).fetchone()
        if row is None:
            raise NotFound(f"Unknown attempt: {attempt_id}")
        outcome = db.execute(
            "SELECT kind FROM attempts WHERE attempt_id=? AND kind IN ('success','failure')", (attempt_id,)
        ).fetchone()
        if outcome is not None:
            raise StateKernelError(f"Attempt is already terminal: {outcome['kind']}")
        return row

    def succeed_attempt(self, attempt_id: str, *, expected_revision: int,
                        prompt_tokens: int | None = None, completion_tokens: int | None = None,
                        elapsed_ms: int | None = None, finish_reason: str | None = None,
                        artifacts: list[dict[str, Any]] | None = None,
                        now: datetime | None = None) -> dict[str, Any]:
        verified_artifacts = self._verify_artifacts(artifacts)
        with self.transaction() as db:
            start = self._start_row(db, attempt_id)
            job = self._job(db, start["job_id"])
            self._check_revision(job, expected_revision)
            revision = job["revision"] + 1
            db.execute(
                "UPDATE jobs SET revision=?,updated_at=?,consecutive_failure_class=NULL,consecutive_failure_count=0 WHERE job_id=?",
                (revision, iso(now), job["job_id"]),
            )
            self._insert_artifacts(db, job["job_id"], attempt_id, verified_artifacts, now)
            return self._journal(
                db, kind="success", attempt_id=attempt_id, job_id=job["job_id"], revision=revision,
                stage_from=job["stage"], stage_to=job["stage"], packet_id=start["packet_id"],
                packet_version=start["packet_version"], operation=start["operation"],
                operation_class=start["operation_class"], request_sha256=start["payload_sha256"],
                attempt_number=start["attempt_number"], model=start["model"], prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, elapsed_ms=elapsed_ms, finish_reason=finish_reason,
                payload={"artifact_ids": [artifact["artifact_id"] for artifact in verified_artifacts]}, now=now,
            )

    def fail_attempt(self, attempt_id: str, *, failure_class: str, failure_code: str,
                     diagnostic: str, retryable: bool, expected_revision: int,
                     elapsed_ms: int | None = None, finish_reason: str | None = None,
                     artifacts: list[dict[str, Any]] | None = None,
                     fallback_available: bool = False,
                     now: datetime | None = None) -> dict[str, Any]:
        if failure_class not in self.policy["failure_classes"]:
            raise ValueError(f"Unknown failure class: {failure_class}")
        bounded = diagnostic[: self.policy["diagnostic_max_chars"]]
        verified_artifacts = self._verify_artifacts(artifacts)
        with self.transaction() as db:
            start = self._start_row(db, attempt_id)
            job = self._job(db, start["job_id"])
            self._check_revision(job, expected_revision)
            streak = job["consecutive_failure_count"] + 1 if job["consecutive_failure_class"] == failure_class else 1
            operation_count = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE kind='start' AND job_id=? AND stage_to=? AND packet_id=? "
                "AND packet_version=? AND operation_class=?",
                (job["job_id"], start["stage_to"], start["packet_id"], start["packet_version"], start["operation_class"]),
            ).fetchone()[0]
            ceiling = self.policy["operation_attempt_ceilings"][start["operation_class"]]
            # Cluster/plan and synthesis actions have an accepted deterministic
            # fallback after their sole model generation. A terminal model
            # outcome must still be journalled, but the job remains runnable
            # long enough to commit that fallback. Extraction has no fallback.
            should_block = (
                not retryable
                or (
                    not fallback_available
                    and (
                        operation_count >= ceiling
                        or streak >= self.policy["consecutive_failure_ceiling"]
                    )
                )
            )
            next_stage = "blocked_needs_human" if should_block else job["stage"]
            resume_stage = job["stage"] if should_block else job["resume_stage"]
            revision = job["revision"] + 1
            db.execute(
                "UPDATE jobs SET stage=?,resume_stage=?,revision=?,updated_at=?,consecutive_failure_class=?,"
                "consecutive_failure_count=? WHERE job_id=?",
                (next_stage, resume_stage, revision, iso(now), failure_class, streak, job["job_id"]),
            )
            self._insert_artifacts(db, job["job_id"], attempt_id, verified_artifacts, now)
            result = self._journal(
                db, kind="failure", attempt_id=attempt_id, job_id=job["job_id"], revision=revision,
                stage_from=job["stage"], stage_to=next_stage, packet_id=start["packet_id"],
                packet_version=start["packet_version"], operation=start["operation"],
                operation_class=start["operation_class"], request_sha256=start["payload_sha256"],
                attempt_number=start["attempt_number"], model=start["model"], elapsed_ms=elapsed_ms,
                finish_reason=finish_reason, failure_class=failure_class, failure_code=failure_code,
                diagnostic=bounded, retryable=retryable,
                payload={"circuit_open": should_block, "fallback_available": fallback_available,
                         "operation_attempts": operation_count,
                         "operation_ceiling": ceiling, "consecutive_failures": streak,
                         "artifact_ids": [artifact["artifact_id"] for artifact in verified_artifacts]}, now=now,
            )
        result["job_stage"] = self.get_job(result["job_id"])["stage"]
        return result

    def notification_context(self, job_id: str) -> dict[str, Any]:
        """Fresh state read used immediately before inline formatting/sending."""
        job = self.get_job(job_id)
        job["deliveries"] = [dict(row) for row in self.connection.execute(
            "SELECT * FROM deliveries WHERE job_id=? ORDER BY delivery_id", (job_id,)
        )]
        return job

    def has_delivery(self, job_id: str, message_kind: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM deliveries WHERE job_id=? AND message_kind=? LIMIT 1", (job_id, message_kind)
        ).fetchone() is not None

    def record_delivery(self, job_id: str, *, message_kind: str, payload_sha256: str,
                        platform: str, platform_id: str, observed_revision: int,
                        observed_stage: str, note_sha256: str | None = None,
                        attempt_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        if len(payload_sha256) != 64:
            raise ValueError("payload_sha256 must be a 64-character hex digest")
        with self.transaction() as db:
            job = self._job(db, job_id)
            self._check_revision(job, observed_revision)
            if job["stage"] != observed_stage:
                raise RevisionConflict(
                    f"Delivery stage changed: observed {observed_stage}, current {job['stage']}"
                )
            cursor = db.execute(
                "INSERT INTO deliveries(job_id,attempt_id,message_kind,observed_revision,observed_stage,payload_sha256,"
                "note_sha256,platform,platform_id,delivered_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, attempt_id, message_kind, observed_revision, observed_stage, payload_sha256,
                 note_sha256, platform, platform_id, iso(now)),
            )
            return dict(db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (cursor.lastrowid,)).fetchone())
