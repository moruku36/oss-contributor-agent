"""Offline policy-text checks; these never launch a runtime or sandbox."""
import os
import tempfile
import unittest
from pathlib import Path

from src.sandbox.seatbelt_policy import build_policy


class SeatbeltRootOpenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.workspace = self.base / "workspace"
        self.runtime = self.base / "runtime"
        self.executable = self.base / "python"
        self.workspace.mkdir()
        self.runtime.mkdir()
        self.executable.touch()

    def _build(self, workspace=None, executable=None, roots=None):
        return build_policy(
            str(self.workspace if workspace is None else workspace),
            str(self.executable if executable is None else executable),
            (str(self.runtime),) if roots is None else roots,
        )

    def test_exact_root_read_without_recursive_root_grant(self):
        policy = self._build()
        self.assertEqual(
            policy.splitlines().count('(allow file-read-data (literal "/"))'), 1,
        )
        self.assertEqual(policy.count('(literal "/")'), 1)
        self.assertNotIn('(subpath "/")', policy)
        self.assertNotIn('(allow file-read*)', policy)
        self.assertNotIn('(allow file-read-data)', policy)
        self.assertNotIn('(allow file-read-metadata', policy)
        self.assertNotIn('sysctl', policy)

    def test_process_network_and_write_deny_invariants(self):
        trusted = self.base / "trusted"
        trusted.mkdir()
        driver = trusted / "driver.py"
        driver.touch()
        external = self.base / "external.txt"
        external.write_text("outside workspace\n", encoding="utf-8")
        policy = self._build(roots=(str(self.runtime), str(trusted)))
        self.assertIn('(deny default)\n', policy)
        self.assertEqual(policy.count('(allow process'), 1)
        self.assertIn(f'(allow process-exec (literal "{self.executable}"))\n', policy)
        self.assertNotIn('process-fork', policy)
        self.assertNotIn('network', policy)
        self.assertEqual(policy.count('(allow file-write'), 1)
        self.assertIn(f'(allow file-write* (subpath "{self.workspace}"))\n', policy)
        self.assertIn(f'(subpath "{trusted}")', policy)
        self.assertNotIn(str(driver), policy)
        self.assertNotIn(str(external), policy)

    def test_canonical_and_narrow_caller_path_validation(self):
        self.assertIn(f'(subpath "{self.runtime}")', self._build())
        with self.assertRaises(ValueError):
            self._build(workspace="/")
        with self.assertRaises(ValueError):
            self._build(roots=("/",))
        with self.assertRaises(ValueError):
            self._build(workspace="workspace")
        with self.assertRaises(ValueError):
            self._build(executable=str(self.base) + "/./python")
        with self.assertRaises(ValueError):
            self._build(roots=(str(self.runtime) + "/",))
        with self.assertRaises(ValueError):
            self._build(roots=[str(self.runtime)])
        with self.assertRaises(ValueError):
            self._build(roots=(str(self.executable),))
        with self.assertRaises(ValueError):
            self._build(workspace=str(self.workspace) + '"')
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            self._build(workspace=alias / "workspace")
        with self.assertRaises(ValueError):
            self._build(executable=alias / "python")
        with self.assertRaises(ValueError):
            self._build(roots=(str(alias / "runtime"),))
        with self.assertRaises(ValueError):
            self._build(roots=(str(self.workspace),))
        binary = self.workspace / "python"
        binary.touch()
        with self.assertRaises(ValueError):
            self._build(executable=binary)
