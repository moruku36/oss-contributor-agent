# Seatbelt root-open compatibility

The original startup crash was a concrete dyld root-directory open failure.
The kernel denied file-read-data on / for the crashing Python process.
Its boot_boot frame had image offset 505048 (0x7b4d8), symbol offset 228
from 0x7b3f4. These offsets correlate the denial with the failure path.

Disassembly of the installed /usr/lib/dyld loads the literal / at 0x7b424
and calls _open at 0x7b430. A negative result branches at 0x7b438 to
0x7b4a0, which loads errno and the message
“failed to open root directory: %s: %d”, then calls _ignition_halt at
0x7b4d4. Its saved return PC, 0x7b4d8, exactly matches the crash frame.
On success, the directory descriptor goes to _boot_set_root before relative
discovery. This establishes the original root-open SIGABRT mechanism.

The narrow policy addition in src/sandbox/seatbelt_policy.py is:

```scheme
(allow file-read-data (literal "/"))
```

This exposes root-directory entry names; descendant contents still require
existing read rules. It adds no recursive root, global metadata, or sysctl
access. Both existing Cryptex dyld read clauses remain. Concurrent denials
for security.mac.lockdown_mode_state and kern.bootargs justify no new grants.

Kurotchi/the operator explicitly selects the canonical actual Python.app
executable from the SAME runtime as the configured runtime roots. A public
installation example, also the executable used in the measured runs, is:

```text
/Library/Frameworks/Python.framework/Versions/3.15/Resources/Python.app/Contents/MacOS/Python
```

The framework bin launcher spawns that app executable and cannot complete
that transition under the single selected-executable policy. The existing
runner accepts the actual app executable as explicit caller input. There is
no automatic detection or fallback, second executable, or process-fork grant.
Canonical path, file type, narrow runtime-root, and workspace separation
validation remain. An executable identity allowance is not a one-exec counter.

diag05 subtracted all diagnostic metadata additions while keeping the direct
same-runtime app executable and root-data-only addition. Its trusted startup
marker passed, exit 0, without a timeout. Ancestor metadata grants were not
needed for this measured startup; the speculative metadata closure is obsolete.
That marker alone did not exercise staging or the target.

Later retained results used the real agent journal → staging → Seatbelt path:

- Compatibility: all eight fixed, reviewed checks passed (8/8): startup,
  workspace writing, blocked external reads, blocked external writes, blocked
  network, blocked child processes, blocked trusted-driver writes, and a clean
  environment. Exit 0, no timeout, journal SUCCEEDED; retained hashes matched.
- srt pinned at ff96458d16e8e9f28315687fbed2bb23b013c6b0: baseline completed
  three methods with two AttributeErrors (foreign reflected comparison and
  unsupported operand) and one ordering-regression pass; exit 1, journal FAILED.
  The fixed candidate completed the same three methods with three passes,
  no errors, failures, skips, or timeout; exit 0, journal SUCCEEDED.

The coordinator-retained offline receipt also records three policy/validation
methods passing. This documentation correction itself runs no tests or probes.
Default deny, workspace-only writes, environment construction, execution limits,
and the staged trusted driver outside the writable workspace remain unchanged.
The driver retains mode 0400. Stages and evidence artifacts remain retained;
these results make no artifact-cleanup claim.

These were human-supervised runs with explicit Kurotchi/operator orchestration.
Only this installed Python/macOS combination was tested. The fixed compatibility
source is controlled evidence, not authentication of arbitrary candidate reports.
No broader CI or platform matrix, automatic CoderLoop operation, or atomic
product PR acceptance is established by these measurements.
