"""Exactly six NEW self-contained unittest methods, authored NOT RUN here.

AttemptResult and source/command strings are inert synthetic evidence. No fixture
source, runner, model, process, network or historical test module is executed.
"""
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from src.agent.core import (
    AttemptResult, CheckEvidence, LoopState as S, ModelReceipt,
    TestAssessment, TestCommand, TestOutcome as O, TestPlan,
)
from src.review import draft_submission as ds
from src.review import package_selection as selection
from src.review import pr_builder, review_package as rp, submission_journal

BASE = "a" * 40
TARGET = ds.DraftTarget("https://github.com", "owner/project", "contributor/project",
                        "refs/heads/fix", "b" * 40, "refs/heads/main", BASE)
PATCH = "--- a/value.py\n+++ b/value.py\n@@ -1,1 +1,1 @@\n-VALUE = 1\n+VALUE = 2\n"
PAYLOADS = ("request.json", "report.json", "report.md", "body.md", "diff.patch")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def prepared(*, target=TARGET, title="Fix bytes 日本語", template="Template 日本語",
             issue_number=7, issue_title="Fix bytes"):
    def check(phase, command, outcome=O.PASS):
        failed = outcome is O.ASSERTION_FAILED
        return CheckEvidence(phase, command, int(failed), 0.001, "synthetic tail",
                             TestAssessment(outcome, 1, int(failed), "synthetic assertion"))

    attempt = AttemptResult(
        True, 1, diff=PATCH, state=S.SUCCEEDED, states=[S.READY, S.SUCCEEDED],
        checks=[check(S.BASELINE, "suite"),
                check(S.REPRODUCING, "reproduce", O.ASSERTION_FAILED),
                check(S.VERIFYING_REPRODUCTION, "reproduce"),
                check(S.VERIFYING_SUITE, "suite")],
        model_calls=1, tokens_used=7, known_tokens_used=7,
        model_receipts=[ModelReceipt("synthetic", "inert-fixture", 7)],
        cleanup_confirmed=True, sandbox_attempt_id="synthetic", elapsed_seconds=0.01,
    )
    report = pr_builder.build_review_report(
        attempt, {"repo": target.repository, "number": issue_number, "title": issue_title},
        base_commit=target.base_commit,
        plan=TestPlan(TestCommand("reproduce", 5), (TestCommand("suite", 5),)),
        execution_kind="synthetic",
    )
    return ds.prepare_draft_submission(report, target=target, title=title,
                                       pr_template=template)


def private_root(case):
    root = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, root, True)
    os.chmod(root, 0o700)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    case.addCleanup(os.close, fd)
    return root, fd


def rebind_public_hashes(path):
    """Recompute public consistency metadata without granting any authority."""
    payloads = {name: (path / name).read_bytes() for name in PAYLOADS}
    manifest = canonical({
        "schema_version": 1, "request_sha256": sha256(payloads["request.json"]).hexdigest(),
        "total_payload_bytes": sum(map(len, payloads.values())),
        "files": {name: {"size_bytes": len(data), "sha256": sha256(data).hexdigest()}
                  for name, data in payloads.items()},
    }).encode("utf-8")
    (path / "manifest.json").write_bytes(manifest)
    (path / "COMPLETE").write_text(canonical({
        "schema_version": 1, "manifest_sha256": sha256(manifest).hexdigest(),
    }), encoding="utf-8")


class PackageSelectionTests(unittest.TestCase):
    def test_actual_prepare_export_read_and_selection_return_exact_detached_request(self):
        root, fd = private_root(self)
        expected = prepared()
        receipt = rp.export_review_package(expected, parent_fd=fd, name="review")
        package = rp.read_review_package(parent_fd=fd, name="review")
        with mock.patch.object(selection, "read_review_package",
                               wraps=rp.read_review_package) as reader:
            selected = selection.load_selected_review_request(fd, "review", expected)
        reader.assert_called_once_with(parent_fd=fd, name="review")
        self.assertEqual(receipt.outcome, "complete")
        self.assertIs(type(selected), ds.DraftRequest)
        self.assertIsNot(selected, expected)
        self.assertEqual(selected.snapshot_json.encode("utf-8"),
                         (root / "review" / "request.json").read_bytes())
        self.assertEqual(selected.snapshot_json, package.snapshot_json)
        self.assertEqual(selected.data["body"], package.body)
        self.assertEqual(selected.data["report_json"], package.report_json)
        self.assertEqual(selected.data["target"], expected.data["target"])
        self.assertEqual(selected.data["pr_template"], "Template 日本語")

    def test_two_valid_packages_reject_wrong_selection_even_with_equal_public_hashes(self):
        _, fd = private_root(self)
        first, second = prepared(), prepared(issue_number=8)
        for name, request in (("first", first), ("second", second)):
            rp.export_review_package(request, parent_fd=fd, name=name)
            self.assertEqual(rp.read_review_package(parent_fd=fd, name=name).snapshot_json,
                             request.snapshot_json)
        self.assertNotEqual(first.snapshot_json, second.snapshot_json)
        with mock.patch.object(ds.DraftRequest, "binding_sha256",
                               new_callable=mock.PropertyMock, return_value="0" * 64):
            self.assertEqual(first.binding_sha256, second.binding_sha256)
            for name, correct, wrong in (("first", first, second), ("second", second, first)):
                with self.subTest(name=name):
                    self.assertEqual(selection.load_selected_review_request(
                        fd, name, correct).snapshot_json, correct.snapshot_json)
                    with self.assertRaises(rp.ReviewPackageError):
                        selection.load_selected_review_request(fd, name, wrong)

    def test_changed_exact_target_head_base_title_template_and_body_are_stale(self):
        _, fd = private_root(self)
        expected = prepared()
        rp.export_review_package(expected, parent_fd=fd, name="original")
        variants = {
            "repository": prepared(target=replace(TARGET, repository="Owner/project")),
            "head_repository": prepared(target=replace(TARGET, head_repository="other/project")),
            "head_ref": prepared(target=replace(TARGET, head_ref="refs/heads/other")),
            "head_commit": prepared(target=replace(TARGET, head_commit="c" * 40)),
            "base_ref": prepared(target=replace(TARGET, base_ref="refs/heads/stable")),
            "base_commit": prepared(target=replace(TARGET, base_commit="d" * 40)),
            "title": prepared(title=expected.data["title"] + " "),
            "template": prepared(template="Template 日本語 "),
            "body": prepared(issue_title="Changed issue and rendered body"),
        }
        for name, changed in variants.items():
            with self.subTest(field=name):
                rp.export_review_package(changed, parent_fd=fd, name=name)
                self.assertEqual(rp.read_review_package(parent_fd=fd, name=name).snapshot_json,
                                 changed.snapshot_json)  # individually valid, still wrong
                with self.assertRaises(rp.ReviewPackageError):
                    selection.load_selected_review_request(fd, name, expected)
                with self.assertRaises(rp.ReviewPackageError):
                    selection.load_selected_review_request(fd, "original", changed)

    def test_expected_mutation_during_actual_read_cannot_change_captured_request(self):
        _, fd = private_root(self)
        expected, replacement = prepared(), prepared(title="Later proposal")
        original = expected.snapshot_json
        rp.export_review_package(expected, parent_fd=fd, name="review")

        def mutate_then_read(**kwargs):
            # Deliberately bypass frozen assignment to exercise capture ordering.
            object.__setattr__(expected, "snapshot_json", replacement.snapshot_json)
            return rp.read_review_package(**kwargs)

        with mock.patch.object(selection, "read_review_package",
                               side_effect=mutate_then_read) as reader:
            selected = selection.load_selected_review_request(fd, "review", expected)
        reader.assert_called_once_with(parent_fd=fd, name="review")
        self.assertEqual(expected.snapshot_json, replacement.snapshot_json)
        self.assertEqual(selected.snapshot_json, original)
        self.assertIsNot(selected, expected)
        view = selected.data
        view["target"]["head_commit"] = "e" * 40
        view["body"] = "changed detached dictionary"
        self.assertEqual(selected.snapshot_json, original)
        self.assertEqual(selected.data["target"]["head_commit"], TARGET.head_commit)
        # A package matching only the later caller mutation must also reject.
        rp.export_review_package(replacement, parent_fd=fd, name="later")
        object.__setattr__(expected, "snapshot_json", original)
        with mock.patch.object(selection, "read_review_package", side_effect=mutate_then_read):
            with self.assertRaises(rp.ReviewPackageError):
                selection.load_selected_review_request(fd, "later", expected)

    def test_malformed_expected_precedes_io_and_incomplete_tampered_packages_reject(self):
        root, fd = private_root(self)
        expected = prepared()
        bad_body = expected.data
        bad_body["body"] += "tampered"
        bad_target = expected.data
        bad_target["target"]["destination"] = "https://example.invalid"
        forged_package = rp.LocalReviewPackage("fake", expected.snapshot_json, "", "", "", "", "")
        invalid = [None, forged_package, ds.DraftRequest("{}"), ds.DraftRequest("[]"),
                   ds.DraftRequest(None), ds.DraftRequest(expected.snapshot_json + "\n"),
                   ds.DraftRequest(canonical(bad_body)), ds.DraftRequest(canonical(bad_target)),
                   object.__new__(ds.DraftRequest)]
        with mock.patch.object(selection, "read_review_package",
                               side_effect=AssertionError("expected validation must precede I/O")) as reader:
            for request in invalid:
                with self.subTest(expected=type(request).__name__):
                    with self.assertRaises(rp.ReviewPackageError):
                        selection.load_selected_review_request(-1, "../invalid", request)
            reader.assert_not_called()

        for name in ("incomplete", "malformed", "tampered", "bad_request"):
            rp.export_review_package(expected, parent_fd=fd, name=name)
        (root / "incomplete" / "COMPLETE").unlink()
        (root / "malformed" / "manifest.json").write_bytes(b"{")
        (root / "tampered" / "body.md").write_text("coherently rehashed wrong body", encoding="utf-8")
        rebind_public_hashes(root / "tampered")
        (root / "bad_request" / "request.json").write_bytes(b"{}")
        rebind_public_hashes(root / "bad_request")
        with mock.patch.object(selection, "read_review_package",
                               wraps=rp.read_review_package) as reader:
            for name in ("incomplete", "malformed", "tampered", "bad_request"):
                with self.subTest(package=name):
                    with self.assertRaises(rp.ReviewPackageError):
                        selection.load_selected_review_request(fd, name, expected)
            self.assertEqual(reader.call_count, 4)

    def test_selection_success_and_failure_have_no_journal_authority_transport_effects(self):
        root, fd = private_root(self)
        expected, wrong = prepared(), prepared(title="Wrong selected proposal")
        rp.export_review_package(expected, parent_fd=fd, name="review")
        rp.export_review_package(wrong, parent_fd=fd, name="wrong")
        rp.export_review_package(expected, parent_fd=fd, name="partial")
        (root / "partial" / "COMPLETE").unlink()

        def contents():
            return {str(path.relative_to(root)): path.read_bytes()
                    for path in root.rglob("*") if path.is_file()}

        before = contents()
        boundaries = [
            (ds.DraftSubmission, "__init__"), (ds.DraftSubmission, "run"),
            (pr_builder, "submit_draft_pr"),
            (ds.HumanApprovalAuthority, "authorize_and_consume"),
            (ds.DraftOnlyTransport, "submit_draft"),
            (submission_journal.SQLiteSubmissionJournal, "__init__"),
            (submission_journal.SQLiteSubmissionJournal, "_transaction"),
            (submission_journal.SQLiteSubmissionJournal, "_claim"),
            (submission_journal.SQLiteSubmissionJournal, "_advance"),
        ]
        with ExitStack() as stack:
            spies = [stack.enter_context(mock.patch.object(
                owner, name, side_effect=AssertionError("selection entered " + name)))
                for owner, name in boundaries]
            selected = selection.load_selected_review_request(fd, "review", expected)
            self.assertEqual(selected.snapshot_json, expected.snapshot_json)
            for name, request in (("wrong", expected), ("partial", expected),
                                  ("review", ds.DraftRequest("{}"))):
                with self.subTest(package=name):
                    with self.assertRaises(rp.ReviewPackageError):
                        selection.load_selected_review_request(fd, name, request)
            for spy in spies:
                spy.assert_not_called()
        self.assertEqual(contents(), before)
        self.assertEqual({path.name for path in root.iterdir()}, {"review", "wrong", "partial"})
