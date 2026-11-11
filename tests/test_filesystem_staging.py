"""Exactly three local filesystem cases; no source, command or runner execution."""

from contextlib import ExitStack, contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat

import pytest

from src.agent import filesystem_staging as staging
from src.agent.filesystem_staging import (
    FilesystemStaging, IncompleteStageReceipt, ManifestFile, StageReceipt, StagingError,
)
from src.agent.snapshot_workspace import SnapshotError, SnapshotLimits, SnapshotWorkspace, TextFile


ATTEMPT = "filesystem-fixture"
BOUNDS = {"timeout_seconds": 5, "max_bytes": 262_144}
MANIFEST = (
    ManifestFile("src/answer.py", "source"),
    ManifestFile("tests/expected.txt", "test"),
    ManifestFile("runner/fixture.txt", "runner"),
)


class _UnusedAssessor:
    def assess(self, *args, **kwargs):
        raise AssertionError("filesystem staging must not invoke a runner")


@contextmanager
def _caller_fds(source: Path, parent: Path):
    with ExitStack() as handles:
        descriptors = []
        for path in (source, parent):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            handles.callback(os.close, fd)
            descriptors.append(fd)
        yield tuple(descriptors)


def _adapter(source_fd, parent_fd, **overrides):
    options = dict(
        manifest=MANIFEST, editable_paths=("src/answer.py",),
        context_paths=("src/answer.py", "tests/expected.txt"), assessor=_UnusedAssessor(),
    )
    options.update(overrides)
    return FilesystemStaging(
        ATTEMPT, source_fd=source_fd, staging_parent_fd=parent_fd, **options,
    )


def _fixture(source: Path) -> dict[str, bytes]:
    contents = {
        "src/answer.py": b"ANSWER = 1\n",
        "tests/expected.txt": b"ANSWER = 2\n",
        # Exceeds read_file's output cap: complete staging uses bounded export.
        "runner/fixture.txt": ("fixture: café\n" + "x" * 70_000 + "\n").encode("utf-8"),
    }
    for path, data in contents.items():
        destination = source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return contents


def _patch(path: str, before: str, after: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-{before}\n+{after}\n"


def test_import_patch_and_verified_stage_preserve_source_and_caller_fds(tmp_path: Path) -> None:
    source, parent = tmp_path / "source", tmp_path / "stages"
    source.mkdir()
    parent.mkdir()
    original = _fixture(source)
    (source / "unselected.bin").write_bytes(b"\xff")
    with _caller_fds(source, parent) as (source_fd, parent_fd):
        identities = tuple(os.fstat(fd).st_ino for fd in (source_fd, parent_fd))
        with _adapter(source_fd, parent_fd) as adapter:
            workspace = adapter.import_workspace(ATTEMPT, timeout_seconds=5)
            assert type(workspace) is SnapshotWorkspace
            assert workspace.apply_patch(
                ATTEMPT, _patch("src/answer.py", "ANSWER = 1", "ANSWER = 2"), **BOUNDS,
            )
            with pytest.raises(SnapshotError, match="protected"):
                workspace.apply_patch(
                    ATTEMPT, _patch("tests/expected.txt", "ANSWER = 2", "ANSWER = 9"), **BOUNDS,
                )
            receipt = adapter.stage(ATTEMPT, workspace, timeout_seconds=5)
            assert type(receipt) is StageReceipt and receipt.outcome == "complete"
            assert receipt.attempt_id == ATTEMPT and adapter.state == "complete"
            assert receipt.stage_name.startswith("stage-") and "/" not in receipt.stage_name
            expected = original | {"src/answer.py": b"ANSWER = 2\n"}
            assert receipt.total_bytes == sum(map(len, expected.values()))
            assert tuple(item.path for item in receipt.files) == tuple(sorted(expected))
            assert {item.path: item.role for item in receipt.files} == {
                entry.path: entry.role for entry in MANIFEST
            }
            stage = parent / receipt.stage_name
            assert stat.S_IMODE(stage.stat().st_mode) == 0o700
            for item in receipt.files:
                destination = stage / item.path
                assert destination.read_bytes() == expected[item.path]
                assert item.sha256 == sha256(expected[item.path]).hexdigest()
                assert item.size_bytes == len(expected[item.path])
                assert stat.S_IMODE(destination.stat().st_mode) == 0o600
                assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
            assert not (stage / "unselected.bin").exists()
            with pytest.raises(StagingError, match="single-use") as consumed:
                adapter.stage(ATTEMPT, workspace, timeout_seconds=5)
            assert consumed.value.receipt is receipt
        assert tuple(os.fstat(fd).st_ino for fd in (source_fd, parent_fd)) == identities
        leaf = os.open("src/answer.py", os.O_RDONLY, dir_fd=source_fd)
        try:
            assert os.read(leaf, 100) == original["src/answer.py"]
        finally:
            os.close(leaf)
        assert tuple(path.name for path in parent.iterdir()) == (receipt.stage_name,)
    assert all((source / path).read_bytes() == data for path, data in original.items())
    assert (source / "unselected.bin").read_bytes() == b"\xff"


def test_rejections_do_not_create_stages_or_write_outside_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, parent, outside = (tmp_path / name for name in ("source", "stages", "outside"))
    source.mkdir()
    parent.mkdir()
    outside.mkdir()
    original = _fixture(source)
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside unchanged\n")
    (source / "leaf.txt").symlink_to(sentinel)
    (source / "linked").symlink_to(outside, target_is_directory=True)
    (source / "hard.txt").hardlink_to(sentinel)
    (source / "directory.txt").mkdir()
    os.mkfifo(source / "pipe.txt", 0o600)
    (source / "invalid.txt").write_bytes(b"\xff\xfe")
    (source / "oversized.txt").write_bytes(b"12345")
    (source / "other.txt").write_bytes(b"12345")
    with _caller_fds(source, parent) as (source_fd, parent_fd):
        # Invalid namespace/roles/selections must fail before even dup/fstat/open.
        def forbidden_io(*args, **kwargs):
            raise AssertionError("preflight touched the filesystem")

        with monkeypatch.context() as preflight:
            for name in ("dup", "fstat", "stat", "open", "mkdir"):
                preflight.setattr(staging.os, name, forbidden_io)
            invalid = (
                {"manifest": (ManifestFile("../outside/sentinel.txt", "source"),)},
                {"manifest": (ManifestFile(str(sentinel), "source"),)},
                {"manifest": (ManifestFile("src//answer.py", "source"),)},
                {"manifest": (ManifestFile(".git/config", "source"),)},
                {"manifest": (ManifestFile("CON.txt", "source"),)},
                {"manifest": (ManifestFile("a.txt", "source"), ManifestFile("A.txt", "test"))},
                {"manifest": (ManifestFile("a", "source"), ManifestFile("a/b", "source"))},
                {"manifest": (ManifestFile("a.txt", "unknown"),)},
                {"editable_paths": ("tests/expected.txt",)},
                {"editable_paths": ("runner/fixture.txt",)},
                {"context_paths": ("missing.txt",)},
                {"limits": SnapshotLimits(max_files=1)},
            )
            for options in invalid:
                with pytest.raises(SnapshotError):
                    _adapter(source_fd, parent_fd, **options)

        # Each selected filesystem entry is explicit; no discovery or execution.
        rejected = (
            ("leaf.txt", SnapshotLimits()), ("linked/sentinel.txt", SnapshotLimits()),
            ("hard.txt", SnapshotLimits()), ("directory.txt", SnapshotLimits()),
            ("pipe.txt", SnapshotLimits()), ("missing.txt", SnapshotLimits()),
            ("invalid.txt", SnapshotLimits()), ("oversized.txt", SnapshotLimits(max_file_bytes=4)),
        )
        for path, limits in rejected:
            with _adapter(
                source_fd, parent_fd, manifest=(ManifestFile(path, "source"),),
                editable_paths=(path,), context_paths=(), limits=limits,
            ) as adapter:
                with pytest.raises((SnapshotError, OSError)):
                    adapter.import_workspace(ATTEMPT, timeout_seconds=5)
                assert adapter.state == "failed"
        with _adapter(
            source_fd, parent_fd,
            manifest=(ManifestFile("oversized.txt", "source"), ManifestFile("other.txt", "source")),
            editable_paths=(), context_paths=(), limits=SnapshotLimits(max_total_bytes=9),
        ) as aggregate:
            with pytest.raises(SnapshotError, match="bound"):
                aggregate.import_workspace(ATTEMPT, timeout_seconds=5)

        # An observed metadata change during a real read must reject import.
        changing = source / "changing.txt"
        changing.write_bytes(b"stable bytes\n")
        real_read = os.read
        changed = []

        def changed_read(fd, count):
            data = real_read(fd, count)
            if data and not changed:
                before = changing.stat()
                os.utime(changing, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
                changed.append(True)
            return data

        with _adapter(
            source_fd, parent_fd, manifest=(ManifestFile("changing.txt", "source"),),
            editable_paths=("changing.txt",), context_paths=(),
        ) as unstable:
            with monkeypatch.context() as during_read:
                during_read.setattr(staging.os, "read", changed_read)
                with pytest.raises(SnapshotError, match="changed"):
                    unstable.import_workspace(ATTEMPT, timeout_seconds=5)
            assert unstable.state == "failed" and changed
        assert changing.read_bytes() == b"stable bytes\n"

        with _adapter(source_fd, parent_fd) as adapter:
            with pytest.raises(SnapshotError, match="attempt"):
                adapter.import_workspace("wrong-attempt", timeout_seconds=5)
            workspace = adapter.import_workspace(ATTEMPT, timeout_seconds=5)
            twin = SnapshotWorkspace(
                ATTEMPT, tuple(TextFile(entry.path, "", entry.role) for entry in MANIFEST),
                editable_paths=("src/answer.py",), context_paths=(), assessor=_UnusedAssessor(),
            )
            with pytest.raises(SnapshotError, match="attempt"):
                adapter.stage("wrong-attempt", workspace, timeout_seconds=5)
            with pytest.raises(SnapshotError, match="identity"):
                adapter.stage(ATTEMPT, twin, timeout_seconds=5)
            with pytest.raises(SnapshotError, match="timeout"):
                adapter.stage(ATTEMPT, workspace, timeout_seconds=float("inf"))
            assert adapter.state == "imported"
        assert all(stat.S_ISDIR(os.fstat(fd).st_mode) for fd in (source_fd, parent_fd))
    assert list(parent.iterdir()) == []
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel.txt"]
    assert sentinel.read_bytes() == b"outside unchanged\n"
    assert all((source / path).read_bytes() == data for path, data in original.items())


def test_partial_failure_receipt_is_terminal_and_releases_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, parent = tmp_path / "source", tmp_path / "stages"
    source.mkdir()
    parent.mkdir()
    original = _fixture(source)
    real_owned, real_open, real_close, real_write = staging._owned, os.open, os.close, os.write
    owned, closed = set(), []

    def track_owned(stack, fd):
        owned.add(fd)
        return real_owned(stack, fd)

    def track_close(fd):
        assert fd in owned, "adapter closed a caller-owned or already closed descriptor"
        owned.remove(fd)
        closed.append(fd)
        real_close(fd)

    with _caller_fds(source, parent) as (source_fd, parent_fd):
        with _adapter(source_fd, parent_fd) as adapter:
            # Track real descriptor allocations without changing platform probes.
            with monkeypatch.context() as tracking:
                tracking.setattr(staging, "_owned", track_owned)
                tracking.setattr(staging.os, "close", track_close)
                workspace = adapter.import_workspace(ATTEMPT, timeout_seconds=5)
                assert workspace.apply_patch(
                    ATTEMPT, _patch("src/answer.py", "ANSWER = 1", "ANSWER = 2"), **BOUNDS,
                )
                creations, writes = [], []

                def failing_open(name, flags, *args, **kwargs):
                    fd = real_open(name, flags, *args, **kwargs)
                    if flags & os.O_CREAT:
                        creations.append(name)
                    return fd

                def fail_after_some_writes(fd, data):
                    if len(creations) >= 2:
                        # A real partial write, then an uncertain failing receipt.
                        real_write(fd, data[:3])
                        raise OSError("injected failure after partial write")
                    # Exercise bounded completion of actual short writes, too.
                    count = real_write(fd, data[:4096])
                    writes.append(count)
                    return count

                tracking.setattr(staging.os, "open", failing_open)
                tracking.setattr(staging.os, "write", fail_after_some_writes)
                with pytest.raises(StagingError) as failed:
                    adapter.stage(ATTEMPT, workspace, timeout_seconds=5)
                receipt = failed.value.receipt
                assert type(receipt) is IncompleteStageReceipt
                assert receipt.outcome == "partial_or_unknown" and receipt.attempt_id == ATTEMPT
                assert receipt.stage_name in str(failed.value)
                assert "injected failure" in str(failed.value.original_error)
                assert receipt.attempted_path == "src/answer.py"
                assert tuple(item.path for item in receipt.verified_files) == ("runner/fixture.txt",)
                assert receipt.verified_total_bytes == len(original["runner/fixture.txt"])
                assert receipt.verified_files[0].sha256 == sha256(original["runner/fixture.txt"]).hexdigest()
                assert len(writes) > 1 and adapter.state == "failed"
                assert not owned and closed
                for fd in set(closed):
                    with pytest.raises(OSError):
                        os.fstat(fd)
                previous = (len(creations), len(writes), len(closed))
                with pytest.raises(StagingError) as repeated:
                    adapter.stage(ATTEMPT, workspace, timeout_seconds=5)
                assert repeated.value.receipt is receipt
                assert previous == (len(creations), len(writes), len(closed))
            # No rollback/deletion: owner can inspect precisely the named stage.
            stage = parent / receipt.stage_name
            assert tuple(path.name for path in parent.iterdir()) == (receipt.stage_name,)
            assert (stage / "runner/fixture.txt").read_bytes() == original["runner/fixture.txt"]
            assert (stage / "src/answer.py").read_bytes() == b"ANS"
            assert not (stage / "tests/expected.txt").exists()
        assert stat.S_ISDIR(os.fstat(source_fd).st_mode)
        assert stat.S_ISDIR(os.fstat(parent_fd).st_mode)
    assert not owned
    assert all((source / path).read_bytes() == data for path, data in original.items())
