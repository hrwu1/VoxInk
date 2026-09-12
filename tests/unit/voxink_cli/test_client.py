from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from voxink_cli.client import GRANT_TOKEN_FILE_ENV, ControlClient, RequestContext
from voxink_cli.discovery import ControlEndpoint
from voxink_cli.envelope import ErrorCode, Failure


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _http_error(status: int, detail: str | None) -> HTTPError:
    body = json.dumps({"detail": detail}).encode() if detail is not None else b""
    return HTTPError(
        "http://127.0.0.1:8300/v1/models/active", status, "failed", {}, io.BytesIO(body)
    )


class ControlClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = ControlEndpoint("http://127.0.0.1:8300", "secret-value")
        self.context = RequestContext.create(request_id="req_test", timeout=4.0)
        self.client = ControlClient(self.endpoint, self.context)

    def _call(self, response: object, call: object = None):
        with patch("voxink_cli.client.urlopen") as urlopen:
            if isinstance(response, BaseException):
                urlopen.side_effect = response
            else:
                urlopen.return_value = response
            result = (call or self.client.models)()
        return result, urlopen.call_args

    def test_requests_carry_the_credential_and_the_request_identifier(self) -> None:
        _, call = self._call(_Response(b'{"models": []}'))

        request = call.args[0]
        self.assertEqual("Bearer secret-value", request.get_header("Authorization"))
        self.assertEqual("req_test", request.get_header("X-voxink-request-id"))
        self.assertEqual(4.0, call.kwargs["timeout"])

    def test_an_idempotency_key_is_sent_on_mutations_only(self) -> None:
        client = ControlClient(
            self.endpoint,
            RequestContext.create(request_id="req_test", idempotency_key="key-1"),
        )

        _, mutation = self._call(
            _Response(b'{"id": "m"}'), lambda: client.select_model("m")
        )
        _, read = self._call(_Response(b'{"models": []}'), client.models)

        self.assertEqual("key-1", mutation.args[0].get_header("X-voxink-idempotency-key"))
        self.assertIsNone(read.args[0].get_header("X-voxink-idempotency-key"))

    def test_an_endpoint_without_a_credential_sends_no_authorization(self) -> None:
        client = ControlClient(ControlEndpoint("http://127.0.0.1:8300", None), self.context)

        _, call = self._call(_Response(b"{}"), client.models)

        self.assertIsNone(call.args[0].get_header("Authorization"))

    def test_a_session_identifier_is_escaped_into_the_path(self) -> None:
        _, call = self._call(
            _Response(b"{}"), lambda: self.client.session("a b/../c")
        )

        self.assertIn("/v1/sessions/a%20b%2F..%2Fc", call.args[0].full_url)

    def test_query_parameters_are_encoded(self) -> None:
        _, call = self._call(
            _Response(b"{}"), lambda: self.client.capture_status(after=12)
        )

        self.assertTrue(call.args[0].full_url.endswith("/v1/ui/capture/status?after=12"))

    def test_service_statuses_become_public_error_codes(self) -> None:
        expected = {
            400: ErrorCode.INVALID_REQUEST,
            401: ErrorCode.PERMISSION_DENIED,
            404: ErrorCode.NOT_FOUND,
            413: ErrorCode.INVALID_REQUEST,
            422: ErrorCode.INVALID_REQUEST,
            503: ErrorCode.NOT_READY,
            418: ErrorCode.INTERNAL_ERROR,
        }
        for status, code in expected.items():
            with self.subTest(status=status):
                with self.assertRaises(Failure) as caught:
                    self._call(_http_error(status, "nope"))
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(status, caught.exception.details["http_status"])
                self.assertEqual("req_test", caught.exception.request_id)

    def test_a_busy_model_and_a_rejected_transition_are_told_apart(self) -> None:
        with self.assertRaises(Failure) as busy:
            self._call(_http_error(409, "stop the active recording before changing models"))
        with self.assertRaises(Failure) as transition:
            self._call(_http_error(409, "cannot stop a stopped session"))

        self.assertEqual(ErrorCode.MODEL_BUSY, busy.exception.code)
        self.assertEqual(ErrorCode.INVALID_TRANSITION, transition.exception.code)

    def test_an_error_without_a_json_body_still_carries_a_message(self) -> None:
        with self.assertRaises(Failure) as caught:
            self._call(_http_error(500, None))

        self.assertEqual(ErrorCode.INTERNAL_ERROR, caught.exception.code)
        self.assertTrue(caught.exception.message)

    def test_an_unreachable_helper_is_not_ready_rather_than_a_crash(self) -> None:
        with self.assertRaises(Failure) as caught:
            self._call(URLError("connection refused"))

        self.assertEqual(ErrorCode.NOT_READY, caught.exception.code)

    def test_a_slow_helper_reports_the_timeout_code(self) -> None:
        with self.assertRaises(Failure) as caught:
            self._call(TimeoutError())

        self.assertEqual(ErrorCode.TIMEOUT, caught.exception.code)
        self.assertIn("4s", caught.exception.message)

    def test_an_unreadable_response_is_an_internal_error(self) -> None:
        with self.assertRaises(Failure) as caught:
            self._call(_Response(b"<html>not json</html>"))

        self.assertEqual(ErrorCode.INTERNAL_ERROR, caught.exception.code)

    def test_an_empty_response_body_decodes_to_an_empty_result(self) -> None:
        result, _ = self._call(_Response(b""))

        self.assertEqual({}, result)

    def test_connecting_without_a_published_endpoint_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Failure) as caught:
                ControlClient.connect(
                    self.context, endpoint_path=Path(directory) / "absent.json"
                )

        self.assertEqual(ErrorCode.NOT_READY, caught.exception.code)

    def test_invalid_selected_token_files_never_select_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "grant"
            for payload in (b"", b" \n\t", b"\xff"):
                source.write_bytes(payload)
                for selected in (source, None):
                    with self.subTest(payload=payload, explicit=selected is not None):
                        with patch.dict(os.environ, {GRANT_TOKEN_FILE_ENV: str(source)}):
                            with patch("voxink_cli.client.load", return_value=self.endpoint):
                                with self.assertRaises(Failure) as caught:
                                    ControlClient.connect(self.context, token_file=selected)
                        self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)
                        self.assertEqual(caught.exception.request_id, self.context.request_id)
            source.unlink()
            with patch("voxink_cli.client.load", return_value=self.endpoint):
                with self.assertRaises(Failure) as caught:
                    ControlClient.connect(self.context, token_file=source)
            self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)

    def test_selected_nonempty_token_is_never_replaced_by_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "grant"
            # Validity belongs to the service: even a rejected token stays a
            # grant credential rather than becoming the more powerful owner.
            for token in ("vxg_test", "invalid-token"):
                source.write_text(token + "\n", encoding="utf-8")
                with patch("voxink_cli.client.load", return_value=self.endpoint):
                    client = ControlClient.connect(self.context, token_file=source)
                self.assertTrue(client.uses_grant)
                self.assertEqual(client.credential, token)
                with patch("voxink_cli.client.urlopen", side_effect=_http_error(401, "invalid credential")):
                    with self.assertRaises(Failure) as caught:
                        client.health()
                self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)

    def test_an_explicit_empty_token_never_falls_back_to_owner(self) -> None:
        client = ControlClient(self.endpoint, self.context, token="")
        _, call = self._call(_Response(b"{}"), client.health)
        self.assertIsNone(call.args[0].get_header("Authorization"))


class FileTranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.client = ControlClient(
            ControlEndpoint("http://127.0.0.1:8300", None),
            RequestContext.create(request_id="req_test"),
        )

    def test_the_upload_carries_the_file_and_its_form_fields(self) -> None:
        audio = self.root / "meeting notes.wav"
        audio.write_bytes(b"RIFF-audio")

        with patch("voxink_cli.client.urlopen") as urlopen:
            urlopen.return_value = _Response(b'{"event": {}}')
            self.client.transcribe_file(
                audio, session_id="s1", source="microphone", language="en"
            )

        request = urlopen.call_args.args[0]
        body = request.data.decode("utf-8", errors="replace")
        self.assertIn("multipart/form-data; boundary=", request.get_header("Content-type"))
        self.assertIn('name="session_id"\r\n\r\ns1', body)
        self.assertIn('name="language"\r\n\r\nen', body)
        self.assertIn('filename="meeting notes.wav"', body)
        self.assertIn("Content-Type: audio/x-wav", body)
        self.assertIn("RIFF-audio", body)

    def test_optional_fields_are_omitted_when_unset(self) -> None:
        audio = self.root / "clip.wav"
        audio.write_bytes(b"RIFF")

        with patch("voxink_cli.client.urlopen") as urlopen:
            urlopen.return_value = _Response(b'{"event": {}}')
            self.client.transcribe_file(
                audio, session_id=None, source="system", language=None
            )

        body = urlopen.call_args.args[0].data.decode("utf-8", errors="replace")
        self.assertNotIn('name="session_id"', body)
        self.assertNotIn('name="language"', body)
        self.assertIn('name="source"\r\n\r\nsystem', body)

    def test_a_missing_file_is_reported_before_any_request(self) -> None:
        with patch("voxink_cli.client.urlopen") as urlopen:
            with self.assertRaises(Failure) as caught:
                self.client.transcribe_file(
                    self.root / "absent.wav", session_id=None, source="microphone", language=None
                )

        self.assertEqual(ErrorCode.NOT_FOUND, caught.exception.code)
        urlopen.assert_not_called()

    def test_an_empty_file_is_rejected_before_any_request(self) -> None:
        audio = self.root / "empty.wav"
        audio.write_bytes(b"")

        with patch("voxink_cli.client.urlopen") as urlopen:
            with self.assertRaises(Failure) as caught:
                self.client.transcribe_file(
                    audio, session_id=None, source="microphone", language=None
                )

        self.assertEqual(ErrorCode.INVALID_REQUEST, caught.exception.code)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
