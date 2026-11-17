import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.sandbox.seatbelt_policy import build_policy


class SeatbeltPolicyTests(unittest.TestCase):
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

    def test_valid_policy(self):
        policy = self._build()
        self.assertEqual(policy.splitlines(), [
            "(version 1)",
            "(deny default)",
            f'(allow process-exec (literal "{self.executable}"))',
            f'(allow file-read* (subpath "{self.workspace}") (literal "{self.executable}") (subpath "/System/Library") (subpath "/usr/lib") (subpath "/System/Cryptexes/OS/System/Library/dyld") (subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld") (subpath "{self.runtime}"))',
            '(allow file-read-data (literal "/"))',
            f'(allow file-write* (subpath "{self.workspace}"))',
        ])
        self.assertTrue(policy.endswith("\n"))
        self.assertNotIn("network", policy)
        self.assertNotIn("process-fork", policy)
        self.assertEqual(self._build(roots=(str(self.runtime),) * 2), policy)
        self.assertNotIn(f'(subpath "{self.runtime}")', self._build(roots=()))

    def test_hostile_quoting(self):
        for bad in ('"', "'", "\\", "\n", "\x7f", "\x85"):
            hostile = self.base / ("hostile" + bad)
            hostile.mkdir()
            binary = hostile / "python"
            binary.touch()
            for kwargs in ({"workspace": hostile}, {"executable": binary},
                           {"roots": (str(hostile),)}):
                with self.subTest(character=repr(bad), field=next(iter(kwargs))):
                    with self.assertRaises(ValueError):
                        self._build(**kwargs)

    def test_symlink_ancestor(self):
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        for kwargs in ({"workspace": alias / "workspace"},
                       {"executable": alias / "python"},
                       {"roots": (str(alias / "runtime"),)}):
            with self.subTest(field=next(iter(kwargs))):
                with self.assertRaises(ValueError):
                    self._build(**kwargs)
        leaf = self.base / "python-link"
        leaf.symlink_to(self.executable)
        with self.assertRaises(ValueError):
            self._build(executable=leaf)

    def test_broad_roots(self):
        broad = ("/", "/Users", "/Library", "/usr")
        realpath, realstat = os.path.realpath, os.stat
        standins = {}
        for index, root in enumerate(broad):
            directory = self.base / f"broad-{index}"
            directory.mkdir()
            standins[root] = directory.stat()

        def canonical(value, strict=False):
            return value if value in broad else realpath(value, strict=strict)

        def attributes(value, *args, **kwargs):
            key = os.fspath(value)
            return standins[key] if key in standins else realstat(value, *args, **kwargs)

        with patch("os.path.realpath", side_effect=canonical), patch("os.stat", side_effect=attributes):
            for root in broad:
                with self.subTest(runtime_root=root):
                    with self.assertRaises(ValueError):
                        self._build(roots=(root,))
        for root in ("/", os.path.realpath(os.path.expanduser("~"))):
            with self.subTest(workspace_root=root):
                with self.assertRaises(ValueError):
                    self._build(workspace=root)

    def test_relative_paths(self):
        missing = self.base / "missing"
        cases = (
            {"workspace": "workspace"}, {"executable": "python"},
            {"roots": ("runtime",)},
            {"workspace": str(self.workspace) + "/."},
            {"executable": str(self.base) + "/./python"},
            {"roots": (str(self.runtime) + "/",)},
            {"workspace": missing}, {"executable": missing},
            {"roots": (str(missing),)},
            {"workspace": self.executable}, {"executable": self.workspace},
            {"roots": (str(self.executable),)}, {"roots": [str(self.runtime)]},
        )
        for index, kwargs in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    self._build(**kwargs)

    def test_overlap(self):
        binary = self.workspace / "python"
        binary.touch()
        nested_runtime = self.workspace / "runtime"
        nested_runtime.mkdir()
        cases = (
            {"executable": binary},
            {"roots": (str(nested_runtime),)},
            {"roots": (str(self.base),)},
            {"roots": (str(self.workspace),)},
            {"workspace": os.path.realpath("/usr/lib")},
        )
        for index, kwargs in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    self._build(**kwargs)

    def test_workspace_contains_canonical_protected_target(self):
        executable = "/opt/runtime/python"
        identities = {}

        def canonical(value, strict=False):
            return "/private/etc" if value == "/etc" else value

        def attributes(value, *args, **kwargs):
            path = canonical(os.fspath(value))
            inode = identities.setdefault(path, len(identities) + 1)
            mode = 0o100755 if path == executable else 0o040755
            return os.stat_result((mode, inode, 1, 1, 0, 0, 0, 0, 0, 0))

        with patch("os.path.realpath", side_effect=canonical) as realpath, \
                patch("os.path.expanduser", return_value="/home"), \
                patch("os.stat", side_effect=attributes):
            with self.assertRaisesRegex(ValueError, "workspace must be disjoint"):
                build_policy("/private", executable, ("/opt/runtime",))
            realpath.assert_any_call("/etc", strict=True)
