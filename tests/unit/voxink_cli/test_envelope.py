from __future__ import annotations

import json
import unittest

from voxink_cli.envelope import (
    EXIT_CODES,
    EXIT_INTERNAL_ERROR,
    EXIT_NOT_READY,
    SCHEMA_VERSION,
    ErrorCode,
    Failure,
    dumps,
    exit_code_for,
    failure,
    success,
)


class EnvelopeTests(unittest.TestCase):
    def test_success_envelope_matches_the_published_shape(self) -> None:
        envelope = success("capture.start", {"session_id": "s1"}, warnings=["careful"])

        self.assertEqual(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "command": "capture.start",
                "data": {"session_id": "s1"},
                "warnings": ["careful"],
            },
            envelope,
        )

    def test_success_without_data_still_carries_an_object(self) -> None:
        self.assertEqual({}, success("status")["data"])

    def test_failure_envelope_matches_the_published_shape(self) -> None:
        error = Failure(
            ErrorCode.APPROVAL_REQUIRED, "needs authorization", request_id="req_1"
        )

        envelope = failure("capture.start", error)

        self.assertFalse(envelope["ok"])
        self.assertEqual(
            {
                "code": "approval_required",
                "message": "needs authorization",
                "retryable": True,
                "request_id": "req_1",
                "details": {},
            },
            envelope["error"],
        )

    def test_failure_takes_the_request_id_from_the_call_when_the_error_has_none(self) -> None:
        envelope = failure(
            "status", Failure(ErrorCode.NOT_READY, "no helper"), request_id="req_2"
        )

        self.assertEqual("req_2", envelope["error"]["request_id"])

    def test_every_taxonomy_code_has_a_documented_exit_code(self) -> None:
        codes = {
            value
            for name, value in vars(ErrorCode).items()
            if name.isupper() and isinstance(value, str)
        }

        self.assertEqual(codes, set(EXIT_CODES))
        self.assertTrue(all(0 < code < 128 for code in EXIT_CODES.values()))

    def test_an_unknown_code_is_an_internal_error_rather_than_a_success(self) -> None:
        self.assertEqual(EXIT_INTERNAL_ERROR, exit_code_for("something_new"))

    def test_failure_reports_its_own_exit_code_and_retryability(self) -> None:
        error = Failure(ErrorCode.NOT_READY, "no helper")

        self.assertEqual(EXIT_NOT_READY, error.exit_code)
        self.assertTrue(error.retryable)
        self.assertFalse(Failure(ErrorCode.INVALID_REQUEST, "bad").retryable)

    def test_dumps_writes_one_line_so_jsonl_needs_no_other_writer(self) -> None:
        line = dumps(success("transcripts.show", {"text": "héllo\nworld"}))

        self.assertNotIn("\n", line)
        self.assertEqual("héllo\nworld", json.loads(line)["data"]["text"])


if __name__ == "__main__":
    unittest.main()
