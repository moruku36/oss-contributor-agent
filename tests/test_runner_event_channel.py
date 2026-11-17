"""Six self-contained offline checks; no transport, execution or old test helpers."""
from __future__ import annotations

from asyncio import CancelledError
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from src.agent.filesystem_staging import StageReceipt, StagedFile
from src.agent.runner_event_channel import (
    EventChannelBackend, EventChannelError, MAX_DISPATCHES, MAX_OUTPUT_BYTES,
    MAX_WIRE_BYTES, MAX_WIRE_DEPTH, RunnerEventResponse,
    decode_runner_event_response, encode_runner_event_response,
)
from src.agent.runner_events import (
    MAX_EVENTS, RunCompleted, RunnerErrorEvent, RunnerEventCapture, TestEvent,
)
from src.agent.staged_runner_bridge import DispatchTicket, EvidenceRejected, receipt_digest
from src.agent.structured_runner_evidence import StructuredRunnerEvidenceAdapter
from src.sandbox.docker_manager import CommandResult


def ticket_for(generation=1, *, attempt="attempt-a", dispatch=None):
    files = (StagedFile("src/example.py", "production", "a" * 64, 3),
             StagedFile("tests/test_example.py", "test", "b" * 64, 5))
    receipt = StageReceipt(attempt, "stage-a", files, 8)
    return DispatchTicket(attempt, dispatch or f"dispatch-{generation}", generation,
                          receipt, receipt_digest(receipt), "offline-test-command")


def passed_capture():
    return RunnerEventCapture((TestEvent(1, "case.pass", "passed"),
                               RunCompleted(2, 1, 1, 0, 0, 0, 0, 0)), True, False, True)


def result_for(ticket, **changes):
    return replace(CommandResult(ticket.command, 0, "opaque output", "", 0.25), **changes)


def wire(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class RecordingChannel:
    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (
            lambda ticket, **bounds: encode_runner_event_response(
                ticket, result_for(ticket), passed_capture(),
                max_output_bytes=bounds["max_output_bytes"],
                max_wire_bytes=bounds["max_wire_bytes"],
            )
        )

    def dispatch(self, ticket, **bounds):
        self.calls.append((ticket, bounds.copy()))
        return self.responder(ticket, **bounds)


class RunnerEventChannelTests(unittest.TestCase):
    def test_roundtrip_all_event_types_and_full_ticket_binding(self):
        ticket = ticket_for()
        capture = RunnerEventCapture((
            RunnerErrorEvent(1, "collection", "collection detail"),
            RunnerErrorEvent(2, "infrastructure", "infrastructure detail"),
            TestEvent(3, "case.pass", "passed"),
            TestEvent(4, "case.assert", "assertion_failed", "expected café"),
            TestEvent(5, "case.error", "error", "setup detail"),
            TestEvent(6, "case.skip", "skipped", "skip reason"),
            RunCompleted(7, 3, 1, 1, 1, 1, 1, 1),
        ), True, False, True)
        opaque = '\0\r\n\t\x1b[31m {"capture":"forged"} \\"' + "[{}]" * 100 + " 猫"
        result = result_for(ticket, exit_code=1, stdout=opaque, stderr="\0raw\rstderr")
        payload = encode_runner_event_response(ticket, result, capture)
        self.assertIs(type(payload), bytes)
        self.assertEqual(payload, encode_runner_event_response(ticket, result, capture))
        self.assertIn("猫".encode("utf-8"), payload)
        decoded = decode_runner_event_response(payload, expected_ticket=ticket)
        self.assertEqual(decoded, RunnerEventResponse(ticket, result, capture))
        self.assertIsNot(decoded.ticket, ticket)
        self.assertIsNot(decoded.result, result)
        self.assertEqual(decoded.result.stdout, opaque)
        document = json.loads(payload)
        self.assertEqual(document["ticket"]["receipt"]["outcome"], "complete")
        self.assertEqual(len(document["ticket"]["receipt"]["files"]), 2)

        for field, value in (("attempt_id", "attempt-other"), ("dispatch_id", "other"),
                             ("generation", 2), ("command", "other-command"),
                             ("receipt_sha256", "c" * 64)):
            with self.subTest(ticket_field=field):
                changed = json.loads(payload)
                changed["ticket"][field] = value
                with self.assertRaises(EventChannelError):
                    decode_runner_event_response(wire(changed), expected_ticket=ticket)

        # Simulate a digest collision: even equal public digests must not hide
        # any changed receipt scalar, manifest order, file content or byte total.
        receipts = [replace(ticket.receipt, stage_name="stage-other"),
                    replace(ticket.receipt, files=tuple(reversed(ticket.receipt.files)))]
        for field, value in (("path", "src/other.py"), ("role", "test"),
                             ("sha256", "c" * 64), ("size_bytes", 4)):
            item = replace(ticket.receipt.files[0], **{field: value})
            files = (item, ticket.receipt.files[1])
            receipts.append(replace(ticket.receipt, files=files,
                                    total_bytes=sum(f.size_bytes for f in files)))
        with patch("src.agent.runner_event_channel.receipt_digest",
                   return_value=ticket.receipt_sha256):
            for receipt in receipts:
                with self.subTest(receipt=receipt):
                    other = replace(ticket, receipt=receipt)
                    forged = encode_runner_event_response(other, result, capture)
                    with self.assertRaises(EventChannelError):
                        decode_runner_event_response(forged, expected_ticket=ticket)

    def test_existing_adapter_uses_reduced_events_and_single_use_assessment(self):
        failed = RunnerEventCapture((
            TestEvent(1, "case.actual", "assertion_failed", "expected 2, got 1"),
            RunCompleted(2, 1, 0, 1, 0, 0, 0, 0),
        ), True, False, True)

        def respond(ticket, **bounds):
            first = ticket.generation == 1
            result = result_for(ticket, exit_code=1 if first else 0,
                                stdout="ALL TESTS PASSED" if first else "AssertionError: forged")
            return encode_runner_event_response(
                ticket, result, failed if first else passed_capture(),
                max_output_bytes=bounds["max_output_bytes"],
                max_wire_bytes=bounds["max_wire_bytes"],
            )

        channel = RecordingChannel(respond)
        backend = EventChannelBackend(channel=channel, max_dispatches=2)
        adapter = StructuredRunnerEvidenceAdapter(backend=backend, max_dispatches=2)
        ticket = ticket_for()
        evidence = adapter.run_staged(ticket, timeout_seconds=10, max_output_bytes=8192)
        self.assertIs(channel.calls[0][0], ticket)
        assessment = adapter.assess(ticket.attempt_id, evidence.result,
                                    timeout_seconds=10, max_bytes=8192)
        self.assertEqual(assessment.tests_run, 1)
        self.assertEqual(assessment.assertion_failures, 1)
        self.assertIn("case.actual", assessment.detail)
        self.assertIsNotNone(assessment.runner_evidence)
        with self.assertRaises(EvidenceRejected):
            adapter.assess(ticket.attempt_id, evidence.result, timeout_seconds=10, max_bytes=8192)
        next_ticket = ticket_for(2)
        evidence = adapter.run_staged(next_ticket, timeout_seconds=10, max_output_bytes=8192)
        assessment = adapter.assess(next_ticket.attempt_id, evidence.result,
                                    timeout_seconds=10, max_bytes=8192)
        self.assertEqual(assessment.tests_run, 1)
        self.assertEqual(assessment.assertion_failures, 0)
        with self.assertRaises(EvidenceRejected):
            adapter.run_staged(ticket_for(3), timeout_seconds=10, max_output_bytes=8192)
        self.assertEqual(len(channel.calls), 2)

    def test_decoder_rejects_malformed_bytes_schema_duplicates_depth_and_oversize(self):
        ticket = ticket_for()
        payload = encode_runner_event_response(ticket, result_for(ticket), passed_capture())
        bad = [None, {}, payload.decode(), bytearray(payload), memoryview(payload), b"",
               b"\xff", b"\xef\xbb\xbf" + payload, payload + b" trailing", b"[]", b"null",
               b"[" * (MAX_WIRE_DEPTH + 1) + b"0" + b"]" * (MAX_WIRE_DEPTH + 1)]
        for old, new in (
            (b'"version":1', b'"version":1,"version":1'),
            (b'"version":1', b'"version":1,"ver\\u0073ion":1'),
            (b'"outcome":"complete"', b'"outcome":"complete","outcome":"complete"'),
            (b'"sequence":1', b'"sequence":1,"sequence":1'),
        ):
            self.assertIn(old, payload)
            bad.append(payload.replace(old, new, 1))

        def changed(path, value=None, *, remove=False):
            doc = json.loads(payload)
            parent = doc
            for key in path[:-1]:
                parent = parent[key]
            if remove:
                del parent[path[-1]]
            else:
                parent[path[-1]] = value
            return wire(doc)

        for path in ((), ("ticket",), ("ticket", "receipt"),
                     ("ticket", "receipt", "files", 0), ("result",),
                     ("capture",), ("capture", "events", 0)):
            bad.append(changed((*path, "unexpected"), "value"))
        for path in (("version",), ("ticket", "receipt"), ("result", "stderr"),
                     ("capture", "complete"), ("capture", "events", 0, "detail")):
            bad.append(changed(path, remove=True))
        for path in (("version",), ("ticket", "generation"),
                     ("ticket", "receipt", "total_bytes"),
                     ("ticket", "receipt", "files", 0, "size_bytes"),
                     ("result", "exit_code"), ("capture", "events", 0, "sequence"),
                     ("capture", "events", 1, "passed")):
            for value in (True, 1.0, "1"):
                bad.append(changed(path, value))
        for path, values in (
            (("version",), (0, 2)),
            (("ticket", "receipt", "outcome"), ("partial", False)),
            (("ticket", "receipt", "files"), ([], {})),
            (("result", "stdout"), (None, [], 3)),
            (("result", "duration_seconds"), (True, "0.25", -1, 2701)),
            (("capture", "complete"), (1, "true")),
            (("capture", "events"), ([], {}, [dict(type="unknown")])),
            (("capture", "events", 0, "outcome"), ("unknown", 1)),
            (("capture", "events", 0, "sequence"), (0, 2)),
            (("capture", "events", 1, "tests_run"), (0, 2)),
        ):
            for value in values:
                bad.append(changed(path, value))
        bad.append(changed(("capture", "events"), [{}] * (MAX_EVENTS + 1)))
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e999", b"1e-999",
                      b"0.25000000000000000001", b"0e999999999999999999999999999"):
            bad.append(payload.replace(b'"duration_seconds":0.25',
                                       b'"duration_seconds":' + token, 1))
        # Escaped lone surrogates are valid JSON syntax but not UTF-8 text values.
        bad.append(payload.replace(b'"stdout":"opaque output"', b'"stdout":"\\ud800"'))
        for index, value in enumerate(bad):
            with self.subTest(index=index):
                with self.assertRaises(EventChannelError):
                    decode_runner_event_response(value, expected_ticket=ticket)
        with patch("src.agent.runner_event_channel.json.loads") as loads:
            with self.assertRaises(EventChannelError):
                decode_runner_event_response(b"\xff" * 33, expected_ticket=ticket,
                                             max_wire_bytes=32)
            loads.assert_not_called()
        with self.assertRaises(EventChannelError):
            decode_runner_event_response(payload, expected_ticket=ticket,
                                         max_wire_bytes=len(payload) - 1)
        self.assertEqual(decode_runner_event_response(
            payload, expected_ticket=ticket, max_wire_bytes=len(payload)).ticket, ticket)

    def test_replay_unknown_cancellation_and_malformed_responses_are_conservative(self):
        ticket = ticket_for()
        channel = RecordingChannel()
        backend = EventChannelBackend(channel=channel, max_dispatches=2)
        backend.run_structured(ticket, timeout_seconds=10, max_output_bytes=1024)
        for invalid in (ticket, ticket_for(2, attempt="attempt-other"), ticket_for(3),
                        ticket_for(2, dispatch=ticket.dispatch_id)):
            with self.assertRaises(EventChannelError):
                backend.run_structured(invalid, timeout_seconds=10, max_output_bytes=1024)
        self.assertEqual(len(channel.calls), 1)
        backend.run_structured(ticket_for(2), timeout_seconds=10, max_output_bytes=1024)
        with self.assertRaises(EventChannelError):
            backend.run_structured(ticket_for(3), timeout_seconds=10, max_output_bytes=1024)
        self.assertEqual(len(channel.calls), 2)

        old_payload = encode_runner_event_response(ticket, result_for(ticket), passed_capture())
        replay = RecordingChannel(lambda *args, **kwargs: old_payload)
        backend = EventChannelBackend(channel=replay)
        backend.run_structured(ticket, timeout_seconds=10, max_output_bytes=1024)
        for _ in range(2):
            with self.assertRaises(EventChannelError):
                backend.run_structured(ticket_for(2), timeout_seconds=10, max_output_bytes=1024)
        self.assertEqual(len(replay.calls), 2)

        for flags in (dict(complete=False), dict(truncated=True), dict(outcome_known=False)):
            capture = replace(passed_capture(), **flags)
            payload = encode_runner_event_response(ticket, result_for(ticket), capture)
            self.assertEqual(decode_runner_event_response(
                payload, expected_ticket=ticket).capture, capture)
            channel = RecordingChannel(lambda *args, **kwargs: payload)
            backend = EventChannelBackend(channel=channel)
            for _ in range(2):
                with self.assertRaises(EventChannelError):
                    backend.run_structured(ticket, timeout_seconds=10, max_output_bytes=1024)
            self.assertEqual(len(channel.calls), 1)

        for failure in (RuntimeError("uncertain"), KeyboardInterrupt(), CancelledError()):
            def raise_failure(*args, **kwargs):
                raise failure
            channel = RecordingChannel(raise_failure)
            backend = EventChannelBackend(channel=channel)
            with self.assertRaises(type(failure)):
                backend.run_structured(ticket, timeout_seconds=10, max_output_bytes=1024)
            with self.assertRaises(EventChannelError):
                backend.run_structured(ticket, timeout_seconds=10, max_output_bytes=1024)
            self.assertEqual(len(channel.calls), 1)
        for malformed in (old_payload.decode(), b"{}", bytearray(old_payload)):
            channel = RecordingChannel(lambda *args, **kwargs: malformed)
            backend = EventChannelBackend(channel=channel)
            for _ in range(2):
                with self.assertRaises(EventChannelError):
                    backend.run_structured(ticket, timeout_seconds=10, max_output_bytes=1024)
            self.assertEqual(len(channel.calls), 1)

    def test_bound_forwarding_and_local_deadline_output_and_wire_enforcement(self):
        ticket = ticket_for()
        result = result_for(ticket, stdout="é", stderr="猫")  # combined five UTF-8 bytes
        payload = encode_runner_event_response(ticket, result, passed_capture())
        channel = RecordingChannel(lambda *args, **kwargs: payload)
        backend = EventChannelBackend(channel=channel, max_wire_bytes=len(payload))
        with patch("src.agent.runner_event_channel.monotonic", side_effect=[100.0, 104.49]):
            run = backend.run_structured(ticket, timeout_seconds=4.5, max_output_bytes=5)
        self.assertEqual(run.result.stdout, "é")
        self.assertEqual(channel.calls, [(ticket, dict(timeout_seconds=4.5,
                              max_output_bytes=5, max_wire_bytes=len(payload)))])
        self.assertIs(channel.calls[0][0], ticket)

        for options in (dict(max_output_bytes=4), dict(max_wire_bytes=len(payload) - 1),
                        dict(late=True), dict(duration=2)):
            late = options.get("late", False)
            response = payload if "duration" not in options else encode_runner_event_response(
                ticket, replace(result, duration_seconds=options["duration"]), passed_capture())
            channel = RecordingChannel(lambda *args, **kwargs: response)
            backend = EventChannelBackend(channel=channel,
                                           max_wire_bytes=options.get("max_wire_bytes", MAX_WIRE_BYTES))
            expected_error = TimeoutError if late else EventChannelError
            with patch("src.agent.runner_event_channel.monotonic",
                       side_effect=[10.0, 12.0] if late else [10.0, 10.5]):
                with self.assertRaises(expected_error):
                    backend.run_structured(ticket, timeout_seconds=2,
                                           max_output_bytes=options.get("max_output_bytes", 5))
            with self.assertRaises(EventChannelError):
                backend.run_structured(ticket, timeout_seconds=2, max_output_bytes=5)
            self.assertEqual(len(channel.calls), 1)

        channel = RecordingChannel()
        backend = EventChannelBackend(channel=channel)
        for timeout in (True, 0, -1, float("nan"), float("inf"), 2701, "10"):
            with self.assertRaises(EventChannelError):
                backend.run_structured(ticket, timeout_seconds=timeout, max_output_bytes=1024)
        for cap in (True, 0, -1, MAX_OUTPUT_BYTES + 1, 1.0, "10"):
            with self.assertRaises(EventChannelError):
                backend.run_structured(ticket, timeout_seconds=10, max_output_bytes=cap)
        self.assertEqual(channel.calls, [])
        for cap in (True, 0, MAX_WIRE_BYTES + 1):
            with self.assertRaises(EventChannelError):
                EventChannelBackend(channel=channel, max_wire_bytes=cap)
        for limit in (True, 0, MAX_DISPATCHES + 1):
            with self.assertRaises(EventChannelError):
                EventChannelBackend(channel=channel, max_dispatches=limit)
        with self.assertRaises(EventChannelError):
            EventChannelBackend(channel=None)

        # A trusted dependency still may fail its contract: detect ticket
        # mutation against the pre-call full scalar snapshot and poison backend.
        mutable_ticket = ticket_for()
        def mutate(*args, **kwargs):
            object.__setattr__(mutable_ticket, "command", "changed-command")
            return payload
        channel = RecordingChannel(mutate)
        backend = EventChannelBackend(channel=channel)
        for _ in range(2):
            with self.assertRaises(EventChannelError):
                backend.run_structured(mutable_ticket, timeout_seconds=10, max_output_bytes=1024)
        self.assertEqual(len(channel.calls), 1)

    def test_encoder_rejects_invalid_exact_objects_without_coercion_or_truncation(self):
        ticket = ticket_for()
        result = result_for(ticket)
        capture = passed_capture()
        bad_results = [None, object()]
        for field, values in (
            ("command", ("other-command", 1)),
            ("exit_code", (True, 1.0, "1", 2**31)),
            ("duration_seconds", (True, -1, float("nan"), float("inf"), "0.25")),
            ("stdout", (b"bytes", "\ud800", "x" * (MAX_OUTPUT_BYTES + 1))),
            ("stderr", (None,)),
        ):
            bad_results.extend(replace(result, **{field: value}) for value in values)
        for value in bad_results:
            with self.assertRaises(EventChannelError):
                encode_runner_event_response(ticket, value, capture)
        for value in (None, replace(ticket, generation=True), replace(ticket, receipt=object()),
                      replace(ticket, receipt=replace(ticket.receipt, total_bytes=True)),
                      replace(ticket, receipt=replace(ticket.receipt, files=list(ticket.receipt.files))),
                      replace(ticket, receipt=replace(ticket.receipt, files=(replace(
                          ticket.receipt.files[0], size_bytes=True), ticket.receipt.files[1])))):
            with self.assertRaises(EventChannelError):
                encode_runner_event_response(value, result, capture)
        for value in (
            None, replace(capture, events=list(capture.events)), replace(capture, complete=1),
            replace(capture, events=(object(),)),
            replace(capture, events=(TestEvent(True, "case.pass", "passed"), capture.events[1])),
            replace(capture, events=(TestEvent(1, "case.pass", "passed", "detail"), capture.events[1])),
            replace(capture, events=(TestEvent(1, "case.pass", "unknown"), capture.events[1])),
            replace(capture, events=(TestEvent(1, "case.pass", "assertion_failed", ""), capture.events[1])),
            replace(capture, events=(capture.events[0],)),
            replace(capture, events=(capture.events[0], RunCompleted(2, 2, 2, 0, 0, 0, 0, 0))),
            replace(capture, events=(capture.events[0], TestEvent(2, "case.pass", "skipped"),
                                    RunCompleted(3, 1, 1, 0, 0, 1, 0, 0))),
        ):
            with self.assertRaises(EventChannelError):
                encode_runner_event_response(ticket, result, value)
        payload = encode_runner_event_response(ticket, result, capture)
        with self.assertRaises(EventChannelError):
            encode_runner_event_response(ticket, result, capture, max_wire_bytes=len(payload) - 1)
        with self.assertRaises(EventChannelError):
            encode_runner_event_response(ticket, result, capture,
                                         max_output_bytes=len(result.stdout.encode("utf-8")) - 1)
        self.assertEqual(result.stdout, "opaque output")
        self.assertEqual(capture, passed_capture())


if __name__ == "__main__":
    unittest.main()
