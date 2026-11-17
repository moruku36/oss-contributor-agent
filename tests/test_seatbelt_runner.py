import signal
import stat
import subprocess
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from src.sandbox import seatbelt_runner as runner


class SeatbeltRunnerTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.args = ("/work/project", "main.py", "/runtime/python3", ("/runtime",))
        self.system = stack.enter_context(
            patch.object(runner.platform, "system", return_value="Darwin")
        )
        self.lstat = stack.enter_context(
            patch.object(runner.os, "lstat", return_value=SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600, st_nlink=1,
            ))
        )
        self.policy = stack.enter_context(
            patch.object(runner, "build_policy", return_value="mock-profile")
        )
        self.stdout = MagicMock(name="stdout")
        self.stdout.__enter__.return_value = self.stdout
        self.stdout.read.return_value = b"out\n"
        self.stderr = MagicMock(name="stderr")
        self.stderr.__enter__.return_value = self.stderr
        self.stderr.read.return_value = b"err\n"
        self.tempfiles = stack.enter_context(
            patch.object(runner.tempfile, "TemporaryFile", side_effect=[self.stdout, self.stderr])
        )
        self.proc = MagicMock(pid=4321, returncode=0)
        self.proc.wait.return_value = 0
        self.launch = stack.enter_context(
            patch.object(runner.subprocess, "Popen", return_value=self.proc)
        )
        self.killpg = stack.enter_context(patch.object(runner.os, "killpg"))
        self.lifecycle = MagicMock()
        self.lifecycle.attach_mock(self.proc.wait, "wait")
        self.lifecycle.attach_mock(self.killpg, "killpg")

    def test_non_darwin_fails_closed(self):
        self.system.return_value = "Linux"
        with self.assertRaises(RuntimeError):
            runner.run_python(*self.args)
        self.lstat.assert_not_called()
        self.policy.assert_not_called()
        self.tempfiles.assert_not_called()
        self.launch.assert_not_called()
        self.killpg.assert_not_called()

    def test_successful_launch(self):
        result = runner.run_python(*self.args)
        self.assertEqual(result, runner.Result(0, False, b"out\n", b"err\n"))
        self.assertTrue(result.success)
        self.policy.assert_called_once_with("/work/project", "/runtime/python3", ("/runtime",))
        self.assertEqual(self.lstat.call_args_list, [
            call("/usr/bin/sandbox-exec"), call("/work/project/main.py"),
        ])
        self.assertEqual(self.tempfiles.call_args_list, [call(dir="/work"), call(dir="/work")])
        self.launch.assert_called_once_with(
            ["/usr/bin/sandbox-exec", "-p", "mock-profile", "/runtime/python3",
             "-I", "-S", "-B", "/work/project/main.py"],
            cwd="/work/project",
            env={"HOME": "/work/project", "TMPDIR": "/work/project", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL, stdout=self.stdout, stderr=self.stderr,
            start_new_session=True, preexec_fn=runner._limits,
        )
        self.proc.wait.assert_called_once_with(timeout=3.0)
        self.stdout.seek.assert_called_once_with(0)
        self.stderr.seek.assert_called_once_with(0)
        self.stdout.read.assert_called_once_with(32768)
        self.stderr.read.assert_called_once_with(32768)
        self.killpg.assert_not_called()

    def test_timeout_kills_and_reaps(self):
        self.proc.wait.side_effect = [
            subprocess.TimeoutExpired("sandbox-exec", 0.25), -signal.SIGKILL,
        ]
        self.proc.returncode = -signal.SIGKILL
        result = runner.run_python(*self.args, timeout=0.25)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.success)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual(self.lifecycle.mock_calls, [
            call.wait(timeout=0.25), call.killpg(4321, signal.SIGKILL), call.wait(),
        ])
        self.launch.assert_called_once()

    def test_wait_exception_kills_and_reaps(self):
        problem = KeyboardInterrupt("interrupted")
        self.proc.wait.side_effect = [problem, 0]
        with self.assertRaises(KeyboardInterrupt) as caught:
            runner.run_python(*self.args)
        self.assertIs(caught.exception, problem)
        self.assertEqual(self.lifecycle.mock_calls, [
            call.wait(timeout=3.0), call.killpg(4321, signal.SIGKILL), call.wait(),
        ])
        self.stdout.read.assert_not_called()
        self.stderr.read.assert_not_called()
        self.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
