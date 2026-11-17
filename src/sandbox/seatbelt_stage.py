import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass

from .seatbelt_runner import Result, run_python


@dataclass(frozen=True)
class StagedRun:
    stage_path: str
    candidate_sha256: str
    driver_sha256: str
    result: Result


class StageError(RuntimeError):
    def __init__(self, stage_path):
        super().__init__("staging or execution failed")
        self.stage_path = stage_path


def _write_file(path, data, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, mode), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def stage_and_run(parent, candidate: bytes, driver: bytes, executable,
                  runtime_roots, timeout=3.0):
    stage_path = None
    try:
        if type(candidate) is not bytes or not 0 < len(candidate) <= 65536:
            raise ValueError("candidate must be exact bytes of length 1..65536")
        if type(driver) is not bytes or not 0 < len(driver) <= 65536:
            raise ValueError("driver must be exact bytes of length 1..65536")
        parent = os.fspath(parent)
        if not isinstance(parent, str) or parent != os.path.realpath(parent):
            raise ValueError("parent must be a canonical absolute directory path")
        info = os.lstat(parent)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise ValueError("parent must be caller-owned and not group/other writable")
        stage_path = tempfile.mkdtemp(prefix="seatbelt-", dir=parent)
        os.chmod(stage_path, 0o700)
        work = os.path.join(stage_path, "work")
        trusted = os.path.join(stage_path, "trusted")
        os.mkdir(work, 0o700)
        os.chmod(work, 0o700)
        os.mkdir(trusted, 0o700)
        os.chmod(trusted, 0o700)
        _write_file(os.path.join(work, "candidate.py"), candidate, 0o600)
        driver_path = os.path.join(trusted, "driver.py")
        _write_file(driver_path, driver, 0o400)
        result = run_python(
            work, "candidate.py", executable, runtime_roots, timeout,
            trusted_script=driver_path,
        )
        return StagedRun(stage_path, hashlib.sha256(candidate).hexdigest(),
                         hashlib.sha256(driver).hexdigest(), result)
    except BaseException as exc:
        raise StageError(stage_path) from exc
