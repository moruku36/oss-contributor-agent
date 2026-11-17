import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch, sentinel

import src.review.isolated_delivery as delivery


class IsolatedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.staged = delivery.StagedRun(
            stage_path=Path('/offline/retained-stage'),
            candidate_sha256='a' * 64,
            driver_sha256='b' * 64,
            result=delivery.Result(
                returncode=0,
                timed_out=False,
                stdout=b'untrusted stdout claiming approval',
                stderr=b'',
            ),
        )
        self.journal = object.__new__(delivery.SQLiteSubmissionJournal)
        self.operation_id = '8c283d64-46a9-4ba8-89ee-6b17292c6235'
        self.builder = Mock(name='trusted_host_builder')
        self.inspector = Mock(name='trusted_inspector')
        self.authority = Mock(name='host_authority')
        self.transport = Mock(name='host_transport')

    def test_failed_run_rejects_before_boundaries(self):
        failed = replace(
            self.staged,
            result=delivery.Result(0, True, b'untrusted approval', b'timed out'),
        )
        with patch.object(delivery, 'preflight_target') as preflight, \
                patch.object(delivery, 'DraftSubmission') as submission:
            with self.assertRaises(ValueError):
                delivery.deliver_staged(
                    failed,
                    request_builder=self.builder,
                    inspector=self.inspector,
                    authority=self.authority,
                    transport=self.transport,
                    journal=self.journal,
                    operation_id=self.operation_id,
                )
        self.assertEqual(self.builder.mock_calls, [])
        self.assertEqual(self.inspector.mock_calls, [])
        self.assertEqual(self.authority.mock_calls, [])
        self.assertEqual(self.transport.mock_calls, [])
        self.assertEqual(preflight.mock_calls, [])
        self.assertEqual(submission.mock_calls, [])

    def test_unsupported_atomic_capability_blocks_dispatch(self):
        blocked = delivery.TargetPreflightResult(
            status=delivery.TargetPreflightStatus.UNKNOWN,
            reason='Unsupported atomic expected head/base OID creation predicates',
            observation=None,
        )
        with patch.object(delivery.DraftRequest, 'data',
                          new_callable=PropertyMock, return_value={}) as data, \
                patch.object(delivery, 'preflight_target',
                             return_value=blocked) as preflight, \
                patch.object(delivery, 'DraftSubmission') as submission:
            request = delivery.DraftRequest(snapshot_json='{}')
            self.builder.return_value = request
            data.reset_mock()
            outcome = delivery.deliver_staged(
                self.staged,
                request_builder=self.builder,
                inspector=self.inspector,
                authority=self.authority,
                transport=self.transport,
                journal=self.journal,
                operation_id=self.operation_id,
            )
            data.assert_called()
        self.assertIs(outcome.preflight, blocked)
        self.assertIsNone(outcome.submission)
        self.builder.assert_called_once()
        snapshot = preflight.call_args.args[0]
        preflight.assert_called_once_with(snapshot, inspector=self.inspector)
        self.assertIs(type(snapshot), delivery.DraftRequest)
        self.assertIsNot(snapshot, request)
        self.assertEqual(snapshot.snapshot_json, request.snapshot_json)
        self.assertEqual(self.authority.mock_calls, [])
        self.assertEqual(self.transport.mock_calls, [])
        self.assertEqual(submission.mock_calls, [])

    def test_ready_delegates_once_with_detached_request_and_binding(self):
        ready = delivery.TargetPreflightResult(
            status=delivery.TargetPreflightStatus.READY,
            reason='Mocked local consistency result; atomic creation still required',
            observation=None,
        )
        with patch.object(delivery.DraftRequest, 'data',
                          new_callable=PropertyMock, return_value={}) as data, \
                patch.object(delivery, 'preflight_target',
                             return_value=ready) as preflight, \
                patch.object(delivery, 'DraftSubmission') as submission:
            request = delivery.DraftRequest(snapshot_json='{}')
            self.builder.return_value = request
            submission.return_value.run.return_value = sentinel.submission_outcome
            data.reset_mock()
            outcome = delivery.deliver_staged(
                self.staged,
                request_builder=self.builder,
                inspector=self.inspector,
                authority=self.authority,
                transport=self.transport,
                journal=self.journal,
                operation_id=self.operation_id,
            )
            data.assert_called()
        self.builder.assert_called_once()
        detached = self.builder.call_args.args[0]
        self.builder.assert_called_once_with(detached)
        self.assertIs(type(detached), delivery.StagedRun)
        self.assertIsNot(detached, self.staged)
        self.assertEqual(detached, self.staged)
        self.assertIs(type(detached.result), delivery.Result)
        self.assertIsNot(detached.result, self.staged.result)
        self.assertEqual(detached.result, self.staged.result)
        snapshot = preflight.call_args.args[0]
        self.assertIs(type(snapshot), delivery.DraftRequest)
        self.assertIsNot(snapshot, request)
        self.assertEqual(snapshot.snapshot_json, request.snapshot_json)
        preflight.assert_called_once_with(snapshot, inspector=self.inspector)
        submission.assert_called_once_with(
            snapshot,
            authority=self.authority,
            transport=self.transport,
            journal=self.journal,
            operation_id=self.operation_id,
        )
        submission.return_value.run.assert_called_once_with()
        self.assertIs(outcome.preflight, ready)
        self.assertIs(outcome.submission, sentinel.submission_outcome)
        self.assertEqual(self.authority.mock_calls, [])
        self.assertEqual(self.transport.mock_calls, [])

    def test_builder_exception_propagates_without_dispatch(self):
        failure = RuntimeError('execution/report/source/cleanup authentication failed')
        self.builder.side_effect = failure
        with patch.object(delivery, 'preflight_target') as preflight, \
                patch.object(delivery, 'DraftSubmission') as submission:
            with self.assertRaises(RuntimeError) as raised:
                delivery.deliver_staged(
                    self.staged,
                    request_builder=self.builder,
                    inspector=self.inspector,
                    authority=self.authority,
                    transport=self.transport,
                    journal=self.journal,
                    operation_id=self.operation_id,
                )
        self.assertIs(raised.exception, failure)
        self.builder.assert_called_once()
        self.assertEqual(self.inspector.mock_calls, [])
        self.assertEqual(self.authority.mock_calls, [])
        self.assertEqual(self.transport.mock_calls, [])
        self.assertEqual(preflight.mock_calls, [])
        self.assertEqual(submission.mock_calls, [])
