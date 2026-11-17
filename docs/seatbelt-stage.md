# Exclusive Seatbelt staging

`stage_and_run(parent, candidate, driver, executable, runtime_roots, timeout=3.0)` prepares one exclusive stage and invokes the runner once. Both payloads must have exact type `bytes`, contain at least one byte, and contain at most 65,536 bytes each. Payload validation happens before allocation.

`parent` must be an existing canonical absolute directory path: passing a symlink, a path containing a symlink component, or a relative path fails. Its owner must match the caller's effective UID, and group/other write permissions must be absent. The caller must keep the parent and its ancestry stable and exclusively control host-side mutation of the allocated stage throughout preparation and execution. These checks assume that trusted host context; they do not eliminate races against a host actor with equivalent privileges.

One `tempfile.mkdtemp` allocation creates this layout:

```text
parent/
  seatbelt-<unique>/       0700
    work/                 0700
      candidate.py        0600
    trusted/              0700
      driver.py           0400
```

Each file is created directly from the supplied bytes using `O_CREAT | O_EXCL | O_NOFOLLOW`, with no copied links. Writes are flushed and each file descriptor is fsynced. Directory entries are not fsynced; this API makes no crash-durability guarantee.

The invocation is `run_python(work, "candidate.py", executable, runtime_roots, timeout, trusted_script=driver_path)`. The trusted driver can read the candidate at `cwd/candidate.py`. The driver must be trusted caller-authored code; the API accepts that trust decision from the caller.

`run_python` retains its default workspace-script behavior and its existing `script_name` validation. Its optional keyword-only `trusted_script` selects an existing canonical absolute `.py` regular file outside the canonical workspace, with one hard link and no symlink. That file's parent is appended to `runtime_roots` before calling the unchanged `build_policy(workspace, executable, runtime_roots)` interface. The policy's protected canonical root, broad root, and workspace/runtime overlap checks therefore also apply to the trusted parent. A driver outside the workspace whose parent overlaps the workspace is rejected by those checks.

The policy grants the trusted runtime parent read-only access and grants pathname writes only within the workspace. The sandbox cannot modify the trusted sibling or stage parent. This OS-enforced boundary makes the driver immutable to the sandbox, even though the child shares the caller's UID. Mode `0400` is an ordinary filesystem permission: the host owner can change permissions and replace or modify the driver. Trusted host processes must preserve it while execution is in progress.

The frozen `StagedRun` contains `stage_path`, hexadecimal `candidate_sha256` and `driver_sha256`, and the runner's `Result`. Hashes identify the supplied bytes. The writable candidate may change during execution. `Result` records exit status, timeout state, stdout, and stderr; authenticated test reporting requires a separate verification protocol.

Stages remain after success, unsuccessful process results, and exceptions. Caught exceptions are chained into `StageError`, whose `stage_path` is `None` before allocation and identifies the retained stage after allocation. A failed stage can be partial. There is no automatic deletion, retry, or replay. Abrupt process death can leave a stage without a returned result or exception; a future journal owns crash recovery and any decision about subsequent execution. The caller owns eventual cleanup. Keep secrets out of staged inputs because these artifacts persist.

The three offline unit tests each invoke `stage_and_run` once. They use real temporary directories and mock the runner, checking successful staging, preservation after a runner exception, and invalid-candidate rejection before allocation. They start no subprocesses.
