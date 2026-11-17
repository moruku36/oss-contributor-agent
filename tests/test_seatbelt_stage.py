import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.sandbox import seatbelt_stage
from src.sandbox.seatbelt_runner import Result


class SeatbeltStageTests(unittest.TestCase):
    def setUp(self):
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.parent = os.path.realpath(parent.name)
        os.chmod(self.parent, 0o700)

    def test_success_stages_bytes_modes_hashes_and_trusted_driver(self):
        candidate = b"print('candidate')\n"
        driver = b"from pathlib import Path\nprint(Path('candidate.py').read_bytes())\n"
        result = Result(0, False, b"ok\n", b"")
        runtime_roots = ("/usr/lib",)
        with mock.patch.object(seatbelt_stage, "run_python", return_value=result) as runner:
            staged = seatbelt_stage.stage_and_run(
                self.parent, candidate, driver, "/usr/bin/python3", runtime_roots,
                timeout=1.5,
            )
        stage = Path(staged.stage_path)
        work = stage / "work"
        trusted = stage / "trusted"
        candidate_path = work / "candidate.py"
        driver_path = trusted / "driver.py"
        self.assertEqual(os.listdir(self.parent), [stage.name])
        self.assertEqual(os.path.realpath(stage), str(stage))
        self.assertEqual(candidate_path.read_bytes(), candidate)
        self.assertEqual(driver_path.read_bytes(), driver)
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(candidate_path.lstat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(driver_path.lstat().st_mode), 0o400)
        self.assertTrue(stat.S_ISREG(candidate_path.lstat().st_mode))
        self.assertTrue(stat.S_ISREG(driver_path.lstat().st_mode))
        self.assertEqual(candidate_path.lstat().st_nlink, 1)
        self.assertEqual(driver_path.lstat().st_nlink, 1)
        self.assertEqual(os.path.commonpath((work, driver_path)), str(stage))
        self.assertEqual(staged.candidate_sha256, hashlib.sha256(candidate).hexdigest())
        self.assertEqual(staged.driver_sha256, hashlib.sha256(driver).hexdigest())
        self.assertIs(staged.result, result)
        runner.assert_called_once_with(
            str(work), "candidate.py", "/usr/bin/python3", runtime_roots, 1.5,
            trusted_script=str(driver_path),
        )

    def test_runner_exception_preserves_stage(self):
        candidate = b"print('candidate')\n"
        driver = b"pass\n"
        failure = RuntimeError("runner failed")
        with mock.patch.object(seatbelt_stage, "run_python", side_effect=failure) as runner:
            with self.assertRaises(seatbelt_stage.StageError) as raised:
                seatbelt_stage.stage_and_run(
                    self.parent, candidate, driver, "/usr/bin/python3", (),
                )
        self.assertIs(raised.exception.__cause__, failure)
        self.assertIsNotNone(raised.exception.stage_path)
        stage = Path(raised.exception.stage_path)
        self.assertTrue(stage.is_dir())
        self.assertEqual(os.listdir(self.parent), [stage.name])
        self.assertEqual((stage / "work" / "candidate.py").read_bytes(), candidate)
        self.assertEqual((stage / "trusted" / "driver.py").read_bytes(), driver)
        runner.assert_called_once()

    def test_invalid_candidate_rejected_before_allocation(self):
        with mock.patch.object(seatbelt_stage.tempfile, "mkdtemp") as mkdir:
            with mock.patch.object(seatbelt_stage, "run_python") as runner:
                with self.assertRaises(seatbelt_stage.StageError) as raised:
                    seatbelt_stage.stage_and_run(
                        self.parent, bytearray(b"candidate"), b"pass\n",
                        "/usr/bin/python3", (),
                    )
        self.assertIsNone(raised.exception.stage_path)
        self.assertIsInstance(raised.exception.__cause__, ValueError)
        self.assertEqual(os.listdir(self.parent), [])
        mkdir.assert_not_called()
        runner.assert_not_called()
