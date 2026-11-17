"""Bounded local review persistence, never approval or dispatch.

Prerequisites: trusted process, trusted already-open private parent directory,
and exclusive control with no concurrent/hostile mutations, mounts or aliases.
No paths, discovery, retries, overwrite, repair or deletion are provided. The
completion marker is written last, after internal readback. This is neither an
atomic multi-file transaction nor fsync/power-loss durability or authentication.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import re
import stat

from src.review.draft_submission import DraftRequest, MAX_REQUEST_BYTES
from src.review.pr_builder import (
    MAX_DIFF_BYTES, MAX_RENDER_BYTES, MAX_SNAPSHOT_BYTES, ReviewReport,
)

__all__ = [
    "export_review_package", "read_review_package", "LocalReviewPackage",
    "PackageReceipt", "IncompletePackageReceipt", "ReviewPackageError",
    "ReviewPackageExportError",
]

_PAYLOAD_CAPS = {
    "request.json": MAX_REQUEST_BYTES,
    "report.json": MAX_SNAPSHOT_BYTES,
    "report.md": MAX_RENDER_BYTES,
    "body.md": MAX_RENDER_BYTES,
    "diff.patch": MAX_DIFF_BYTES,
}
MAX_MANIFEST_BYTES = 4096
MAX_MARKER_BYTES = 256
MAX_TOTAL_BYTES = 6_291_456  # includes manifest and completion marker
_CAPS = {**_PAYLOAD_CAPS, "manifest.json": MAX_MANIFEST_BYTES,
         "COMPLETE": MAX_MARKER_BYTES}


class ReviewPackageError(ValueError):
    """Rejected local package; never an authorization decision."""


@dataclass(frozen=True, slots=True)
class PackageReceipt:
    name: str
    request_sha256: str
    total_bytes: int
    outcome: str = field(default="complete", init=False)


@dataclass(frozen=True, slots=True)
class IncompletePackageReceipt:
    name: str
    directory_created: bool
    attempted_file: str | None
    outcome: str = field(default="incomplete", init=False)


class ReviewPackageExportError(RuntimeError):
    """Retained receipt; a post-completion close error keeps a complete receipt."""

    def __init__(self, receipt: PackageReceipt | IncompletePackageReceipt,
                 original_error: BaseException):
        super().__init__(f"package {receipt.name}: {receipt.outcome}; retained, no retry or cleanup")
        self.receipt, self.original_error = receipt, original_error


@dataclass(frozen=True, slots=True)
class LocalReviewPackage:
    """Detached review-only bytes as text. Each domain view is a new object."""

    name: str
    snapshot_json: str
    report_json: str
    human_report: str
    body: str
    diff: str
    manifest_json: str

    @property
    def request(self) -> DraftRequest:
        return DraftRequest(self.snapshot_json)

    @property
    def report(self) -> ReviewReport:
        return ReviewReport(self.report_json)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ReviewPackageError("invalid UTF-8") from exc


def _json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReviewPackageError("duplicate JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(_text(data), object_pairs_hook=pairs)
        if type(value) is not dict or _canonical(value) != data:
            raise ReviewPackageError("noncanonical JSON object")
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ReviewPackageError("invalid canonical JSON") from exc


def _name(name: str) -> None:
    if type(name) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ReviewPackageError("explicit simple child name required (1..64 ASCII characters)")


def _platform() -> None:
    if (os.name != "posix" or any(not getattr(os, name, 0) for name in
            ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK"))
            or not {os.open, os.stat, os.mkdir} <= os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
            or os.scandir not in os.supports_fd):
        raise ReviewPackageError("descriptor-relative no-follow filesystem support required")


def _stamp(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private_directory(info: os.stat_result) -> None:
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()):
        raise ReviewPackageError("trusted caller-owned private directory required")


def _parent(fd: int, handles: ExitStack) -> int:
    _platform()
    if type(fd) is not int or fd < 0:
        raise ReviewPackageError("already-open directory FD required")
    result = os.dup(fd)
    handles.callback(os.close, result)
    _private_directory(os.fstat(result))
    return result


def _child(parent: int, name: str, handles: ExitStack) -> int:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _private_directory(before)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                 | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    handles.callback(os.close, fd)
    opened = os.fstat(fd)
    linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _private_directory(opened)
    if _stamp(before) != _stamp(opened) or _stamp(opened) != _stamp(linked):
        raise ReviewPackageError("directory changed")
    return fd


def _regular(info: os.stat_result, cap: int) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()
            or not 0 <= info.st_size <= cap):
        raise ReviewPackageError("nonprivate/nonregular/linked/oversized file")


def _inventory(root: int, complete: bool) -> dict[str, os.stat_result]:
    expected = set(_CAPS) if complete else set(_CAPS) - {"COMPLETE"}
    # Incremental membership check only: never discover or open arbitrary files.
    seen = set()
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name not in expected or entry.name in seen:
                raise ReviewPackageError("extra package entry")
            seen.add(entry.name)
    if seen != expected:
        raise ReviewPackageError("incomplete package file set")
    infos = {}
    total = 0
    for name in sorted(expected):
        info = os.stat(name, dir_fd=root, follow_symlinks=False)
        _regular(info, _CAPS[name])
        total += info.st_size
        if total > MAX_TOTAL_BYTES:
            raise ReviewPackageError("total package byte bound exceeded")
        infos[name] = info
    return infos  # all per-file and total bounds checked BEFORE the first read


def _read_file(root: int, name: str, before: os.stat_result) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                 dir_fd=root)
    try:
        if _stamp(os.fstat(fd)) != _stamp(before):
            raise ReviewPackageError("file changed before read")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65_536, before.st_size + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > before.st_size:
                raise ReviewPackageError("file grew during bounded read")
            chunks.append(chunk)
        after = os.fstat(fd)
        linked = os.stat(name, dir_fd=root, follow_symlinks=False)
        if (size != before.st_size or _stamp(before) != _stamp(after)
                or _stamp(after) != _stamp(linked)):
            raise ReviewPackageError("truncated or changed file")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _payloads(request: DraftRequest) -> dict[str, bytes]:
    if type(request) is not DraftRequest:
        raise ReviewPackageError("actual prepared DraftRequest required")
    snapshot = request.snapshot_json  # capture once; never retain the caller object
    # Validate via the ACTUAL domain reconstruction, including report, target,
    # base, template/body and canonical request bytes. No dispatch object exists.
    data = DraftRequest(snapshot).data
    report = ReviewReport(data["report_json"])
    payloads = {
        "request.json": snapshot.encode("utf-8"),
        "report.json": report.snapshot_json.encode("utf-8"),
        "report.md": report.to_markdown().encode("utf-8"),
        "body.md": data["body"].encode("utf-8"),
        "diff.patch": report.diff_preview.encode("utf-8"),
    }
    if any(len(value) > _PAYLOAD_CAPS[name] for name, value in payloads.items()):
        raise ReviewPackageError("payload byte bound exceeded")
    return payloads


def _manifest(payloads: dict[str, bytes]) -> bytes:
    return _canonical({
        "schema_version": 1,
        "request_sha256": sha256(payloads["request.json"]).hexdigest(),
        "total_payload_bytes": sum(map(len, payloads.values())),
        "files": {name: {"size_bytes": len(value), "sha256": sha256(value).hexdigest()}
                  for name, value in payloads.items()},
    })


def _load(root: int, name: str, *, complete: bool) -> LocalReviewPackage:
    infos = _inventory(root, complete)
    manifest_bytes = _read_file(root, "manifest.json", infos["manifest.json"])
    manifest = _json(manifest_bytes)
    if (set(manifest) != {"schema_version", "request_sha256", "total_payload_bytes", "files"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or type(manifest["request_sha256"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["request_sha256"])
            or type(manifest["files"]) is not dict or set(manifest["files"]) != set(_PAYLOAD_CAPS)
            or type(manifest["total_payload_bytes"]) is not int
            or manifest["total_payload_bytes"] != sum(infos[n].st_size for n in _PAYLOAD_CAPS)):
        raise ReviewPackageError("invalid manifest schema or total")
    for filename in _PAYLOAD_CAPS:
        entry = manifest["files"][filename]
        if (type(entry) is not dict or set(entry) != {"size_bytes", "sha256"}
                or type(entry["size_bytes"]) is not int
                or entry["size_bytes"] != infos[filename].st_size
                or type(entry["sha256"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ReviewPackageError("invalid manifest file binding")
    if complete:
        marker = _read_file(root, "COMPLETE", infos["COMPLETE"])
        if marker != _canonical({"schema_version": 1,
                                 "manifest_sha256": sha256(manifest_bytes).hexdigest()}):
            raise ReviewPackageError("invalid completion marker")
    payloads = {}
    for filename in _PAYLOAD_CAPS:
        value = _read_file(root, filename, infos[filename])
        if sha256(value).hexdigest() != manifest["files"][filename]["sha256"]:
            raise ReviewPackageError("payload digest mismatch")
        _text(value)
        payloads[filename] = value
    if _manifest(payloads) != manifest_bytes:
        raise ReviewPackageError("manifest/request digest mismatch")
    expected = _payloads(DraftRequest(_text(payloads["request.json"])))
    if expected != payloads:
        raise ReviewPackageError("report/body/diff differs from the exact prepared request")
    return LocalReviewPackage(name, *(_text(payloads[n]) for n in _PAYLOAD_CAPS),
                              _text(manifest_bytes))


def _write_file(root: int, name: str, data: bytes, *, completion: list[bool] | None = None) -> None:
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=root)
    try:
        initial = os.fstat(fd)
        _regular(initial, 0)
        offset = 0
        while offset < len(data):
            count = os.write(fd, memoryview(data)[offset:offset + 65_536])
            if not 0 < count <= min(65_536, len(data) - offset):
                raise OSError("incomplete write")
            offset += count
        if completion is not None:
            # Full marker write is the completion point. Later close failures
            # must NEVER mislabel a readable completed package as incomplete.
            completion[0] = True
            return
        after = os.fstat(fd)
        _regular(after, len(data))
        if ((initial.st_dev, initial.st_ino) != (after.st_dev, after.st_ino)
                or after.st_size != len(data)
                or _stamp(after) != _stamp(os.stat(name, dir_fd=root, follow_symlinks=False))):
            raise ReviewPackageError("written file changed")
    finally:
        os.close(fd)


def export_review_package(request: DraftRequest, *, parent_fd: int, name: str) -> PackageReceipt:
    """Exclusively mkdir one fresh child; retain every partial/uncertain result."""
    _name(name)
    payloads = _payloads(request)
    manifest = _manifest(payloads)
    marker = _canonical({"schema_version": 1, "manifest_sha256": sha256(manifest).hexdigest()})
    total = sum(map(len, payloads.values())) + len(manifest) + len(marker)
    if (len(manifest) > MAX_MANIFEST_BYTES or len(marker) > MAX_MARKER_BYTES
            or total > MAX_TOTAL_BYTES):
        raise ReviewPackageError("package byte bound exceeded")
    created, attempted = False, None
    completed = [False]
    receipt = PackageReceipt(name, sha256(payloads["request.json"]).hexdigest(), total)
    try:
        with ExitStack() as handles:
            parent = _parent(parent_fd, handles)
            os.mkdir(name, mode=0o700, dir_fd=parent)  # no collision retry
            created = True
            root = _child(parent, name, handles)
            for filename, value in {**payloads, "manifest.json": manifest}.items():
                attempted = filename
                _write_file(root, filename, value)
            captured = _load(root, name, complete=False)
            if captured.snapshot_json.encode("utf-8") != payloads["request.json"]:
                raise ReviewPackageError("internal request readback mismatch")
            attempted = "COMPLETE"
            _write_file(root, "COMPLETE", marker, completion=completed)  # LAST
        return receipt
    except BaseException as exc:
        if completed[0]:
            raise ReviewPackageExportError(receipt, exc) from exc
        raise ReviewPackageExportError(IncompletePackageReceipt(name, created, attempted), exc) from exc


def read_review_package(*, parent_fd: int, name: str) -> LocalReviewPackage:
    """Read ONLY a complete fixed file set; return detached review-only values."""
    _name(name)
    try:
        with ExitStack() as handles:
            parent = _parent(parent_fd, handles)
            root = _child(parent, name, handles)
            return _load(root, name, complete=True)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise ReviewPackageError("invalid or incomplete local review package") from exc
