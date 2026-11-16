"""Trusted, single-host SQLite consumption journal; never approval authority.

Only DraftSubmission drives transitions. No replay, expiry or recovery API.
See docs/draft-submission.md for filesystem and crash assumptions.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
import re
import sqlite3
import stat
from threading import Lock

from src.review.draft_submission import SubmissionOutcome, SubmissionStatus


class JournalError(RuntimeError):
    """Storage is unavailable/ambiguous. Do not enter the next boundary."""


class JournalReplayError(ValueError):
    """Identity or exact request was already consumed, regardless of outcome."""


def _operation_id(value: object) -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("explicit stable operation_id (1..128 ASCII characters) required")
    return value


@dataclass(frozen=True, slots=True)
class JournalEntry:
    operation_id: str
    snapshot_json: str
    stage: str
    outcome: SubmissionOutcome


_APPLICATION_ID = 1146310730
_SCHEMA = """CREATE TABLE submissions (
    operation_id TEXT PRIMARY KEY NOT NULL,
    snapshot_json TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL CHECK(stage IN ('authority_intent','transport_intent','finished')),
    status TEXT NOT NULL CHECK(status IN ('unknown','submitted','rejected')),
    dispatched INTEGER NOT NULL CHECK(dispatched IN (0,1)),
    reason TEXT NOT NULL,
    pull_request_number INTEGER
)"""


class SQLiteSubmissionJournal:
    """Explicit absolute local filename in a trusted private directory.

    Creates a new 0600 file; existing files must have this exact schema. Opens
    fresh connections, pins the file identity, and poisons this object after
    any storage error/cancellation. Never cache absence or terminal outcomes.
    """

    def __init__(self, path: str) -> None:
        if type(path) is not str or not os.path.isabs(path) or "\x00" in path:
            raise ValueError("absolute local SQLite filename required; no URI or memory database")
        self._path = path
        self._lock = Lock()
        self._broken = False
        created = False
        try:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                created = True
            self._identity = self._file_identity()
            with self._transaction(initialize=created):
                pass
            if created:
                # Persist the directory entry too. Unsupported storage fails closed.
                fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException as exc:
            self._broken = True
            if isinstance(exc, Exception):
                raise JournalError("journal initialization failed; preserve the file") from exc
            raise

    def _file_identity(self) -> tuple[int, int]:
        info = os.lstat(self._path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise JournalError("journal must be a regular non-symlink file with one link")
        return info.st_dev, info.st_ino

    def _commit(self, connection: sqlite3.Connection) -> None:
        connection.commit()

    @contextmanager
    def _transaction(self, *, initialize: bool = False):
        with self._lock:
            if self._broken:
                raise JournalError("journal object is unusable after a storage error")
            connection = None
            try:
                if self._file_identity() != self._identity:
                    raise JournalError("journal file replaced")
                connection = sqlite3.connect(self._path, timeout=1.0, isolation_level=None, uri=False)
                if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                    raise JournalError("only DELETE rollback journal mode is supported")
                connection.execute("PRAGMA synchronous=EXTRA")
                if connection.execute("PRAGMA synchronous").fetchone() != (3,):
                    raise JournalError("SQLite EXTRA synchronization required")
                connection.execute("BEGIN IMMEDIATE")
                if initialize:
                    connection.execute(_SCHEMA)
                    connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    connection.execute("PRAGMA user_version=1")
                schema = connection.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
                ).fetchall()
                if (connection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,)
                        or connection.execute("PRAGMA user_version").fetchone() != (1,)
                        or schema != [("table", "submissions", _SCHEMA)]):
                    raise JournalError("unsupported journal schema; no migration is attempted")
                yield connection
                if self._file_identity() != self._identity:
                    raise JournalError("journal file replaced during transaction")
                self._commit(connection)
            except JournalReplayError:
                raise
            except BaseException as exc:
                self._broken = True
                if isinstance(exc, Exception):
                    raise JournalError("journal transaction failed or commit is ambiguous") from exc
                raise
            finally:
                if connection is not None:
                    # Closing rolls back any uncommitted work. Never retry a commit.
                    try:
                        connection.close()
                    except BaseException as exc:
                        self._broken = True
                        if isinstance(exc, Exception):
                            raise JournalError("journal close failed; preserve ambiguous state") from exc
                        raise

    @staticmethod
    def _entry(row: tuple) -> JournalEntry:
        identity, snapshot, digest, stage, status, dispatched, reason, number = row
        _operation_id(identity)
        if (type(snapshot) is not str or type(digest) is not str
                or sha256(snapshot.encode("utf-8")).hexdigest() != digest
                or stage not in ("authority_intent", "transport_intent", "finished")
                or type(dispatched) is not int or dispatched not in (0, 1)
                or type(reason) is not str):
            raise JournalError("corrupt journal entry")
        outcome = SubmissionOutcome(SubmissionStatus(status), digest, bool(dispatched), reason, number)
        if ((stage != "finished" and (outcome.status is not SubmissionStatus.UNKNOWN or number is not None))
                or (stage == "authority_intent" and dispatched != 0)
                or (stage == "transport_intent" and dispatched != 1)
                or (outcome.status is SubmissionStatus.SUBMITTED
                    and (dispatched != 1 or type(number) is not int or not 1 <= number <= 2**63 - 1))
                or (outcome.status is not SubmissionStatus.SUBMITTED and number is not None)):
            raise JournalError("inconsistent journal outcome")
        return JournalEntry(identity, snapshot, stage, outcome)

    def lookup(self, operation_id: str) -> JournalEntry | None:
        """Read a detached record; absence is not permission to dispatch."""
        identity = _operation_id(operation_id)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM submissions WHERE operation_id=?", (identity,)).fetchone()
            return None if row is None else self._entry(row)

    def _claim(self, identity: str, snapshot: str, outcome: SubmissionOutcome) -> None:
        with self._transaction() as connection:
            # Both constraints are checked under the write lock, across processes.
            rows = connection.execute(
                "SELECT * FROM submissions WHERE operation_id=? OR snapshot_json=? OR request_sha256=?",
                (identity, snapshot, outcome.request_sha256),
            ).fetchall()
            if rows:
                for row in rows:
                    self._entry(row)
                raise JournalReplayError("operation identity or exact request already consumed; no replay")
            connection.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)", (
                identity, snapshot, outcome.request_sha256, "authority_intent",
                outcome.status.value, int(outcome.dispatched), outcome.reason, None,
            ))

    def _advance(self, identity: str, snapshot: str, previous: str,
                 stage: str, outcome: SubmissionOutcome) -> None:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM submissions WHERE operation_id=?", (identity,)).fetchone()
            if row is None:
                raise JournalError("consumed journal record missing")
            entry = self._entry(row)
            if (entry.snapshot_json != snapshot or entry.stage != previous
                    or entry.outcome.request_sha256 != outcome.request_sha256
                    or previous not in ("authority_intent", "transport_intent")
                    or stage not in ("transport_intent", "finished")
                    or (stage == "transport_intent" and previous != "authority_intent")):
                raise JournalError("stale or misbound journal transition")
            updated = connection.execute(
                "UPDATE submissions SET stage=?,status=?,dispatched=?,reason=?,pull_request_number=? "
                "WHERE operation_id=? AND snapshot_json=? AND stage=?",
                (stage, outcome.status.value, int(outcome.dispatched), outcome.reason,
                 outcome.pull_request_number, identity, snapshot, previous),
            )
            if updated.rowcount != 1:
                raise JournalError("journal transition did not consume exactly one record")
