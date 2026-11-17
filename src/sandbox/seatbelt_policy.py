"""Build Seatbelt text only; this module never runs a process."""
import os
import stat
from pathlib import Path

_BROAD = ("/", "/Users", "/Library", "/usr")
_PROTECTED = ("/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/dev")


def _path(value: str, directory: bool) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("paths must be nonempty strings")
    if any(not c.isprintable() or c in ('"', "'", "\\") for c in value):
        raise ValueError("quotes, backslashes and nonprintable characters are forbidden")
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ValueError("paths must be absolute and normalized")
    try:
        if os.path.realpath(value, strict=True) != value:
            raise ValueError("symlink components and noncanonical paths are forbidden")
        mode = os.stat(value).st_mode
    except OSError:
        raise ValueError("paths must exist and be accessible") from None
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError("expected a directory" if directory else "expected a regular file")
    return value


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except FileNotFoundError:
        return a == b


def _overlap(a: str, b: str) -> bool:
    return any(
        _same(root, str(parent))
        for root, leaf in ((a, b), (b, a))
        for parent in (Path(leaf), *Path(leaf).parents)
    )


def build_policy(workspace: str, executable: str,
                 runtime_roots: tuple[str, ...]) -> str:
    """Validate trusted paths and return a deny-default Seatbelt v1 profile."""
    if not isinstance(runtime_roots, tuple):
        raise ValueError("runtime_roots must be a tuple")
    workspace = _path(workspace, True)
    executable = _path(executable, False)
    roots = tuple(dict.fromkeys(_path(p, True) for p in runtime_roots))
    if any(_same(p, broad) for p in roots for broad in _BROAD):
        raise ValueError("runtime roots must be narrow directories")
    home = os.path.realpath(os.path.expanduser("~"))
    if any(_same(workspace, p) for p in (*_BROAD, home)):
        raise ValueError("workspace cannot be a broad root or home directory")
    protected = tuple(os.path.realpath(p, strict=True) for p in _PROTECTED)
    if any(_overlap(workspace, p) for p in (*protected, executable, *roots)):
        raise ValueError("workspace must be disjoint from protected and runtime paths")
    readers = [
        f'(subpath "{p}")'
        for p in (workspace, "/System/Library", "/usr/lib", *roots)
    ]
    readers.insert(1, f'(literal "{executable}")')
    return "\n".join((
        "(version 1)",
        "(deny default)",
        f'(allow process-exec (literal "{executable}"))',
        "(allow file-read* " + " ".join(readers) + ")",
        f'(allow file-write* (subpath "{workspace}"))',
        "",
    ))
