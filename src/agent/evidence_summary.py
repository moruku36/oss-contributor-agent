"""Bounded scalar copies of accepted evidence; consistency, never authentication.

The classifier is shared with the sole structured assessor. Reconstruction only
checks that its recorded classification agrees with its counts and flags.
"""
from dataclasses import dataclass, fields
from hashlib import sha256
import json
from math import isfinite
import re


@dataclass(frozen=True, slots=True)
class RunnerEvidenceSummary:
    schema_version: int
    attempt_id: str
    dispatch_id: str
    generation: int
    stage_name: str
    receipt_sha256: str
    command: str
    result_sha256: str
    exit_code: int
    duration_seconds: float
    classification: str
    tests_run: int
    passed: int
    assertion_failures: int
    test_errors: int
    skipped: int
    collection_errors: int
    infrastructure_errors: int
    complete: bool
    truncated: bool
    outcome_known: bool
    detail_sha256: str
    binding_sha256: str = ""


def summary_digest(e) -> str:
    """Bind the scalar record for reconstruction; this public hash is forgeable."""
    data = {f.name: getattr(e, f.name) for f in fields(RunnerEvidenceSummary)
            if f.name != "binding_sha256"}
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("ascii")).hexdigest()


def result_digest(result) -> str:
    """Exact command/result scalars, including complete captured output."""
    payload = [result.command, result.exit_code, result.stdout, result.stderr,
               result.duration_seconds]
    return sha256(json.dumps(payload, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("ascii")).hexdigest()


def structured_classification(tests_run, failures, test_errors, collection_errors,
                              infrastructure_errors, complete, truncated,
                              outcome_known, exit_code) -> tuple[str, str]:
    if test_errors or collection_errors or infrastructure_errors:
        return "infra_error", "test/collection/infrastructure errors"
    if not complete or truncated or not outcome_known:
        return "unknown", "incomplete, truncated or unknown outcome"
    if not tests_run:
        return "unknown", "empty or all-skipped run"
    if failures and exit_code > 0:
        return "assertion_failed", "authoritative assertion failures"
    if not failures and exit_code == 0:
        return "pass", "executed tests passed"
    return "unknown", "exit code contradicts structured summary"


def _text(value, cap):
    if (type(value) is not str or not value.strip() or len(value) > cap
            or len(value.encode("utf-8")) > cap
            or any(ord(c) < 32 and c not in "\n\t" for c in value) or "\x7f" in value):
        raise ValueError("invalid bounded evidence text")


def validate_summary(e, *, attempt_id, command, exit_code, duration_seconds,
                     outcome, tests_run, assertion_failures, detail, result=None):
    """Validate a detached record against its enclosing check, without assessing."""
    if type(e) is not RunnerEvidenceSummary:
        raise ValueError("exact scalar runner evidence required")
    if type(e.schema_version) is not int or e.schema_version != 1:
        raise ValueError("unsupported runner evidence version")
    for name, cap in (("attempt_id", 128), ("dispatch_id", 128), ("stage_name", 128),
                      ("command", 4096), ("classification", 32)):
        _text(getattr(e, name), cap)
    for name in ("receipt_sha256", "result_sha256", "detail_sha256", "binding_sha256"):
        value = getattr(e, name)
        if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("invalid evidence digest")
    if type(e.generation) is not int or not 1 <= e.generation <= 64:
        raise ValueError("invalid evidence generation")
    if type(e.exit_code) is not int or not -(2**31) <= e.exit_code < 2**31:
        raise ValueError("invalid evidence exit code")
    try:
        valid_duration = (type(e.duration_seconds) in (int, float)
                          and isfinite(e.duration_seconds) and 0 <= e.duration_seconds <= 2700)
    except OverflowError:
        valid_duration = False
    if not valid_duration:
        raise ValueError("invalid evidence duration")
    for name in ("tests_run", "passed", "assertion_failures", "test_errors", "skipped",
                 "collection_errors", "infrastructure_errors"):
        value = getattr(e, name)
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise ValueError("invalid evidence count")
    if (e.tests_run != e.passed + e.assertion_failures + e.test_errors
            or e.assertion_failures > 128
            or e.tests_run + e.skipped + e.collection_errors + e.infrastructure_errors > 1_000_000):
        raise ValueError("inconsistent evidence counts")
    if any(type(getattr(e, name)) is not bool
           for name in ("complete", "truncated", "outcome_known")):
        raise ValueError("explicit evidence flags required")
    _text(detail, 4096)
    if e.binding_sha256 != summary_digest(e):
        raise ValueError("runner scalar evidence digest mismatch")
    classification, _ = structured_classification(
        e.tests_run, e.assertion_failures, e.test_errors, e.collection_errors,
        e.infrastructure_errors, e.complete, e.truncated, e.outcome_known, e.exit_code)
    if (e.attempt_id != attempt_id or e.command != command or e.exit_code != exit_code
            or (type(e.duration_seconds), e.duration_seconds)
            != (type(duration_seconds), duration_seconds)
            or e.classification != outcome or e.classification != classification
            or e.tests_run != tests_run or e.assertion_failures != assertion_failures
            or e.detail_sha256 != sha256(detail.encode("utf-8")).hexdigest()):
        raise ValueError("runner evidence/check binding mismatch")
    if result is not None and e.result_sha256 != result_digest(result):
        raise ValueError("runner evidence/result digest mismatch")


def summary_from_data(data):
    if type(data) is not dict or set(data) != {f.name for f in fields(RunnerEvidenceSummary)}:
        raise ValueError("missing or unknown runner evidence fields")
    return RunnerEvidenceSummary(**data)
