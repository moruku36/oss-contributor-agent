"""Explicit directory-descriptor import and single-use staging; no execution.

The trusted caller supplies already-open, exclusively controlled source and
staging-parent directories and an exhaustive *selected-file* manifest. These
roots must be disjoint, with no concurrent mutations, hostile mounts or aliases;
descriptor checks reject identical roots but cannot prove their ancestry or
container binding. No arbitrary host Path, discovery, runner or CLI is supplied.
Python deadline checks cannot preempt blocked filesystem I/O. Unsupported
descriptor/no-follow operations fail closed. This is not isolation evidence.

Use as a context manager (or close explicitly) if staging is abandoned. Caller
FDs remain caller-owned; only duplicates and internal traversal FDs are closed.
Staging consumes its dispatch before mkdir. Partial/uncertain output is retained
for the owning sandbox backend/operator: no retry, deletion, rollback, atomic
multi-file update or crash-durability claim is provided.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
import os
import stat
from time import monotonic
from uuid import uuid4

from src.agent.snapshot_workspace import (
    RunnerAssessor, SnapshotError, SnapshotLimits, SnapshotWorkspace, TextFile,
)


@dataclass(frozen=True)
class ManifestFile:
    path: str
    role: str


@dataclass(frozen=True)
class StagedFile:
    path: str
    role: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class StageReceipt:
    attempt_id: str
    stage_name: str  # relative to the supplied staging-parent descriptor
    files: tuple[StagedFile, ...]
    total_bytes: int
    outcome: str = field(default="complete", init=False)


@dataclass(frozen=True)
class IncompleteStageReceipt:
    attempt_id: str
    stage_name: str
    verified_files: tuple[StagedFile, ...]
    verified_total_bytes: int
    attempted_path: str | None
    outcome: str = field(default="partial_or_unknown", init=False)


class StagingError(RuntimeError):
    """Terminal dispatch receipt, never a success or cleanup confirmation."""

    def __init__(
        self, receipt: StageReceipt | IncompleteStageReceipt,
        original_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            f"stage {receipt.stage_name} for {receipt.attempt_id}: {receipt.outcome}; "
            "single-use dispatch; no automatic retry or cleanup"
        )
        self.receipt = receipt
        self.original_error = original_error
        self.close_error = close_error


def _deadline(seconds: float) -> float:
    try:
        valid = (not isinstance(seconds, bool) and isinstance(seconds, (int, float))
                 and isfinite(seconds) and 0 < seconds <= 30)
    except OverflowError:
        valid = False
    if not valid:
        raise SnapshotError("filesystem timeout must be finite, positive and at most 30 seconds")
    return monotonic() + seconds


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("filesystem operation deadline exhausted")
    return remaining


def _platform() -> None:
    if (os.name != "posix"
            or any(not getattr(os, name, 0) for name in
                   ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"))
            or not {os.open, os.stat, os.mkdir} <= os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks):
        raise SnapshotError("descriptor-relative no-follow filesystem support required")


def _owned(stack: ExitStack, fd: int) -> int:
    stack.callback(os.close, fd)
    return fd


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    # Access time may change on read; content/identity metadata must not.
    return (_identity(info) + (info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
                              info.st_size, info.st_mtime_ns, info.st_ctime_ns))


def _regular(info: os.stat_result, cap: int, *, private: bool = False) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SnapshotError("only regular files with one link are accepted")
    if not 0 <= info.st_size <= cap:
        raise SnapshotError("file size exceeds byte bound")
    if private and stat.S_IMODE(info.st_mode) != 0o600:
        raise SnapshotError("staged file is not private")


def _directory(
    parent: int, name: str, stack: ExitStack, deadline: float,
    *, private: bool = False,
) -> tuple[int, tuple[int, int]]:
    _remaining(deadline)
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotError("symlink or non-directory path component")
    fd = _owned(stack, os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=parent,
    ))
    after = os.fstat(fd)
    linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(after.st_mode) or _identity(before) != _identity(after)
            or _identity(after) != _identity(linked) or after.st_mode != linked.st_mode):
        raise SnapshotError("directory identity changed")
    if private and stat.S_IMODE(after.st_mode) != 0o700:
        raise SnapshotError("staged directory is not private")
    _remaining(deadline)
    return fd, _identity(after)


def _read_bytes(fd: int, cap: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        _remaining(deadline)
        chunk = os.read(fd, min(65_536, cap + 1 - size))
        _remaining(deadline)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > cap:
            raise SnapshotError("actual file bytes exceed bound")
        chunks.append(chunk)


def _directory_link(
    parent: int, name: str, identity: tuple[int, int], deadline: float,
    *, private: bool = False,
) -> None:
    _remaining(deadline)
    linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(linked.st_mode) or _identity(linked) != identity
            or (private and stat.S_IMODE(linked.st_mode) != 0o700)):
        raise SnapshotError("directory link changed")
    _remaining(deadline)


class FilesystemStaging:
    """Single-caller capability adapter bound to one imported snapshot identity.

    Construction validates all manifest paths/roles/edit/context selections via
    SnapshotWorkspace before any filesystem access. Keep caller FDs open until
    import_workspace duplicates them. stage accepts only that returned workspace
    and stages its current contents, including immutable tests/runner evidence.
    """

    def __init__(
        self, attempt_id: str, *, source_fd: int, staging_parent_fd: int,
        manifest: tuple[ManifestFile, ...], editable_paths: tuple[str, ...],
        context_paths: tuple[str, ...], assessor: RunnerAssessor,
        limits: SnapshotLimits = SnapshotLimits(),
    ) -> None:
        if (type(limits) is not SnapshotLimits or type(manifest) is not tuple
                or not 1 <= len(manifest) <= limits.max_files
                or any(type(entry) is not ManifestFile for entry in manifest)):
            raise SnapshotError("bounded explicit ManifestFile tuple required")
        # Empty contents validate the whole namespace and selections without I/O.
        SnapshotWorkspace(
            attempt_id, tuple(TextFile(entry.path, "", entry.role) for entry in manifest),
            editable_paths=editable_paths, context_paths=context_paths,
            assessor=assessor, limits=limits,
        )
        if any(type(fd) is not int or fd < 0 for fd in (source_fd, staging_parent_fd)):
            raise SnapshotError("explicit already-open directory descriptors required")
        self._attempt_id = attempt_id
        self._source_fd, self._parent_input = source_fd, staging_parent_fd
        self._manifest, self._editable, self._context = manifest, editable_paths, context_paths
        self._assessor, self._limits = assessor, limits
        self._owned = ExitStack()
        self._workspace: SnapshotWorkspace | None = None
        self._parent: int | None = None
        self._state = "new"
        self._receipt: StageReceipt | IncompleteStageReceipt | None = None

    @property
    def state(self) -> str:
        return self._state

    def _attempt(self, attempt_id: str) -> None:
        if type(attempt_id) is not str or attempt_id != self._attempt_id:
            raise SnapshotError("wrong filesystem workspace attempt")

    def __enter__(self) -> FilesystemStaging:
        if self._state not in {"new", "imported"}:
            raise SnapshotError("filesystem capability is closed or consumed")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._state in {"new", "imported"}:
            self._state = "closed"
        # ExitStack tries every close even if a close fails; never retry a close.
        self._owned.close()

    def import_workspace(self, attempt_id: str, *, timeout_seconds: float) -> SnapshotWorkspace:
        self._attempt(attempt_id)
        if self._state != "new":
            raise SnapshotError("filesystem import is single-use")
        deadline = _deadline(timeout_seconds)
        _platform()
        self._state = "importing"
        try:
            with ExitStack() as source_handles:
                _remaining(deadline)
                source = _owned(source_handles, os.dup(self._source_fd))
                self._parent = _owned(self._owned, os.dup(self._parent_input))
                roots = (os.fstat(source), os.fstat(self._parent))
                if (any(not stat.S_ISDIR(root.st_mode) for root in roots)
                        or _identity(roots[0]) == _identity(roots[1])):
                    raise SnapshotError("distinct trusted source and staging-parent directories required")
                aliases = {_identity(roots[0]): ""}
                entries: list[TextFile] = []
                total = 0
                for entry in self._manifest:
                    cap = min(self._limits.max_file_bytes, self._limits.max_total_bytes - total)
                    data = self._import_file(source, entry.path, cap, deadline, aliases)
                    total += len(data)
                    try:
                        text = data.decode("utf-8", errors="strict")
                    except UnicodeError:
                        raise SnapshotError("invalid UTF-8 file") from None
                    entries.append(TextFile(entry.path, text, entry.role))
                workspace = SnapshotWorkspace(
                    attempt_id, tuple(entries), editable_paths=self._editable,
                    context_paths=self._context, assessor=self._assessor, limits=self._limits,
                )
            _remaining(deadline)
        except BaseException:
            self._state = "failed"
            self.close()
            raise
        self._workspace = workspace
        self._state = "imported"
        return workspace

    @staticmethod
    def _import_file(
        root: int, path: str, cap: int, deadline: float,
        aliases: dict[tuple[int, int], str],
    ) -> bytes:
        with ExitStack() as handles:
            parent = root
            links: list[tuple[int, str, tuple[int, int]]] = []
            parts = path.split("/")
            for index, component in enumerate(parts[:-1], 1):
                child, identity = _directory(parent, component, handles, deadline)
                prefix = "/".join(parts[:index])
                if identity in aliases and aliases[identity] != prefix:
                    raise SnapshotError("filesystem directory alias")
                aliases[identity] = prefix
                links.append((parent, component, identity))
                parent = child
            _remaining(deadline)
            before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            _regular(before, cap)
            if _identity(before) in aliases:
                raise SnapshotError("filesystem file alias")
            fd = _owned(handles, os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent,
            ))
            opened = os.fstat(fd)
            _regular(opened, cap)
            if _stamp(before) != _stamp(opened):
                raise SnapshotError("file identity changed before read")
            data = _read_bytes(fd, cap, deadline)
            after = os.fstat(fd)
            linked = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (_stamp(before) != _stamp(after) or _stamp(after) != _stamp(linked)
                    or len(data) != after.st_size):
                raise SnapshotError("file identity or bytes changed during read")
            for directory, component, identity in links:
                _directory_link(directory, component, identity, deadline)
            aliases[_identity(after)] = path
            _remaining(deadline)
            return data

    def stage(
        self, attempt_id: str, workspace: SnapshotWorkspace, *, timeout_seconds: float,
    ) -> StageReceipt:
        self._attempt(attempt_id)
        if self._receipt is not None:
            raise StagingError(self._receipt)
        if self._state != "imported" or workspace is not self._workspace:
            raise SnapshotError("stage requires the imported workspace identity")
        deadline = _deadline(timeout_seconds)
        files = workspace.export_files(
            attempt_id, timeout_seconds=_remaining(deadline), max_bytes=self._limits.max_total_bytes,
        )
        # Reuse snapshot validation before mkdir, including roles/selections/bounds.
        SnapshotWorkspace(
            attempt_id, files, editable_paths=self._editable, context_paths=self._context,
            assessor=self._assessor, limits=self._limits,
        )
        if tuple((entry.path, entry.role) for entry in files) != tuple(sorted(
            (entry.path, entry.role) for entry in self._manifest
        )):
            raise SnapshotError("imported manifest changed")
        payloads = tuple((entry, entry.text.encode("utf-8")) for entry in files)
        _remaining(deadline)
        name = "stage-" + uuid4().hex
        verified: list[StagedFile] = []
        attempted: str | None = None
        self._receipt = IncompleteStageReceipt(attempt_id, name, (), 0, None)
        self._state = "staging"  # consumed before the first possible side effect
        try:
            with self._owned, ExitStack() as handles:
                assert self._parent is not None
                _remaining(deadline)
                os.mkdir(name, mode=0o700, dir_fd=self._parent)  # one name, no collision retry
                root, root_identity = _directory(self._parent, name, handles, deadline, private=True)
                created: dict[str, tuple[int, int]] = {}
                for entry, data in payloads:
                    attempted = entry.path
                    self._stage_file(root, entry.path, data, created, deadline)
                    verified.append(StagedFile(entry.path, entry.role, sha256(data).hexdigest(), len(data)))
                _directory_link(self._parent, name, root_identity, deadline, private=True)
                _remaining(deadline)
            _remaining(deadline)  # includes descriptor closing; no late success
            receipt = StageReceipt(
                attempt_id, name, tuple(verified), sum(item.size_bytes for item in verified),
            )
            _remaining(deadline)
            self._receipt = receipt
            self._state = "complete"
            return receipt
        except BaseException as error:
            self._state = "failed"
            self._receipt = IncompleteStageReceipt(
                attempt_id, name, tuple(verified), sum(item.size_bytes for item in verified), attempted,
            )
            close_error = None
            try:
                self.close()
            except BaseException as closing:
                close_error = closing
            raise StagingError(self._receipt, error, close_error) from error

    @staticmethod
    def _stage_file(
        root: int, path: str, data: bytes, created: dict[str, tuple[int, int]], deadline: float,
    ) -> None:
        with ExitStack() as handles:
            parent = root
            parts = path.split("/")
            links: list[tuple[int, str, tuple[int, int]]] = []
            for index, component in enumerate(parts[:-1], 1):
                prefix = "/".join(parts[:index])
                _remaining(deadline)
                if prefix not in created:
                    os.mkdir(component, mode=0o700, dir_fd=parent)
                child, identity = _directory(parent, component, handles, deadline, private=True)
                if prefix in created and created[prefix] != identity:
                    raise SnapshotError("staged directory identity changed")
                created[prefix] = identity
                links.append((parent, component, identity))
                parent = child
            _remaining(deadline)
            fd = _owned(handles, os.open(
                parts[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=parent,
            ))
            initial = os.fstat(fd)
            _regular(initial, 0, private=True)
            offset = 0
            while offset < len(data):
                _remaining(deadline)
                written = os.write(fd, memoryview(data)[offset:offset + 65_536])
                if not 0 < written <= min(65_536, len(data) - offset):
                    raise OSError("incomplete or invalid stage write")
                offset += written
            before = os.fstat(fd)
            _regular(before, len(data), private=True)
            if _identity(initial) != _identity(before) or before.st_size != len(data):
                raise SnapshotError("staged file identity or size changed")
            _remaining(deadline)
            os.lseek(fd, 0, os.SEEK_SET)
            readback = _read_bytes(fd, len(data), deadline)
            after = os.fstat(fd)
            linked = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (readback != data or _stamp(before) != _stamp(after)
                    or _stamp(after) != _stamp(linked)):
                raise SnapshotError("staged readback or identity verification failed")
            for directory, component, identity in links:
                _directory_link(directory, component, identity, deadline, private=True)
            _remaining(deadline)
