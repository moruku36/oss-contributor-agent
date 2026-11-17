"""Host-only attempt consumption; history never authorizes execution or delivery."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import os
import re
import sqlite3
import stat

from .seatbelt_runner import Result
from .seatbelt_stage import StageError, StagedRun, stage_and_run


class JournalError(RuntimeError):
    """Storage or history is uncertain; preserve the journal and stop."""


class AttemptConsumedError(JournalError):
    """This explicit attempt identity already exists, regardless of outcome."""


def _attempt_id(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
        raise JournalError("attempt_id must be an explicit stable 1..128 character ASCII identifier")
    return value


def _path(value):
    if (type(value) is not str or not 0 < len(value) <= 4096 or "\x00" in value
            or len(value.encode("utf-8")) > 4096 or not os.path.isabs(value)
            or value != os.path.realpath(value)):
        raise JournalError("path must be canonical, absolute and at most 4096 UTF-8 bytes")
    return value


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    version: int
    candidate_sha256: str
    driver_sha256: str
    state: str
    stage_path: str | None
    returncode: int | None
    timed_out: int | None


_APPLICATION_ID = 1163022914
_SCHEMA = """CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY NOT NULL, version INTEGER NOT NULL,
    candidate_sha256 TEXT NOT NULL, driver_sha256 TEXT NOT NULL,
    state TEXT NOT NULL, stage_path TEXT, returncode INTEGER, timed_out INTEGER
)"""


def _record(row):
    entry = AttemptRecord(*row)
    _attempt_id(entry.attempt_id)
    if (type(entry.version) is not int or entry.version != 1
            or type(entry.candidate_sha256) is not str or type(entry.driver_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", entry.candidate_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", entry.driver_sha256) is None
            or entry.state not in ("STARTED", "UNKNOWN", "SUCCEEDED", "FAILED")):
        raise JournalError("malformed attempt record")
    if entry.stage_path is not None:
        _path(entry.stage_path)
    if entry.state in ("STARTED", "UNKNOWN"):
        if (entry.returncode is not None or entry.timed_out is not None
                or (entry.state == "STARTED" and entry.stage_path is not None)):
            raise JournalError("inconsistent incomplete attempt")
    elif (entry.stage_path is None or type(entry.returncode) is not int
            or not -(2**31) <= entry.returncode < 2**31
            or type(entry.timed_out) is not int or entry.timed_out not in (0, 1)
            or (entry.state == "SUCCEEDED") != (entry.returncode == 0 and entry.timed_out == 0)):
        raise JournalError("inconsistent terminal attempt")
    return entry


class SQLiteAttemptJournal:
    """Private local file in a stable trusted host directory; no retry API."""

    def __init__(self, path):
        self.path, self._broken = _path(path), False
        directory = os.path.dirname(path)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise JournalError("journal directory must be caller-owned and private")
        created = False
        try:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                created = True
            self._identity = self._file_identity()
            with self._transaction(initialize=created):
                pass
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            self._broken = True
            raise

    def _file_identity(self):
        info = os.lstat(self.path)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise JournalError("journal must be a private caller-owned regular file with one link")
        return info.st_dev, info.st_ino

    @contextmanager
    def _transaction(self, *, write=True, initialize=False):
        connection = None
        try:
            if self._broken or self._file_identity() != self._identity:
                raise JournalError("journal unavailable or replaced")
            connection = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
            if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise JournalError("DELETE journal mode required")
            connection.execute("PRAGMA synchronous=FULL")
            if connection.execute("PRAGMA synchronous").fetchone() != (2,):
                raise JournalError("FULL synchronization required")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if initialize:
                connection.execute(_SCHEMA)
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute("PRAGMA user_version=1")
            if (connection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,)
                    or connection.execute("PRAGMA user_version").fetchone() != (1,)
                    or connection.execute("SELECT type,name,sql FROM sqlite_master "
                                          "WHERE name NOT GLOB 'sqlite_*'").fetchall()
                    != [("table", "attempts", _SCHEMA)]):
                raise JournalError("unsupported journal schema; preserve without migration")
            yield connection
            if self._file_identity() != self._identity:
                raise JournalError("journal replaced during transaction")
            connection.commit()
        except BaseException as exc:
            if not isinstance(exc, AttemptConsumedError):
                self._broken = True
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    self._broken = True
                    raise

    def lookup(self, attempt_id):
        """Detached metadata only; STARTED is projected as UNKNOWN, without writes."""
        identity = _attempt_id(attempt_id)
        with self._transaction(write=False) as connection:
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (identity,)).fetchone()
            entry = None if row is None else _record(row)
            return replace(entry, state="UNKNOWN") if entry and entry.state == "STARTED" else entry

    def _claim(self, identity, candidate_hash, driver_hash):
        row = (identity, 1, candidate_hash, driver_hash, "STARTED", None, None, None)
        _record(row)
        with self._transaction() as connection:
            old = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (identity,)).fetchone()
            if old is not None:
                _record(old)
                raise AttemptConsumedError("attempt_id already consumed; execution is forbidden")
            connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)", row)

    def _finish(self, identity, candidate_hash, driver_hash, state, path, code=None, timed_out=None):
        row = (identity, 1, candidate_hash, driver_hash, state, path, code, timed_out)
        _record(row)
        with self._transaction() as connection:
            old = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (identity,)).fetchone()
            if old is None or _record(old) != AttemptRecord(*row[:4], "STARTED", None, None, None):
                raise JournalError("missing, stale or misbound attempt")
            if state == "STARTED":
                raise JournalError("terminal transition required")
            connection.execute("UPDATE attempts SET state=?,stage_path=?,returncode=?,timed_out=? "
                               "WHERE attempt_id=?", (state, path, code, timed_out, identity))


def journaled_stage_and_run(journal, attempt_id, parent, candidate, driver, executable,
                            runtime_roots, timeout=3.0):
    """Consume once before real staging; return only after durable terminal storage."""
    if type(journal) is not SQLiteAttemptJournal:
        raise JournalError("exact SQLiteAttemptJournal required")
    identity, parent = _attempt_id(attempt_id), _path(os.fspath(parent))
    if os.path.commonpath((parent, journal.path)) == parent:
        raise JournalError("host journal must be outside the staging parent")
    if type(candidate) is not bytes or not 0 < len(candidate) <= 65536:
        raise JournalError("candidate must be exact bytes of length 1..65536")
    if type(driver) is not bytes or not 0 < len(driver) <= 65536:
        raise JournalError("driver must be exact bytes of length 1..65536")
    hashes = sha256(candidate).hexdigest(), sha256(driver).hexdigest()
    journal._claim(identity, *hashes)
    try:
        staged = stage_and_run(parent, candidate, driver, executable, runtime_roots, timeout)
    except StageError as exc:
        try:
            path = None if exc.stage_path is None else _path(exc.stage_path)
        except (JournalError, ValueError, OSError):
            path = None
        journal._finish(identity, *hashes, "UNKNOWN", path)
        raise
    if (type(staged) is not StagedRun or type(staged.result) is not Result
            or (staged.candidate_sha256, staged.driver_sha256) != hashes
            or type(staged.result.timed_out) is not bool
            or type(staged.result.stdout) is not bytes or type(staged.result.stderr) is not bytes):
        raise JournalError("malformed or misbound staged result; attempt remains consumed")
    result = staged.result
    journal._finish(identity, *hashes, "SUCCEEDED" if result.success else "FAILED",
                    staged.stage_path, result.returncode, int(result.timed_out))
    return staged
