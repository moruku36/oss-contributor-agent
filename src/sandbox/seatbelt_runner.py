import os
import platform
import resource
import signal
import stat
import subprocess
import tempfile
from dataclasses import dataclass

from .seatbelt_policy import build_policy


@dataclass(frozen=True)
class Result:
    returncode: int
    timed_out: bool
    stdout: bytes
    stderr: bytes

    @property
    def success(self):
        return self.returncode == 0 and not self.timed_out


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _kill_and_reap(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        proc.wait()


def run_python(workspace, script_name, executable, runtime_roots, timeout=3.0):
    if platform.system() != "Darwin" or not stat.S_ISREG(os.lstat("/usr/bin/sandbox-exec").st_mode):
        raise RuntimeError("Darwin and a regular /usr/bin/sandbox-exec are required")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10:
        raise ValueError("timeout must be finite, positive, and at most 10 seconds")
    if (not isinstance(script_name, str) or not script_name.endswith(".py")
            or "/" in script_name or "\\" in script_name):
        raise ValueError("script_name must be a single .py basename")
    workspace, executable = os.fspath(workspace), os.fspath(executable)
    policy = build_policy(workspace, executable, runtime_roots)
    script = os.path.join(workspace, script_name)
    info = os.lstat(script)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("script must be regular, without symlinks or extra hard links")
    tempdir = os.path.dirname(workspace)
    if tempdir == workspace:
        raise ValueError("workspace must have an external parent directory")
    with tempfile.TemporaryFile(dir=tempdir) as stdout, tempfile.TemporaryFile(dir=tempdir) as stderr:
        proc = subprocess.Popen(
            ["/usr/bin/sandbox-exec", "-p", policy, executable, "-I", "-S", "-B", script],
            cwd=workspace, env={"HOME": workspace, "TMPDIR": workspace, "LC_ALL": "C"},
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=True, preexec_fn=_limits,
        )
        try:
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_and_reap(proc)
            stdout.seek(0)
            stderr.seek(0)
            return Result(proc.returncode, timed_out, stdout.read(32768), stderr.read(32768))
        except BaseException:
            _kill_and_reap(proc)
            raise
