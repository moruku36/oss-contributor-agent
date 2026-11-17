"""Exactly six self-contained unittest cases, authored NOT RUN here.

All report evidence and command/model strings are inert synthetic fixtures.
No fixture source, command, model or historical test module is executed.
"""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from enum import Enum
import json
import os
import tempfile
import unittest
from unittest import mock

from src.agent.core import (
    AttemptResult, CheckEvidence, LoopState as S, ModelReceipt,
    TestAssessment, TestCommand, TestOutcome as O, TestPlan,
)
from src.review import draft_submission as ds
from src.review import package_selection, pr_builder, review_package, submission_journal
from src.review import target_preflight as tp


def prepared(title="Synthetic 日本語"):
    """Build genuine domain bytes without running the described evidence."""
    def check(phase, command, outcome=O.PASS):
        failed = outcome is O.ASSERTION_FAILED
        return CheckEvidence(phase, command, int(failed), 0.001, "synthetic tail",
                             TestAssessment(outcome, 1, int(failed), "synthetic assertion"))

    target = ds.DraftTarget("https://github.com", "owner/project", "contributor/project",
                            "refs/heads/fix", "b" * 40, "refs/heads/main", "a" * 40)
    attempt = AttemptResult(
        True, 1, diff="--- a/value.py\n+++ b/value.py\n@@ -1,1 +1,1 @@\n-VALUE = 1\n+VALUE = 2\n",
        state=S.SUCCEEDED, states=[S.READY, S.SUCCEEDED],
        checks=[check(S.BASELINE, "suite"),
                check(S.REPRODUCING, "reproduce", O.ASSERTION_FAILED),
                check(S.VERIFYING_REPRODUCTION, "reproduce"),
                check(S.VERIFYING_SUITE, "suite")],
        model_calls=1, tokens_used=7, known_tokens_used=7,
        model_receipts=[ModelReceipt("synthetic", "inert-fixture", 7)],
        cleanup_confirmed=True, sandbox_attempt_id="synthetic", elapsed_seconds=0.01,
    )
    report = pr_builder.build_review_report(
        attempt, {"repo": target.repository, "number": 7, "title": "Synthetic issue"},
        base_commit=target.base_commit, execution_kind="synthetic",
        plan=TestPlan(TestCommand("reproduce", 5), (TestCommand("suite", 5),)),
    )
    return ds.prepare_draft_submission(report, target=target, title=title,
                                       pr_template="Synthetic template 日本語")


def observed(request):
    return tp.TargetObservation(
        tp.ObservationStatus.OBSERVED, ds.DraftTarget(**request.data["target"]),
        request.snapshot_json, True, True,
    )


class TargetPreflightTests(unittest.TestCase):
    def test_actual_package_selection_then_matching_observation(self):
        request = prepared()
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o700)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                review_package.export_review_package(request, parent_fd=fd, name="selected")
                selected = package_selection.load_selected_review_request(fd, "selected", request)
                reply = observed(selected)
                inspector = mock.Mock(spec=["inspect_target"])
                inspector.inspect_target.return_value = reply
                result = tp.preflight_target(selected, inspector=inspector)
                self.assertIs(result.status, tp.TargetPreflightStatus.READY)
                self.assertEqual(result.reason, "local_consistency_only")
                inspector.inspect_target.assert_called_once()
                passed = inspector.inspect_target.call_args.args[0]
                self.assertIsNot(passed, selected)
                self.assertEqual(passed.snapshot_json.encode("utf-8"), request.snapshot_json.encode("utf-8"))
                self.assertIsNot(result.observation, reply)
                self.assertIsNot(result.observation.target, reply.target)
                self.assertEqual(result.observation, reply)
                with self.assertRaises(FrozenInstanceError):
                    result.observation.draft_only = False
            finally:
                os.close(fd)

    def test_every_target_field_movement_or_mismatch_and_unsupported_capabilities(self):
        request = prepared()
        good = observed(request)
        movements = {
            "destination": "https://elsewhere.invalid",
            "repository": "Owner/project", "head_repository": "other/project",
            "head_ref": "refs/heads/another", "head_commit": "c" * 40,
            "base_ref": "refs/heads/another-base", "base_commit": "d" * 40,
        }
        self.assertEqual(set(movements), set(ds.DraftTarget.__dataclass_fields__))
        for name, value in movements.items():
            with self.subTest(field=name):
                reply = replace(good, target=replace(good.target, **{name: value}))
                inspector = mock.Mock(spec=["inspect_target"])
                inspector.inspect_target.return_value = reply
                result = tp.preflight_target(request, inspector=inspector)
                self.assertIs(result.status, tp.TargetPreflightStatus.REJECTED)
                self.assertEqual(result.reason, "target_mismatch")
        for name in ("draft_only", "atomic_head_base_preconditions"):
            with self.subTest(capability=name):
                inspector = mock.Mock(spec=["inspect_target"])
                inspector.inspect_target.return_value = replace(good, **{name: False})
                result = tp.preflight_target(request, inspector=inspector)
                self.assertIs(result.status, tp.TargetPreflightStatus.REJECTED)
                self.assertEqual(result.reason, "unsupported_capability")

    def test_malformed_misbound_snapshots_strict_capabilities_and_enum_identity(self):
        request = prepared()
        good = observed(request)

        class OtherStatus(Enum):
            OBSERVED = "observed"

        class Text(str):
            pass

        class ObservationSubclass(tp.TargetObservation):
            pass

        malformed = [None, {}, True, {"ready": True},
                     ObservationSubclass(good.status, good.target, good.request_snapshot_json, True, True)]
        for snapshot in (None, b"{}", "{}", request.binding_sha256,
                         request.snapshot_json + "\n", Text(request.snapshot_json)):
            malformed.append(replace(good, request_snapshot_json=snapshot))
        for status in ("observed", "ready", True, OtherStatus.OBSERVED, tp.TargetPreflightStatus.READY):
            malformed.append(replace(good, status=status))
        for field in ("draft_only", "atomic_head_base_preconditions"):
            for value in (1, 0, "true", None, [], object()):
                malformed.append(replace(good, **{field: value}))
        for name in ds.DraftTarget.__dataclass_fields__:
            malformed.append(replace(good, target=replace(good.target, **{name: 1})))
        malformed.extend([
            replace(good, target={}),
            replace(good, target=replace(good.target, head_ref="fix")),
            replace(good, target=replace(good.target, base_commit="A" * 40)),
        ])
        for index, reply in enumerate(malformed):
            with self.subTest(malformed=index):
                inspector = mock.Mock(spec=["inspect_target"])
                inspector.inspect_target.return_value = reply
                self.assertIs(tp.preflight_target(request, inspector=inspector).status,
                              tp.TargetPreflightStatus.UNKNOWN)
        # Same complete target, different valid proposal bytes: definite mismatch.
        for changed in (prepared("Different title"),):
            inspector = mock.Mock(spec=["inspect_target"])
            inspector.inspect_target.return_value = replace(good, request_snapshot_json=changed.snapshot_json)
            result = tp.preflight_target(request, inspector=inspector)
            self.assertIs(result.status, tp.TargetPreflightStatus.REJECTED)
            self.assertEqual(result.reason, "request_mismatch")

    def test_inspector_exceptions_missing_ambiguous_and_explicit_unknown(self):
        request = prepared()
        good = observed(request)
        for error in (RuntimeError("read failed"), ValueError("ambiguous"), TimeoutError("read deadline")):
            with self.subTest(exception=type(error)):
                inspector = mock.Mock(spec=["inspect_target"])
                inspector.inspect_target.side_effect = error
                self.assertIs(tp.preflight_target(request, inspector=inspector).status,
                              tp.TargetPreflightStatus.UNKNOWN)
                inspector.inspect_target.assert_called_once()

        class BrokenLookup:
            @property
            def inspect_target(self):
                raise RuntimeError("lookup failed")

        for inspector in (object(), BrokenLookup(), type("Missing", (), {"inspect_target": True})()):
            with self.subTest(inspector=type(inspector)):
                self.assertIs(tp.preflight_target(request, inspector=inspector).status,
                              tp.TargetPreflightStatus.UNKNOWN)
        inspector = mock.Mock(spec=["inspect_target"])
        inspector.inspect_target.return_value = replace(good, status=tp.ObservationStatus.UNKNOWN,
                                                       draft_only=False)
        result = tp.preflight_target(request, inspector=inspector)
        self.assertIs(result.status, tp.TargetPreflightStatus.UNKNOWN)
        self.assertEqual(result.reason, "observation_unknown")
        inspector.inspect_target.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            tp.preflight_target(request, inspector=inspector)

    def test_mutation_cannot_change_captured_request_bound_method_or_detached_observation_bytes(self):
        request = prepared()
        snapshot = request.snapshot_json
        reply = observed(request)
        alternate = prepared("Mutated proposal").snapshot_json
        calls = []
        lookups = []

        class MutatingInspector:
            @property
            def inspect_target(self):
                lookups.append("lookup")
                object.__setattr__(request, "snapshot_json", alternate)
                return self.observe

            def observe(self, passed):
                calls.append(passed)
                self.observe = lambda _: (_ for _ in ()).throw(AssertionError("rebound method called"))
                self_snapshot = passed.snapshot_json
                self_data = passed.data
                self_data["target"]["head_commit"] = "c" * 40
                object.__setattr__(passed, "snapshot_json", alternate)
                if self_snapshot != snapshot:
                    raise AssertionError("input snapshot was not captured before method lookup")
                return reply

        result = tp.preflight_target(request, inspector=MutatingInspector())
        self.assertIs(result.status, tp.TargetPreflightStatus.READY)
        self.assertEqual(lookups, ["lookup"])
        self.assertEqual(len(calls), 1)
        self.assertIsNot(calls[0], request)
        self.assertEqual(result.observation.request_snapshot_json.encode("utf-8"), snapshot.encode("utf-8"))
        original_target = result.observation.target
        object.__setattr__(reply.target, "head_commit", "d" * 40)
        object.__setattr__(reply, "request_snapshot_json", alternate)
        object.__setattr__(reply, "draft_only", False)
        object.__setattr__(reply, "target", ds.DraftTarget(**prepared().data["target"]))
        self.assertEqual(result.observation.target.head_commit, "b" * 40)
        self.assertIs(result.observation.target, original_target)
        self.assertEqual(result.observation.request_snapshot_json, snapshot)
        self.assertIs(result.observation.draft_only, True)
        # A misbound reply after mutation must not become matching by moving input.
        second = prepared()
        bad_reply = observed(prepared("Mutated proposal"))

        class MovingInput:
            def inspect_target(self, passed):
                object.__setattr__(second, "snapshot_json", alternate)
                object.__setattr__(passed, "snapshot_json", alternate)
                return bad_reply

        self.assertIs(tp.preflight_target(second, inspector=MovingInput()).status,
                      tp.TargetPreflightStatus.REJECTED)

    def test_invalid_request_precedes_inspector_and_zero_authority_journal_transport_effects(self):
        request = prepared()
        good = observed(request)
        lookups = []

        class UntouchedInspector:
            @property
            def inspect_target(self):
                lookups.append("unexpected")
                raise AssertionError("invalid request reached inspector")

        changed = request.data
        changed["target"]["base_commit"] = "c" * 40  # contradicts the real report
        wrong_base = json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        changed = request.data
        changed["body"] += "changed"
        wrong_body = json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        invalid = [None, object(), True, ds.DraftRequest("{}"), ds.DraftRequest(b"{}"),
                   ds.DraftRequest(request.snapshot_json + "\n"),
                   ds.DraftRequest(wrong_base), ds.DraftRequest(wrong_body)]
        with ExitStack() as stack:
            forbidden = [
                stack.enter_context(mock.patch.object(ds.DraftSubmission, "__init__", side_effect=AssertionError)),
                stack.enter_context(mock.patch.object(ds.DraftSubmission, "run", side_effect=AssertionError)),
                stack.enter_context(mock.patch.object(ds.HumanApprovalAuthority, "authorize_and_consume", side_effect=AssertionError)),
                stack.enter_context(mock.patch.object(ds.DraftOnlyTransport, "submit_draft", side_effect=AssertionError)),
                stack.enter_context(mock.patch.object(submission_journal.SQLiteSubmissionJournal, "__init__", side_effect=AssertionError)),
                stack.enter_context(mock.patch.object(submission_journal.SQLiteSubmissionJournal, "_claim", side_effect=AssertionError)),
                stack.enter_context(mock.patch.object(submission_journal.SQLiteSubmissionJournal, "_advance", side_effect=AssertionError)),
                stack.enter_context(mock.patch.object(submission_journal.SQLiteSubmissionJournal, "lookup", side_effect=AssertionError)),
            ]
            for value in invalid:
                with self.subTest(invalid=type(value)):
                    result = tp.preflight_target(value, inspector=UntouchedInspector())
                    self.assertIs(result.status, tp.TargetPreflightStatus.REJECTED)
                    self.assertEqual(result.reason, "invalid_request")
            self.assertEqual(lookups, [])
            inspector = mock.Mock(spec=["inspect_target"])
            for reply, status in (
                (good, tp.TargetPreflightStatus.READY),
                (replace(good, atomic_head_base_preconditions=False), tp.TargetPreflightStatus.REJECTED),
                (None, tp.TargetPreflightStatus.UNKNOWN),
            ):
                inspector.inspect_target.return_value = reply
                self.assertIs(tp.preflight_target(request, inspector=inspector).status, status)
            for boundary in forbidden:
                boundary.assert_not_called()
