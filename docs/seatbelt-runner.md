# Seatbelt runner

`run_python(workspace, script_name, executable, runtime_roots, timeout=3.0)` returns frozen `Result(returncode, timed_out, stdout, stderr)`. `success` requires return code zero and no timeout. Timeout accepts finite positive integers or floats up to 10 seconds; booleans are rejected.

Darwin and a regular, nonsymlink `/usr/bin/sandbox-exec` are required. Policy, validation, and launch errors propagate; there is no host fallback. `build_policy` validates canonical trusted paths. The script must be one `.py` basename, regular, nonsymlink, and have exactly one hard link.

Call only from a trusted, single-threaded parent because resource limits use `preexec_fn`. The caller must trust the executable and runtime roots, maintain exclusive control of the workspace, and keep its ancestors stable. Path checks do not prevent replacement races before Python opens the script. The workspace parent must be trusted and writable; filesystem root cannot be the workspace.

The child uses `-I -S -B`, workspace cwd, closed stdin, and only `HOME`, `TMPDIR` (both workspace), and `LC_ALL=C` in its environment. CPU, per-file size, descriptor, and core limits are respectively 10 seconds, 65536 bytes, 64, and zero.

Output goes to anonymous temporary files in the workspace parent. Only the first 32768 bytes of each stream are returned; truncation is silent. There is no memory or aggregate disk bound. Timeout covers the initial child wait; timeout and exception cleanup send SIGKILL to its process group and reap the direct child. Descendants that escape that group are not covered.

The four tests mock launch and filesystem boundaries. They have not been executed here and do not establish macOS policy acceptance or enforcement.
