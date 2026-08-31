"""End-to-end tests for the public `voxink` command.

The CLI is exercised against a real loopback helper rather than a stubbed
client, so these tests cover the published contract from argument parsing
through the control API to the envelope a consumer reads.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import signal
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import uvicorn

from services.transcription.app import PROTOCOL_VERSION, ServiceSettings, create_app
from voxink_cli import discovery
from voxink_cli.cli import main
from voxink_cli.envelope import (
    EXIT_CONFLICT,
    EXIT_INVALID_REQUEST,
    EXIT_NOT_FOUND,
    EXIT_NOT_READY,
    EXIT_OK,
    SCHEMA_VERSION,
    ErrorCode,
)

from tests.integration.test_transcription_service import FakeProvider, _write_model_package


AUTH_SECRET = "public-cli-secret"


class _LiveHelper:
    """Run one application on a loopback port for the lifetime of a test."""

    def __init__(self, app: object) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self.port = int(self._socket.getsockname()[1])
        self._server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="on")
        )
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 10.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if time.monotonic() >= deadline:
                raise AssertionError("the local helper did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._socket.close()


def _write_wav(path: Path, *, milliseconds: int = 200, sample_rate: int = 16_000) -> Path:
    frames = sample_rate * milliseconds // 1000
    data = b"".join(struct.pack("<h", 1000 if index % 32 < 16 else -1000) for index in range(frames))
    path.write_bytes(
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    return path


class PublicCommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.output_root = self.root / "transcripts"
        self.output_root.mkdir()
        self.models_root = self.root / "models"
        self.models_root.mkdir()
        self.model_path = _write_model_package(self.models_root, "Qwen3-ASR-1.7B")
        _write_model_package(self.models_root, "Qwen3-ASR-0.6B")
        self.native_capture = self.root / "voxink-capture"
        self.native_capture.touch()
        self.native_capture.chmod(0o755)
        self.endpoint_path = self.root / "control-endpoint.json"

        self.provider = FakeProvider()
        app = create_app(
            ServiceSettings(
                model_path=self.model_path,
                models_root=self.models_root,
                output_root=self.output_root,
                auth_secret=AUTH_SECRET,
                native_capture_path=self.native_capture,
            ),
            provider=self.provider,
            provider_factory=lambda _: FakeProvider(),
        )
        self.helper = _LiveHelper(app)
        self.helper.start()
        self.addCleanup(self.helper.stop)
        discovery.publish(
            self.endpoint_path,
            base_url=self.helper.base_url,
            auth_secret=AUTH_SECRET,
            protocol_version=str(PROTOCOL_VERSION),
        )

    # -- helpers -----------------------------------------------------------

    def _run(self, *argv: str) -> tuple[int, list[dict], str]:
        out, err = io.StringIO(), io.StringIO()
        code = main(
            [*argv, "--format", "json", "--endpoint-file", str(self.endpoint_path)],
            stdout=out,
            stderr=err,
        )
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line]
        return code, lines, err.getvalue()

    def _ok(self, *argv: str) -> dict:
        code, envelopes, stderr = self._run(*argv)
        self.assertEqual(EXIT_OK, code, f"{argv} failed: {envelopes} {stderr}")
        self.assertEqual(1, len(envelopes))
        envelope = envelopes[0]
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(SCHEMA_VERSION, envelope["schema_version"])
        return envelope

    def _error(self, *argv: str) -> tuple[int, dict]:
        code, envelopes, _ = self._run(*argv)
        self.assertEqual(1, len(envelopes), envelopes)
        self.assertFalse(envelopes[0]["ok"], envelopes[0])
        return code, envelopes[0]["error"]

    # -- discovery ---------------------------------------------------------

    def test_capabilities_reports_versions_features_and_grant_scopes(self) -> None:
        envelope = self._ok("capabilities")

        data = envelope["data"]
        self.assertEqual("capabilities", envelope["command"])
        self.assertEqual(PROTOCOL_VERSION, data["protocol_version"])
        self.assertEqual(SCHEMA_VERSION, data["schema_version"])
        self.assertEqual("local-only", data["privacy_mode"])
        self.assertIn("system", data["capture_modes"])
        self.assertTrue(data["features"]["capture.system"]["available"])
        self.assertFalse(data["features"]["durable_session_registry"]["supported"])
        self.assertIn("capture.microphone", data["grant_scopes"])
        self.assertEqual("developer-credential", data["grant_enforcement"])

    def test_capabilities_declares_that_grants_are_not_enforced_yet(self) -> None:
        warnings = " ".join(self._ok("capabilities")["data"] and self._ok("capabilities")["warnings"])

        self.assertIn("Scoped grants are not enforced", warnings)

    def test_status_reports_readiness_and_capture_state(self) -> None:
        data = self._ok("status")["data"]

        self.assertTrue(data["ready"])
        self.assertEqual("ready", data["capture_state"])
        self.assertEqual(self.helper.base_url, data["endpoint"])

    def test_doctor_reports_a_check_for_every_local_prerequisite(self) -> None:
        data = self._ok("doctor")["data"]

        names = {check["name"] for check in data["checks"]}
        self.assertEqual(
            {"helper", "native_capture", "models", "vocabulary", "session_registry"}, names
        )
        self.assertTrue(next(c for c in data["checks"] if c["name"] == "helper")["ok"])
        self.assertFalse(
            next(c for c in data["checks"] if c["name"] == "session_registry")["ok"]
        )
        self.assertEqual(1, data["needs_attention"])

    def test_devices_answers_even_where_no_audio_backend_is_installed(self) -> None:
        envelope = self._ok("devices", "list")

        self.assertIsInstance(envelope["data"]["devices"], list)

    def test_applications_lists_the_native_catalog(self) -> None:
        listed = MagicMock(returncode=0, stdout="com.apple.Safari\tSafari\n", stderr="")
        with patch("services.transcription.app.subprocess.run", return_value=listed):
            data = self._ok("applications", "list")["data"]

        self.assertEqual([{"bundle_id": "com.apple.Safari", "name": "Safari"}], data["applications"])

    # -- models and vocabulary --------------------------------------------

    def test_models_are_listed_and_selected(self) -> None:
        listed = self._ok("models", "list")["data"]
        self.assertEqual(
            {"Qwen3-ASR-1.7B", "Qwen3-ASR-0.6B"}, {model["id"] for model in listed["models"]}
        )
        self.assertEqual("Qwen3-ASR-1.7B", listed["active_model"])

        selected = self._ok("models", "select", "Qwen3-ASR-0.6B")["data"]

        self.assertEqual("Qwen3-ASR-0.6B", selected["id"])
        self.assertTrue(selected["active"])
        self.assertEqual("Qwen3-ASR-0.6B", self._ok("models", "list")["data"]["active_model"])

    def test_selecting_an_uninstalled_model_is_not_found(self) -> None:
        code, error = self._error("models", "select", "Qwen3-ASR-42B")

        self.assertEqual(EXIT_NOT_FOUND, code)
        self.assertEqual(ErrorCode.NOT_FOUND, error["code"])
        self.assertFalse(error["retryable"])

    def test_vocabulary_is_read_and_replaced_from_a_file(self) -> None:
        source = self.root / "vocabulary.txt"
        source.write_text("VoxInk\nvox ink => VoxInk\n", encoding="utf-8")

        replaced = self._ok("vocabulary", "replace", "--file", str(source))["data"]

        self.assertEqual(["VoxInk"], replaced["terms"])
        self.assertEqual([{"misheard": "vox ink", "correct": "VoxInk"}], replaced["corrections"])
        self.assertEqual(replaced["content"], self._ok("vocabulary", "show")["data"]["content"])

    def test_replacing_the_vocabulary_from_stdin_is_refused_under_no_input(self) -> None:
        code, error = self._error("vocabulary", "replace", "--no-input")

        self.assertEqual(EXIT_INVALID_REQUEST, code)
        self.assertEqual(ErrorCode.INVALID_REQUEST, error["code"])

    def test_replacing_the_vocabulary_from_a_missing_file_is_not_found(self) -> None:
        code, error = self._error("vocabulary", "replace", "--file", str(self.root / "absent"))

        self.assertEqual(EXIT_NOT_FOUND, code)
        self.assertEqual(ErrorCode.NOT_FOUND, error["code"])

    # -- file transcription and artifacts ---------------------------------

    def test_a_local_file_is_transcribed_into_a_discoverable_transcript(self) -> None:
        audio = _write_wav(self.root / "clip.wav")

        event = self._ok("transcribe", str(audio), "--language", "en")["data"]["event"]

        self.assertEqual("Local transcription works.", event["text"])
        self.assertTrue(event["is_final"])

        listed = self._ok("transcripts", "list")["data"]
        self.assertEqual(1, len(listed["transcripts"]))
        artifact = listed["transcripts"][0]
        self.assertEqual(1, artifact["part_count"])
        self.assertFalse(artifact["interrupted"])

        shown = self._ok("transcripts", "show", artifact["session_id"])["data"]
        self.assertIn("Local transcription works.", shown["text"])
        self.assertNotIn("schema_version", shown["text"])
        self.assertEqual(artifact["session_id"], shown["metadata"]["session_id"])

    def test_listing_transcripts_reports_what_they_cost_on_disk(self) -> None:
        """Retention is on by default, so its size has to be answerable."""
        self._ok("transcribe", str(_write_wav(self.root / "clip.wav")))

        listed = self._ok("transcripts", "list")["data"]
        artifact = listed["transcripts"][0]
        self.assertIn("audio_bytes", artifact)
        self.assertEqual(
            artifact["total_bytes"], artifact["size_bytes"] + artifact["audio_bytes"]
        )
        self.assertEqual(
            listed["retained_bytes"],
            sum(item["total_bytes"] for item in listed["transcripts"]),
        )

    def test_transcribing_a_missing_file_never_reaches_the_helper(self) -> None:
        code, error = self._error("transcribe", str(self.root / "absent.wav"))

        self.assertEqual(EXIT_NOT_FOUND, code)
        self.assertEqual(0, self.provider.calls)

    def test_reading_an_unknown_transcript_is_not_found(self) -> None:
        code, error = self._error("transcripts", "show", "2026-01-01_00-00-00")

        self.assertEqual(EXIT_NOT_FOUND, code)
        self.assertEqual(ErrorCode.NOT_FOUND, error["code"])

    # -- sessions ----------------------------------------------------------

    def test_sessions_warns_that_only_open_sessions_are_listed(self) -> None:
        envelope = self._ok("sessions", "list")

        self.assertFalse(envelope["data"]["durable_registry"])
        self.assertIn("voxink transcripts list", " ".join(envelope["warnings"]))

    def test_an_unknown_session_is_not_found(self) -> None:
        code, error = self._error("sessions", "show", "no-such-session")

        self.assertEqual(EXIT_NOT_FOUND, code)
        self.assertEqual(ErrorCode.NOT_FOUND, error["code"])

    # -- capture lifecycle -------------------------------------------------

    def _fake_capture_process(self) -> MagicMock:
        process = MagicMock()
        process.pid = 4321
        process.returncode = 0
        process.stderr = None
        process.poll.return_value = None
        return process

    def test_the_capture_lifecycle_runs_through_the_control_api(self) -> None:
        process = self._fake_capture_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process) as spawned:
            started = self._ok(
                "capture", "start", "--source", "microphone", "--source", "system"
            )["data"]
            self.assertEqual("recording", started["state"])
            self.assertTrue(started["session_id"])
            command = spawned.call_args.args[0]
            self.assertIn("--mic", command)
            self.assertIn("--system-all", command)
            self.assertIn("--retain-audio", command)

            self.assertEqual("recording", self._ok("capture", "status")["data"]["state"])
            self.assertEqual("paused", self._ok("capture", "pause")["data"]["state"])
            self.assertEqual("recording", self._ok("capture", "resume")["data"]["state"])

            stopped = self._ok("capture", "stop", "--wait")["data"]
            process.send_signal.assert_called_with(signal.SIGINT)

        self.assertEqual("ready", stopped["state"])

    def test_selected_application_capture_passes_the_bundle_identifiers(self) -> None:
        process = self._fake_capture_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process) as spawned:
            self._ok(
                "capture",
                "start",
                "--source",
                "applications",
                "--app",
                "com.apple.Safari",
                "--no-retain-audio",
            )
            command = spawned.call_args.args[0]

        self.assertIn("com.apple.Safari", command)
        self.assertNotIn("--system-all", command)
        self.assertNotIn("--retain-audio", command)

    def test_microphone_only_capture_starts_without_a_system_stream(self) -> None:
        process = self._fake_capture_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process) as spawned:
            started = self._ok("capture", "start", "--source", "microphone")["data"]
            command = spawned.call_args.args[0]

        self.assertEqual("recording", started["state"])
        self.assertEqual(["microphone"], started["sources"])
        self.assertIn("--mic", command)
        self.assertNotIn("--system-all", command)
        self.assertNotIn("--app", command)

    def test_system_only_capture_asks_for_no_microphone(self) -> None:
        process = self._fake_capture_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process) as spawned:
            self._ok("capture", "start", "--source", "system")
            command = spawned.call_args.args[0]

        self.assertIn("--system-all", command)
        self.assertNotIn("--mic", command)

    def test_selected_applications_without_a_bundle_identifier_is_invalid(self) -> None:
        code, error = self._error("capture", "start", "--source", "applications")

        self.assertEqual(EXIT_INVALID_REQUEST, code)
        self.assertEqual(ErrorCode.INVALID_REQUEST, error["code"])

    def test_controlling_capture_that_is_not_running_is_a_rejected_transition(self) -> None:
        code, error = self._error("capture", "pause")

        self.assertEqual(EXIT_CONFLICT, code)
        self.assertEqual(ErrorCode.INVALID_TRANSITION, error["code"])

    def test_watch_streams_one_envelope_per_event_and_ends_with_the_state(self) -> None:
        process = self._fake_capture_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process):
            self._ok("capture", "start", "--source", "system")
            process.poll.return_value = 0
            code, envelopes, _ = self._run("capture", "watch")

        self.assertEqual(EXIT_OK, code)
        self.assertTrue(envelopes)
        self.assertTrue(all(envelope["ok"] for envelope in envelopes))
        self.assertEqual("ready", envelopes[-1]["data"]["state"])

    # -- contract ----------------------------------------------------------

    def test_text_failures_go_to_stderr_and_leave_stdout_empty(self) -> None:
        out, err = io.StringIO(), io.StringIO()

        code = main(
            ["sessions", "show", "no-such-session", "--endpoint-file", str(self.endpoint_path)],
            stdout=out,
            stderr=err,
        )

        self.assertEqual(EXIT_NOT_FOUND, code)
        self.assertEqual("", out.getvalue())
        self.assertIn("not_found", err.getvalue())

    def test_text_warnings_go_to_stderr_and_leave_the_result_on_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()

        main(
            ["sessions", "list", "--endpoint-file", str(self.endpoint_path)],
            stdout=out,
            stderr=err,
        )

        self.assertIn("warning:", err.getvalue())
        self.assertNotIn("warning:", out.getvalue())

    def test_a_global_option_is_accepted_before_or_after_the_command(self) -> None:
        out = io.StringIO()

        main(
            ["--format", "json", "status", "--endpoint-file", str(self.endpoint_path)],
            stdout=out,
            stderr=io.StringIO(),
        )

        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_a_revoked_endpoint_reports_not_ready_rather_than_a_traceback(self) -> None:
        discovery.revoke(self.endpoint_path)

        code, error = self._error("status")

        self.assertEqual(EXIT_NOT_READY, code)
        self.assertEqual(ErrorCode.NOT_READY, error["code"])
        self.assertTrue(error["retryable"])

    def test_a_stopped_helper_reports_not_ready_rather_than_a_traceback(self) -> None:
        self.helper.stop()

        code, error = self._error("status")

        self.assertEqual(EXIT_NOT_READY, code)
        self.assertEqual(ErrorCode.NOT_READY, error["code"])

    def test_every_result_carries_a_request_identifier_for_the_audit_log(self) -> None:
        _, error = self._error("sessions", "show", "no-such-session", "--request-id", "req_mine")

        self.assertEqual("req_mine", error["request_id"])


if __name__ == "__main__":
    unittest.main()
