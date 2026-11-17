"""Four authored offline scenarios; runner mocked, no OS acceptance claim."""
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from src.sandbox import execution_recovery as recovery, seatbelt_stage
from src.sandbox.seatbelt_runner import Result


class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name))
        self.parent = self.root / "stages"
        self.parent.mkdir(mode=0o700)
        self.path = str(self.root / "attempts.sqlite")
        self.journal = recovery.SQLiteAttemptJournal(self.path)
        self.candidate, self.driver = b"value = 1\n", b"pass\n"

    def run_attempt(self, journal=None):
        return recovery.journaled_stage_and_run(
            self.journal if journal is None else journal, "attempt-1", str(self.parent),
            self.candidate, self.driver, "/usr/bin/python3", (), timeout=1.5,
        )

    def test_real_journal_and_staging_commit_success_before_return(self):
        def runner(*args, **kwargs):
            observer = recovery.SQLiteAttemptJournal(self.path)
            self.assertEqual(observer.lookup("attempt-1").state, "UNKNOWN")
            with sqlite3.connect(self.path) as connection:
                self.assertEqual(connection.execute("SELECT state FROM attempts").fetchone(), ("STARTED",))
            return Result(0, False, b"synthetic stdout", b"")

        with mock.patch.object(seatbelt_stage, "run_python", side_effect=runner) as run:
            staged = self.run_attempt()
            with self.assertRaises(recovery.AttemptConsumedError):
                self.run_attempt()
        entry = recovery.SQLiteAttemptJournal(self.path).lookup("attempt-1")
        self.assertIs(type(staged), seatbelt_stage.StagedRun)
        self.assertEqual((entry.state, entry.returncode, entry.timed_out), ("SUCCEEDED", 0, 0))
        self.assertEqual((entry.candidate_sha256, entry.driver_sha256),
                         (sha256(self.candidate).hexdigest(), sha256(self.driver).hexdigest()))
        self.assertEqual(entry.stage_path, staged.stage_path)
        self.assertEqual((Path(staged.stage_path) / "work" / "candidate.py").read_bytes(), self.candidate)
        run.assert_called_once()

    def test_interruption_is_unknown_and_reopen_cannot_execute_again(self):
        with mock.patch.object(seatbelt_stage, "run_python", side_effect=KeyboardInterrupt) as run:
            with self.assertRaises(seatbelt_stage.StageError) as interrupted:
                self.run_attempt()
            reopened = recovery.SQLiteAttemptJournal(self.path)
            with self.assertRaises(recovery.AttemptConsumedError):
                self.run_attempt(reopened)
        entry = reopened.lookup("attempt-1")
        self.assertEqual(entry.state, "UNKNOWN")
        self.assertEqual(entry.stage_path, interrupted.exception.stage_path)
        self.assertIsInstance(interrupted.exception.__cause__, KeyboardInterrupt)
        self.assertIsNone(entry.returncode)
        run.assert_called_once()

    def test_competing_handle_cannot_stage_while_first_attempt_runs(self):
        competitor = recovery.SQLiteAttemptJournal(self.path)

        def runner(*args, **kwargs):
            with self.assertRaises(recovery.AttemptConsumedError):
                self.run_attempt(competitor)
            return Result(7, False, b"", b"synthetic failure")

        with mock.patch.object(seatbelt_stage, "run_python", side_effect=runner) as run:
            staged = self.run_attempt()
        self.assertEqual(staged.result.returncode, 7)
        self.assertEqual(competitor.lookup("attempt-1").state, "FAILED")
        self.assertEqual(len(list(self.parent.iterdir())), 1)
        run.assert_called_once()

    def test_malformed_existing_record_blocks_wrapper_before_staging(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)", (
                "attempt-1", 1, "x" * 65, "0" * 64, "STARTED", None, None, None,
            ))
        with mock.patch.object(seatbelt_stage, "run_python") as run:
            with self.assertRaises(recovery.JournalError):
                self.run_attempt()
        self.assertEqual(list(self.parent.iterdir()), [])
        run.assert_not_called()
