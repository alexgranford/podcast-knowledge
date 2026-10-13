#!/usr/bin/env python3
"""Acceptance tests for the podcast state kernel."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from podcast_state import (  # noqa: E402
    DuplicateDeterministicAttempt,
    InvalidTransition,
    PodcastStore,
    RevisionConflict,
    SingleWorkerLock,
    StateKernelError,
    WorkerLockUnavailable,
    file_sha256,
    payload_hash,
    worker_session,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state" / "podcast.sqlite3"
        self.store = PodcastStore(self.db)
        self.store.create_job("podcast-test", "apple:1000000000000", now=T0)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def advance_to(self, target: str) -> dict:
        sequence = {
            "queued": "resolving",
            "resolving": "acquiring_audio",
            "acquiring_audio": "transcribing",
            "transcribing": "validating_transcript",
            "validating_transcript": "extracting",
        }
        while self.store.get_job("podcast-test")["stage"] != target:
            job = self.store.get_job("podcast-test")
            self.store.transition_job(
                "podcast-test", sequence[job["stage"]], expected_revision=job["revision"], now=T0
            )
        return self.store.get_job("podcast-test")




class MigrationAndRecoveryTests(StoreCase):
    def test_migration_is_idempotent_and_versioned(self) -> None:
        self.store.migrate()
        rows = self.store.connection.execute("SELECT version,filename,sha256 FROM schema_migrations").fetchall()
        self.assertEqual(3, len(rows))
        self.assertEqual(
            [(1, "0001_state_kernel.sql"), (2, "0002_job_scoped_packets.sql"),
             (3, "0003_remove_artifact_schema_version.sql")],
            [tuple(row[:2]) for row in rows],
        )
        self.assertTrue(all(len(row["sha256"]) == 64 for row in rows))







class TransitionAndLockTests(StoreCase):


    def test_terminal_state_is_irreversible(self) -> None:
        job = self.store.transition_job("podcast-test", "cancelled", expected_revision=0, now=T0)
        with self.assertRaises(InvalidTransition):
            self.store.transition_job("podcast-test", "queued", expected_revision=job["revision"], now=T0)

    def test_single_worker_lock_excludes_second_tick(self) -> None:
        lock_path = Path(self.temp.name) / "worker.lock"
        first = SingleWorkerLock(lock_path).acquire()
        try:
            with self.assertRaises(WorkerLockUnavailable):
                SingleWorkerLock(lock_path).acquire()
        finally:
            first.release()
        with SingleWorkerLock(lock_path):
            self.assertTrue(lock_path.is_file())









if __name__ == "__main__":
    unittest.main(verbosity=2)
