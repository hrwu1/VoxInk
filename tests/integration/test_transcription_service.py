from __future__ import annotations

import sys
import hashlib
import json
import tempfile
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
import signal
import subprocess
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clients.python_capture.vad import EnergyVadChunker, VadConfig
from services.transcription.app import (
    ServiceSettings,
    _finalize_audio,
    _native_capture_view,
    _read_native_error,
    _shutdown,
    _start_native_stderr_reader,
    _StreamContext,
    create_app,
)
from voxink_core.models import SessionConfig, Source, TranscriptEvent
from voxink_core.local_models import MODEL_MANIFEST
from voxink_core.persistence.audio import AudioRetentionWriter


class FakeProvider:
    name = "fake-local"

    def __init__(self) -> None:
        self.is_loaded = False
        self.calls = 0

    async def load(self) -> None:
        self.is_loaded = True

    async def transcribe_file(self, audio, options):
        self.calls += 1
        self.is_loaded = True
        assert Path(audio).is_file()
        return TranscriptEvent(
            session_id=options.session_id,
            segment_id=options.segment_id,
            stream_id=options.stream_id,
            source=options.source,
            start_ms=options.start_ms,
            end_ms=options.end_ms,
            language=options.language or "en",
            text="Local transcription works.",
            is_final=True,
        )

    async def transcribe_stream(self, frames, options):
        raise NotImplementedError
        yield

    async def unload(self) -> None:
        self.is_loaded = False


class ScriptedProvider(FakeProvider):
    """Return prepared phrases so overlap-induced repetition is observable."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__()
        self.texts = list(texts)

    async def transcribe_file(self, audio, options):
        event = await super().transcribe_file(audio, options)
        text = self.texts.pop(0) if self.texts else ""
        return replace(event, text=text)


def _write_model_package(root: Path, model_id: str) -> Path:
    path = root / model_id
    path.mkdir(parents=True)
    payload = model_id.encode("utf-8")
    (path / "weights.bin").write_bytes(payload)
    (path / MODEL_MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": model_id,
                "display_name": model_id.replace("-", " "),
                "profile": "Efficient" if "0.6B" in model_id else "High accuracy",
                "description": "Test local model",
                "provider": "fake-local",
                "files": [
                    {
                        "path": "weights.bin",
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _start_message(
    session_id: str,
    source: str,
    *,
    sources: list[str] | None = None,
    vad: dict | None = None,
    language: str | None = None,
) -> dict:
    message = {
        "type": "stream.start",
        "schema_version": 1,
        "session_id": session_id,
        "session_sources": sources or [source],
        "stream_id": f"{source}-1",
        "source": source,
        "audio": {
            "encoding": "pcm_s16le",
            "sample_rate_hz": 16000,
            "channels": 1,
            "frame_duration_ms": 20,
        },
        "vad": vad
        or {
            "threshold_rms": 500,
            "min_speech_duration_ms": 20,
            "silence_duration_ms": 40,
            "pre_roll_ms": 0,
            "overlap_ms": 0,
        },
    }
    if language is not None:
        message["language"] = language
    return message


VOICED = int(1000).to_bytes(2, "little", signed=True) * 320
SILENCE = bytes(640)


def _send_frame(websocket, source: str, pcm: bytes, start_ms: int) -> dict:
    websocket.send_json(
        {
            "type": "audio.frame",
            "sequence": start_ms // 20,
            "stream_id": f"{source}-1",
            "start_ms": start_ms,
            "duration_ms": 20,
            "byte_length": len(pcm),
        }
    )
    websocket.send_bytes(pcm)
    return websocket.receive_json()


def _utterance(websocket, source: str, start_ms: int) -> list[dict]:
    """Send one voiced frame plus enough silence to finalize it."""
    responses = []
    for offset, pcm in enumerate((VOICED, SILENCE, SILENCE)):
        responses.append(_send_frame(websocket, source, pcm, start_ms + offset * 20))
    return responses


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "outputs"
        self.provider = FakeProvider()
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=self.provider
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_health_is_lightweight_and_local_only(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual("local-only", response.json()["privacy_mode"])
        self.assertFalse(response.json()["model_loaded"])
        self.assertEqual(0, self.provider.calls)
        self.assertEqual(1, response.json()["protocol_version"])

    def test_authenticated_service_rejects_missing_and_wrong_credentials(self) -> None:
        app = create_app(
            ServiceSettings(output_root=self.output_root, auth_secret="correct-secret"),
            provider=self.provider,
        )
        with TestClient(app) as client:
            self.assertEqual(200, client.get("/health").status_code)
            self.assertEqual(401, client.get("/v1/providers").status_code)
            self.assertEqual(401, client.get("/v1/models").status_code)
            self.assertEqual(
                401,
                client.get(
                    "/v1/providers", headers={"Authorization": "Bearer wrong-secret"}
                ).status_code,
            )
            response = client.get(
                "/v1/providers", headers={"Authorization": "Bearer correct-secret"}
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual(
                200,
                client.get(
                    "/v1/models", headers={"Authorization": "Bearer correct-secret"}
                ).status_code,
            )


    def test_authenticated_websocket_rejects_missing_credential(self) -> None:
        app = create_app(
            ServiceSettings(output_root=self.output_root, auth_secret="correct-secret"),
            provider=self.provider,
        )
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/v1/sessions/private/stream"):
                    pass

            with client.websocket_connect(
                "/v1/sessions/private/stream",
                headers={"Authorization": "Bearer correct-secret"},
            ) as websocket:
                websocket.send_json(_start_message("private", "microphone"))
                self.assertEqual("stream.ready", websocket.receive_json()["type"])

            protocol = "voxink.auth.correct-secret"
            with client.websocket_connect(
                "/v1/sessions/browser-private/stream",
                subprotocols=[protocol],
            ) as websocket:
                self.assertEqual(protocol, websocket.accepted_subprotocol)
                websocket.send_json(_start_message("browser-private", "microphone"))
                self.assertEqual("stream.ready", websocket.receive_json()["type"])

    def test_ui_bootstrap_exchanges_once_for_launch_credential(self) -> None:
        app = create_app(
            ServiceSettings(
                output_root=self.output_root,
                auth_secret="launch-secret",
                ui_bootstrap_secret="one-time-bootstrap",
            ),
            provider=self.provider,
        )
        with TestClient(app) as client:
            self.assertEqual(401, client.post("/v1/ui/bootstrap").status_code)
            response = client.post(
                "/v1/ui/bootstrap",
                headers={"X-VoxInk-Bootstrap": "one-time-bootstrap"},
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual({"token": "launch-secret"}, response.json())
            self.assertEqual(
                401,
                client.post(
                    "/v1/ui/bootstrap",
                    headers={"X-VoxInk-Bootstrap": "one-time-bootstrap"},
                ).status_code,
            )

    def test_native_capture_ui_builds_selected_application_command_and_controls_it(self) -> None:
        executable = Path(self.temporary.name) / "voxink-capture"
        executable.touch()
        executable.chmod(0o755)
        process = MagicMock()
        process.pid = 1234
        process.returncode = 0
        process.stderr = None
        process.poll.return_value = None
        app = create_app(
            ServiceSettings(
                output_root=self.output_root,
                auth_secret="ui-secret",
                native_capture_path=executable,
            ),
            provider=self.provider,
        )
        headers = {"Authorization": "Bearer ui-secret"}
        with patch("services.transcription.app.subprocess.Popen", return_value=process) as spawned:
            with TestClient(app) as client:
                capabilities = client.get("/v1/ui/capture/capabilities", headers=headers)
                self.assertTrue(capabilities.json()["native_capture_available"])
                started = client.post(
                    "/v1/ui/capture/start",
                    headers=headers,
                    json={
                        "sources": ["microphone", "system"],
                        "applications": ["com.apple.Safari"],
                        "retain_audio": True,
                    },
                )
                self.assertEqual(200, started.status_code, started.text)
                command = spawned.call_args.args[0]
                self.assertIn("--mic", command)
                self.assertIn("--app", command)
                self.assertIn("com.apple.Safari", command)
                self.assertIn("--retain-audio", command)
                self.assertIn("--no-captions", command)
                self.assertNotIn("--system-all", command)

                self.assertEqual(200, client.post("/v1/ui/capture/pause", headers=headers).status_code)
                process.send_signal.assert_called_with(signal.SIGUSR1)
                self.assertEqual(200, client.post("/v1/ui/capture/resume", headers=headers).status_code)
                process.send_signal.assert_called_with(signal.SIGUSR2)
                stopped = client.post("/v1/ui/capture/stop", headers=headers)
                self.assertEqual(200, stopped.status_code, stopped.text)
                self.assertEqual("ready", stopped.json()["state"])
                process.send_signal.assert_called_with(signal.SIGINT)

    def test_native_application_catalog_is_parsed_for_the_ui(self) -> None:
        executable = Path(self.temporary.name) / "voxink-capture"
        executable.touch()
        executable.chmod(0o755)
        app = create_app(
            ServiceSettings(output_root=self.output_root, native_capture_path=executable),
            provider=self.provider,
        )
        listed = subprocess.CompletedProcess(
            [str(executable), "--list-apps"],
            0,
            "com.apple.Safari\tSafari\tpid=42\ncom.apple.Safari\tSafari Helper\tpid=43\ncom.google.Chrome\tGoogle Chrome\tpid=84\n",
            "",
        )
        with patch("services.transcription.app.subprocess.run", return_value=listed):
            with TestClient(app) as client:
                response = client.get("/v1/ui/capture/applications")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            [
                {"bundle_id": "com.apple.Safari", "name": "Safari"},
                {"bundle_id": "com.google.Chrome", "name": "Google Chrome"},
            ],
            response.json()["applications"],
        )

    def test_manual_session_lifecycle(self) -> None:
        created = self.client.post(
            "/v1/sessions",
            json={"session_id": "manual", "sources": ["microphone", "system"]},
        )
        self.assertEqual(201, created.status_code)
        self.assertEqual("created", created.json()["state"])
        for action, expected in (
            ("start", "running"),
            ("pause", "paused"),
            ("resume", "running"),
            ("stop", "stopped"),
        ):
            response = self.client.post(f"/v1/sessions/manual/{action}")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(expected, response.json()["state"])

    def test_reading_the_native_error_twice_does_not_raise_on_empty_stderr(self) -> None:
        """A helper that exits cleanly with no stderr is read once per status poll."""
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=self.provider
        )
        runtime = app.state.runtime
        process = subprocess.Popen(
            [sys.executable, "-c", ""], stderr=subprocess.PIPE, text=True
        )
        process.wait(timeout=10)
        _read_native_error(runtime, process)
        self.assertIsNone(runtime.native_error)
        # The UI polls /status every 500 ms, so this runs again on a closed stream.
        _read_native_error(runtime, process)
        self.assertIsNone(runtime.native_error)

    def test_native_error_keeps_the_first_detail_across_repeated_reads(self) -> None:
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=self.provider
        )
        runtime = app.state.runtime
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stderr.write('capture failed\\n')"],
            stderr=subprocess.PIPE,
            text=True,
        )
        process.wait(timeout=10)
        _read_native_error(runtime, process)
        self.assertEqual("capture failed", runtime.native_error)
        _read_native_error(runtime, process)
        self.assertEqual("capture failed", runtime.native_error)

    def test_shutdown_finalizes_sessions_even_when_native_capture_stop_raises(self) -> None:
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=self.provider
        )
        runtime = app.state.runtime
        with TestClient(app) as client:
            client.post("/v1/sessions", json={"session_id": "open", "sources": ["microphone"]})
            client.post("/v1/sessions/open/start")
            self.assertEqual("running", runtime.sessions.get("open").state.value)
            with patch(
                "services.transcription.app._stop_native_capture",
                side_effect=ValueError("read of closed file"),
            ):
                _shutdown(runtime)
        self.assertEqual("stopped", runtime.sessions.get("open").state.value)

    def test_native_helper_stderr_is_drained_so_a_flood_cannot_wedge_capture(self) -> None:
        """The Swift CLI warns per dropped frame; an undrained pipe blocks it."""
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=self.provider
        )
        runtime = app.state.runtime
        script = (
            "import sys\n"
            "for index in range(20000):\n"
            "    sys.stderr.write(f'dropped frame {index}\\n')\n"
            "sys.stderr.flush()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _start_native_stderr_reader(runtime, process)
        try:
            process.wait(timeout=15)
        finally:
            _read_native_error(runtime, process)
        self.assertEqual(0, process.returncode)
        self.assertEqual("dropped frame 19999", runtime.native_error)

    def test_the_ui_event_feed_is_bounded_and_keeps_cursors_absolute(self) -> None:
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=self.provider
        )
        runtime = app.state.runtime
        runtime.native_session_id = "watched"
        runtime.ui_events["watched"] = deque(maxlen=3)
        runtime.ui_events_total["watched"] = 0
        for index in range(5):
            runtime.ui_events["watched"].append({"text": str(index)})
            runtime.ui_events_total["watched"] += 1

        view = _native_capture_view(runtime, after=0)
        # The two oldest were dropped, but the cursor still counts every event.
        self.assertEqual(["2", "3", "4"], [event["text"] for event in view["events"]])
        self.assertEqual(5, view["next_cursor"])
        caught_up = _native_capture_view(runtime, after=view["next_cursor"])
        self.assertEqual([], caught_up["events"])
        self.assertEqual(5, caught_up["next_cursor"])

    def test_a_session_the_ui_is_not_watching_records_no_events(self) -> None:
        with self.client.websocket_connect("/v1/sessions/live/stream") as websocket:
            websocket.send_json(
                {
                    "type": "stream.start",
                    "schema_version": 1,
                    "session_id": "live",
                    "stream_id": "microphone-1",
                    "source": "microphone",
                    "audio": {
                        "encoding": "pcm_s16le",
                        "sample_rate_hz": 16000,
                        "channels": 1,
                    },
                }
            )
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            pcm = bytes(3200)
            websocket.send_json(
                {
                    "type": "audio.chunk",
                    "sequence": 0,
                    "stream_id": "microphone-1",
                    "start_ms": 0,
                    "end_ms": 100,
                    "byte_length": len(pcm),
                    "finalization_reason": "silence",
                }
            )
            websocket.send_bytes(pcm)
            self.assertEqual("transcript.final", websocket.receive_json()["type"])
        # Nothing polls this session, so nothing may be retained for it.
        self.assertEqual({}, self.client.app.state.runtime.ui_events)

    def test_batch_transcription_creates_durable_markdown(self) -> None:
        response = self.client.post(
            "/v1/transcriptions",
            data={
                "session_id": "batch",
                "source": "system",
                "stream_id": "system-1",
                "language": "en",
                "start_ms": "100",
                "end_ms": "900",
            },
            files={"file": ("sample.wav", b"fake-wave-data", "audio/wav")},
        )
        self.assertEqual(200, response.status_code, response.text)
        event = response.json()["event"]
        self.assertEqual("system", event["source"])
        self.assertEqual("Local transcription works.", event["text"])
        markdown = next((self.output_root / "batch").glob("transcript-part-*.md"))
        content = markdown.read_text()
        self.assertIn("[SYSTEM]", content)
        self.assertIn("Local transcription works.", content)

    def test_vad_chunk_websocket_round_trip(self) -> None:
        with self.client.websocket_connect("/v1/sessions/live/stream") as websocket:
            websocket.send_json(
                {
                    "type": "stream.start",
                    "schema_version": 1,
                    "session_id": "live",
                    "stream_id": "microphone-1",
                    "source": "microphone",
                    "audio": {
                        "encoding": "pcm_s16le",
                        "sample_rate_hz": 16000,
                        "channels": 1,
                    },
                }
            )
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            pcm = bytes(3200)
            websocket.send_json(
                {
                    "type": "audio.chunk",
                    "sequence": 0,
                    "stream_id": "microphone-1",
                    "start_ms": 0,
                    "end_ms": 100,
                    "byte_length": len(pcm),
                    "finalization_reason": "silence",
                }
            )
            websocket.send_bytes(pcm)
            result = websocket.receive_json()
            self.assertEqual("transcript.final", result["type"])
            self.assertEqual("microphone", result["event"]["source"])
            websocket.send_json(
                {"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 100}
            )
            self.assertEqual("stream.stopped", websocket.receive_json()["type"])

    def test_continuous_frames_are_segmented_by_server_vad(self) -> None:
        with self.client.websocket_connect("/v1/sessions/frames/stream") as websocket:
            websocket.send_json(
                {
                    "type": "stream.start",
                    "schema_version": 1,
                    "session_id": "frames",
                    "stream_id": "system-1",
                    "source": "system",
                    "audio": {
                        "encoding": "pcm_s16le",
                        "sample_rate_hz": 16000,
                        "channels": 1,
                        "frame_duration_ms": 20,
                    },
                    "vad": {
                        "threshold_rms": 500,
                        "min_speech_duration_ms": 20,
                        "silence_duration_ms": 40,
                        "pre_roll_ms": 0,
                        "overlap_ms": 0,
                    },
                }
            )
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            voiced = int(1000).to_bytes(2, "little", signed=True) * 320
            silence = bytes(640)
            responses = []
            for sequence, pcm in enumerate((voiced, silence, silence)):
                websocket.send_json(
                    {
                        "type": "audio.frame",
                        "sequence": sequence,
                        "stream_id": "system-1",
                        "start_ms": sequence * 20,
                        "duration_ms": 20,
                        "byte_length": len(pcm),
                    }
                )
                websocket.send_bytes(pcm)
                responses.append(websocket.receive_json())
            self.assertEqual("transcript.final", responses[-1]["type"])
            self.assertEqual("system", responses[-1]["event"]["source"])
            websocket.send_json(
                {"type": "stream.stop", "stream_id": "system-1", "at_ms": 60}
            )
            self.assertEqual("stream.stopped", websocket.receive_json()["type"])

    def test_optional_audio_is_saved_and_combined_on_stop(self) -> None:
        created = self.client.post(
            "/v1/sessions",
            json={
                "session_id": "recorded",
                "sources": ["system"],
                "audio_saved": True,
                "save_separate_audio": True,
                "save_combined_audio": True,
            },
        )
        self.assertEqual(201, created.status_code)
        with self.client.websocket_connect("/v1/sessions/recorded/stream") as websocket:
            websocket.send_json(
                {
                    "type": "stream.start",
                    "schema_version": 1,
                    "session_id": "recorded",
                    "session_sources": ["system"],
                    "stream_id": "system-1",
                    "source": "system",
                    "audio": {
                        "encoding": "pcm_s16le",
                        "sample_rate_hz": 16000,
                        "channels": 1,
                    },
                }
            )
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            pcm = int(123).to_bytes(2, "little", signed=True) * 320
            websocket.send_json(
                {
                    "type": "audio.chunk",
                    "sequence": 0,
                    "stream_id": "system-1",
                    "source": "system",
                    "start_ms": 0,
                    "end_ms": 20,
                    "byte_length": len(pcm),
                }
            )
            websocket.send_bytes(pcm)
            self.assertEqual("transcript.final", websocket.receive_json()["type"])
            websocket.send_json(
                {"type": "stream.stop", "stream_id": "system-1", "at_ms": 20}
            )
            self.assertEqual("stream.stopped", websocket.receive_json()["type"])
        session_dir = self.output_root / "recorded"
        self.assertTrue((session_dir / "system-part-001.wav").is_file())
        self.assertTrue((session_dir / "combined-part-001.wav").is_file())

    def test_shared_session_stops_only_after_both_source_streams(self) -> None:
        def start_message(source: str) -> dict:
            return {
                "type": "stream.start",
                "schema_version": 1,
                "session_id": "dual",
                "session_sources": ["microphone", "system"],
                "stream_id": f"{source}-1",
                "source": source,
                "audio": {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                },
            }

        pcm = bytes(640)
        with self.client.websocket_connect("/v1/sessions/dual/stream") as mic:
            mic.send_json(start_message("microphone"))
            self.assertEqual("stream.ready", mic.receive_json()["type"])
            with self.client.websocket_connect("/v1/sessions/dual/stream") as system:
                system.send_json(start_message("system"))
                self.assertEqual("stream.ready", system.receive_json()["type"])
                mic.send_json(
                    {"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 20}
                )
                self.assertEqual("stream.stopped", mic.receive_json()["type"])
                self.assertEqual(
                    "running", self.client.get("/v1/sessions/dual").json()["state"]
                )
                system.send_json(
                    {
                        "type": "audio.chunk",
                        "sequence": 0,
                        "stream_id": "system-1",
                        "start_ms": 0,
                        "end_ms": 20,
                        "byte_length": len(pcm),
                    }
                )
                system.send_bytes(pcm)
                self.assertEqual("transcript.final", system.receive_json()["type"])
                system.send_json(
                    {"type": "stream.stop", "stream_id": "system-1", "at_ms": 20}
                )
                self.assertEqual("stream.stopped", system.receive_json()["type"])
        self.assertEqual("stopped", self.client.get("/v1/sessions/dual").json()["state"])


class ModelSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.models_root = self.root / "models"
        self.small = _write_model_package(self.models_root, "Qwen3-ASR-0.6B")
        self.large = _write_model_package(self.models_root, "Qwen3-ASR-1.7B")
        self.initial_provider = FakeProvider()
        self.created_paths: list[Path] = []

        def provider_factory(path: Path):
            self.created_paths.append(path)
            return FakeProvider()

        self.app = create_app(
            ServiceSettings(
                model_path=self.large,
                models_root=self.models_root,
                output_root=self.root / "outputs",
            ),
            provider=self.initial_provider,
            provider_factory=provider_factory,
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_lists_installed_models_and_the_active_profile(self) -> None:
        response = self.client.get("/v1/models")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("Qwen3-ASR-1.7B", payload["active_model"])
        self.assertEqual(
            ["Qwen3-ASR-0.6B", "Qwen3-ASR-1.7B"],
            [item["id"] for item in payload["models"]],
        )
        self.assertEqual(
            [False, True], [item["active"] for item in payload["models"]]
        )

    def test_switches_provider_and_uses_the_model_in_session_metadata(self) -> None:
        self.initial_provider.is_loaded = True

        response = self.client.post(
            "/v1/models/active", json={"model": "Qwen3-ASR-0.6B"}
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(self.initial_provider.is_loaded)
        self.assertEqual([self.small.resolve()], self.created_paths)
        self.assertEqual("Qwen3-ASR-0.6B", self.client.get("/health").json()["model"])
        created = self.client.post(
            "/v1/sessions",
            json={"session_id": "small", "sources": ["microphone"]},
        )
        self.assertEqual(201, created.status_code, created.text)
        config = self.app.state.runtime.sessions.get("small").config
        self.assertEqual("Qwen3-ASR-0.6B", config.model)
        self.assertEqual("fake-local", config.provider)

    def test_rejects_switching_while_a_session_is_active(self) -> None:
        self.client.post(
            "/v1/sessions", json={"session_id": "live", "sources": ["microphone"]}
        )
        self.client.post("/v1/sessions/live/start")

        response = self.client.post(
            "/v1/models/active", json={"model": "Qwen3-ASR-0.6B"}
        )

        self.assertEqual(409, response.status_code)
        self.assertIn("stop", response.json()["detail"])
        self.assertEqual([], self.created_paths)

    def test_rejects_session_start_while_model_is_switching(self) -> None:
        self.app.state.runtime.model_switching = True

        response = self.client.post(
            "/v1/sessions", json={"session_id": "racing", "sources": ["microphone"]}
        )

        self.assertEqual(409, response.status_code)
        self.assertIn("changing", response.json()["detail"])

    def test_rejects_uninstalled_or_inactive_model_claims(self) -> None:
        missing = self.client.post(
            "/v1/models/active", json={"model": "Qwen3-ASR-missing"}
        )
        claimed = self.client.post(
            "/v1/sessions",
            json={
                "session_id": "wrong",
                "sources": ["microphone"],
                "model": "Qwen3-ASR-0.6B",
            },
        )

        self.assertEqual(404, missing.status_code)
        self.assertEqual(409, claimed.status_code)


class StreamTimelineTests(unittest.TestCase):
    """Chunk timestamps must follow the capture clock the client reports."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "outputs"
        self.provider = FakeProvider()
        self.client = TestClient(
            create_app(ServiceSettings(output_root=self.output_root), provider=self.provider)
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def _events(self, responses: list[dict]) -> list[dict]:
        return [item["event"] for item in responses if item["type"] == "transcript.final"]

    def test_late_joining_stream_shares_the_session_timeline(self) -> None:
        sources = ["microphone", "system"]
        with self.client.websocket_connect("/v1/sessions/dual/stream") as mic:
            mic.send_json(_start_message("dual", "microphone", sources=sources))
            self.assertEqual("stream.ready", mic.receive_json()["type"])
            mic_events = self._events(_utterance(mic, "microphone", 0))

            # The system source joins thirty seconds into the same session.
            with self.client.websocket_connect("/v1/sessions/dual/stream") as system:
                system.send_json(_start_message("dual", "system", sources=sources))
                self.assertEqual("stream.ready", system.receive_json()["type"])
                system_events = self._events(_utterance(system, "system", 30_000))
                system.send_json({"type": "stream.stop", "stream_id": "system-1"})
                self.assertEqual("stream.stopped", system.receive_json()["type"])
            mic.send_json({"type": "stream.stop", "stream_id": "microphone-1"})
            self.assertEqual("stream.stopped", mic.receive_json()["type"])

        self.assertEqual(0, mic_events[0]["start_ms"])
        self.assertEqual(
            30_000,
            system_events[0]["start_ms"],
            "a stream joining late must not restart the transcript clock at zero",
        )
        transcript = next((self.output_root / "dual").glob("transcript-part-*.md")).read_text()
        self.assertIn("[00:00:00.000", transcript)
        self.assertIn("[00:00:30.000", transcript)

    def test_timeline_survives_pause_and_resume(self) -> None:
        with self.client.websocket_connect("/v1/sessions/paused/stream") as websocket:
            websocket.send_json(_start_message("paused", "microphone"))
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            _utterance(websocket, "microphone", 0)

            websocket.send_json({"type": "stream.pause", "stream_id": "microphone-1"})
            self.assertEqual("stream.paused", websocket.receive_json()["type"])
            websocket.send_json(
                {"type": "stream.resume", "stream_id": "microphone-1", "at_ms": 60_000}
            )
            self.assertEqual("stream.resumed", websocket.receive_json()["type"])

            # Sixty seconds of paused wall time must not be added twice.
            events = self._events(_utterance(websocket, "microphone", 60_000))
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1"})
            self.assertEqual("stream.stopped", websocket.receive_json()["type"])

        self.assertEqual(60_000, events[0]["start_ms"])

    def test_audio_while_paused_is_ignored_without_dropping_the_stream(self) -> None:
        with self.client.websocket_connect("/v1/sessions/gated/stream") as websocket:
            websocket.send_json(_start_message("gated", "microphone"))
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            websocket.send_json({"type": "stream.pause", "stream_id": "microphone-1"})
            self.assertEqual("stream.paused", websocket.receive_json()["type"])

            ignored = _utterance(websocket, "microphone", 0)
            self.assertTrue(all(item["type"] == "audio.ignored" for item in ignored), ignored)
            self.assertEqual(0, self.provider.calls)

            # The connection is still healthy and resumes transcribing.
            websocket.send_json(
                {"type": "stream.resume", "stream_id": "microphone-1", "at_ms": 1_000}
            )
            self.assertEqual("stream.resumed", websocket.receive_json()["type"])
            events = self._events(_utterance(websocket, "microphone", 1_000))
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1"})
            self.assertEqual("stream.stopped", websocket.receive_json()["type"])

        self.assertEqual(1, len(events))
        transcript = next((self.output_root / "gated").glob("transcript-part-*.md")).read_text()
        self.assertEqual(1, transcript.count("Local transcription works."))

    def test_both_streams_must_pause_before_the_shared_session_pauses(self) -> None:
        sources = ["microphone", "system"]
        with self.client.websocket_connect("/v1/sessions/shared/stream") as mic:
            mic.send_json(_start_message("shared", "microphone", sources=sources))
            self.assertEqual("stream.ready", mic.receive_json()["type"])
            with self.client.websocket_connect("/v1/sessions/shared/stream") as system:
                system.send_json(_start_message("shared", "system", sources=sources))
                self.assertEqual("stream.ready", system.receive_json()["type"])

                mic.send_json({"type": "stream.pause", "stream_id": "microphone-1"})
                self.assertEqual("running", mic.receive_json()["session_state"])

                # The second pause used to raise InvalidSessionTransition.
                system.send_json({"type": "stream.pause", "stream_id": "system-1"})
                self.assertEqual("paused", system.receive_json()["session_state"])

                # Either source resuming reopens the shared session.
                system.send_json({"type": "stream.resume", "stream_id": "system-1"})
                self.assertEqual("running", system.receive_json()["session_state"])

                system.send_json({"type": "stream.stop", "stream_id": "system-1"})
                self.assertEqual("stream.stopped", system.receive_json()["type"])
            mic.send_json({"type": "stream.stop", "stream_id": "microphone-1"})
            self.assertEqual("stream.stopped", mic.receive_json()["type"])
        self.assertEqual("stopped", self.client.get("/v1/sessions/shared").json()["state"])

    def test_manual_language_override_reaches_the_provider(self) -> None:
        with self.client.websocket_connect("/v1/sessions/zh/stream") as websocket:
            websocket.send_json(_start_message("zh", "microphone", language="zh"))
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            events = self._events(_utterance(websocket, "microphone", 0))
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1"})
            self.assertEqual("stream.stopped", websocket.receive_json()["type"])
        self.assertEqual("zh", events[0]["language"])


class OverlapDeduplicationTests(unittest.TestCase):
    """Chunk overlap must not put repeated phrases into the transcript."""

    def test_repeated_overlap_text_is_removed_before_persistence(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output_root = Path(temporary.name) / "outputs"
        provider = ScriptedProvider(
            ["we should ship the beta", "the beta on Friday"]
        )
        client = TestClient(
            create_app(ServiceSettings(output_root=output_root), provider=provider)
        )
        self.addCleanup(client.close)

        overlapping = {
            "threshold_rms": 500,
            "min_speech_duration_ms": 20,
            "silence_duration_ms": 40,
            "pre_roll_ms": 0,
            "overlap_ms": 40,
        }
        with client.websocket_connect("/v1/sessions/overlap/stream") as websocket:
            websocket.send_json(
                _start_message("overlap", "microphone", vad=overlapping)
            )
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            texts = []
            for index in range(2):
                # Contiguous capture time: chunk two starts inside chunk one's
                # range, which is exactly the overlap the chunker introduces.
                for item in _utterance(websocket, "microphone", index * 60):
                    if item["type"] == "transcript.final":
                        texts.append(item["event"]["text"])
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1"})
            self.assertEqual("stream.stopped", websocket.receive_json()["type"])

        self.assertEqual(["we should ship the beta", "on Friday"], texts)
        transcript = next((output_root / "overlap").glob("transcript-part-*.md")).read_text()
        self.assertEqual(1, transcript.count("the beta"))


class RuntimeGuardTests(unittest.TestCase):
    def test_finalizing_audio_twice_never_overwrites_or_raises(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output_root = Path(temporary.name) / "outputs"
        app = create_app(ServiceSettings(output_root=output_root), provider=FakeProvider())
        runtime = app.state.runtime

        session = runtime.sessions.create(
            SessionConfig(
                session_id="repeat",
                sources=(Source.SYSTEM,),
                audio_saved=True,
                save_combined_audio=True,
            )
        )
        session.start()
        writer = AudioRetentionWriter(output_root / "repeat")
        writer.append_pcm(Source.SYSTEM, bytes(640), start_ms=0)
        runtime.audio_writers["repeat"] = writer
        _finalize_audio(runtime, session)
        combined = output_root / "repeat" / "combined-part-001.wav"
        self.assertTrue(combined.is_file())
        original = combined.read_bytes()

        # A recovered session finalizes again; existing audio must survive.
        runtime.audio_writers["repeat"] = writer
        _finalize_audio(runtime, session)
        self.assertEqual(original, combined.read_bytes())

    def test_lag_is_reported_only_after_a_full_window_of_slow_work(self) -> None:
        context = _StreamContext(
            session_id="s",
            start={},
            stream_id="microphone-1",
            source=Source.MICROPHONE,
            chunker=EnergyVadChunker(VadConfig()),
            sample_rate=16000,
        )
        self.assertIsNone(context.record_work(1_000.0, 4_000.0), "one slow chunk is not lag")
        self.assertIsNone(context.record_work(2_000.0, 8_000.0))
        factor = context.record_work(3_000.0, 12_000.0)
        self.assertIsNotNone(factor)
        self.assertAlmostEqual(4.0, factor)
        # The window resets, so keeping up again stops the warnings.
        self.assertIsNone(context.record_work(6_000.0, 600.0))


class ShutdownTests(unittest.TestCase):
    def test_shutdown_closes_sessions_left_running(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output_root = Path(temporary.name) / "outputs"
        app = create_app(ServiceSettings(output_root=output_root), provider=FakeProvider())

        with TestClient(app) as client:
            client.post(
                "/v1/transcriptions",
                data={"session_id": "batch", "source": "microphone"},
                files={"file": ("a.wav", b"fake-wave-data", "audio/wav")},
            )
            self.assertEqual("running", client.get("/v1/sessions/batch").json()["state"])

        transcript = next((output_root / "batch").glob("transcript-part-*.md")).read_text()
        self.assertIn("voxink-part-status: complete", transcript)


if __name__ == "__main__":
    unittest.main()


class StreamSessionErrorTests(unittest.TestCase):
    """A stream that cannot join its session must say why, not just drop."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "outputs"
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=FakeProvider()
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def _start(self, stream_id: str, source: str) -> dict:
        return {
            "type": "stream.start",
            "schema_version": 1,
            "session_id": "closed",
            "stream_id": stream_id,
            "source": source,
            "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16_000, "channels": 1},
            "stream_mode": "frames",
        }

    def test_joining_a_stopped_session_reports_a_readable_error(self) -> None:
        self.client.post("/v1/sessions", json={"session_id": "closed"})
        self.client.post("/v1/sessions/closed/start")
        self.client.post("/v1/sessions/closed/stop")
        with self.client.websocket_connect("/v1/sessions/closed/stream") as websocket:
            websocket.send_json(self._start("microphone-1", "microphone"))
            message = websocket.receive_json()
        self.assertEqual("error", message["type"])
        self.assertEqual("session_unavailable", message["code"])
        self.assertIn("stopped", message["message"])

    def test_a_source_outside_the_session_reports_a_readable_error(self) -> None:
        self.client.post(
            "/v1/sessions", json={"session_id": "closed", "sources": ["microphone"]}
        )
        with self.client.websocket_connect("/v1/sessions/closed/stream") as websocket:
            websocket.send_json(self._start("system-1", "system"))
            message = websocket.receive_json()
        self.assertEqual("error", message["type"])
        self.assertEqual("session_unavailable", message["code"])
        self.assertIn("system", message["message"])


class StreamDeclaredSessionTests(unittest.TestCase):
    """A stream.start must be able to configure the session it implicitly creates.

    Every shipped client opens a session this way rather than through
    `POST /v1/sessions`, so anything only that route can set is unreachable in
    practice.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "outputs"
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=FakeProvider()
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def _front_matter(self, session_id: str) -> str:
        part = next((self.output_root / session_id).glob("transcript-part-*.md"))
        return part.read_text()

    def test_selected_applications_reach_the_transcript_metadata(self) -> None:
        message = _start_message("apps", "system")
        message["application_bundle_identifiers"] = [
            "com.apple.Safari",
            "us.zoom.xos",
            "com.apple.Safari",
        ]
        with self.client.websocket_connect("/v1/sessions/apps/stream") as websocket:
            websocket.send_json(message)
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            _utterance(websocket, "system", 0)
            websocket.send_json({"type": "stream.stop", "stream_id": "system-1"})

        front_matter = self._front_matter("apps")
        self.assertIn('applications: ["com.apple.Safari", "us.zoom.xos"]', front_matter)

    def test_microphone_stream_carries_the_sessions_application_list(self) -> None:
        """The native client opens the mic socket first; the list must survive."""
        message = _start_message("mixed", "microphone", sources=["microphone", "system"])
        message["application_bundle_identifiers"] = ["us.zoom.xos"]
        with self.client.websocket_connect("/v1/sessions/mixed/stream") as websocket:
            websocket.send_json(message)
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            _utterance(websocket, "microphone", 0)
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1"})

        self.assertIn('applications: ["us.zoom.xos"]', self._front_matter("mixed"))

    def test_requested_retention_saves_audio_without_a_session_route_call(self) -> None:
        message = _start_message("kept", "microphone")
        message["retention"] = {"audio": True, "separate": True, "combined": False}
        with self.client.websocket_connect("/v1/sessions/kept/stream") as websocket:
            websocket.send_json(message)
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            _utterance(websocket, "microphone", 0)
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1"})

        session_dir = self.output_root / "kept"
        saved = sorted(path.name for path in session_dir.glob("*.wav"))
        self.assertEqual(["microphone-part-001.wav"], saved)
        self.assertIn("audio_saved: true", self._front_matter("kept"))

    def test_retention_stays_off_when_no_client_asks_for_it(self) -> None:
        with self.client.websocket_connect("/v1/sessions/plain/stream") as websocket:
            websocket.send_json(_start_message("plain", "microphone"))
            self.assertEqual("stream.ready", websocket.receive_json()["type"])
            _utterance(websocket, "microphone", 0)
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1"})

        self.assertEqual([], list((self.output_root / "plain").glob("*.wav")))
        self.assertIn("audio_saved: false", self._front_matter("plain"))

    def test_an_impossible_retention_request_is_reported_not_ignored(self) -> None:
        message = _start_message("bad", "microphone")
        message["retention"] = {"audio": True, "separate": False, "combined": False}
        with self.client.websocket_connect("/v1/sessions/bad/stream") as websocket:
            websocket.send_json(message)
            reply = websocket.receive_json()
        self.assertEqual("error", reply["type"])
        self.assertEqual("invalid_message", reply["code"])
        self.assertIn("save_separate_audio", reply["message"])

    def test_unknown_retention_fields_are_rejected(self) -> None:
        message = _start_message("typo", "microphone")
        message["retention"] = {"audio": True, "seperate": True}
        with self.client.websocket_connect("/v1/sessions/typo/stream") as websocket:
            websocket.send_json(message)
            reply = websocket.receive_json()
        self.assertEqual("error", reply["type"])
        self.assertIn("seperate", reply["message"])


class BatchSessionLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "outputs"
        app = create_app(
            ServiceSettings(output_root=self.output_root), provider=FakeProvider()
        )
        self.runtime = app.state.runtime
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def _transcribe(self, **data: str) -> dict:
        response = self.client.post(
            "/v1/transcriptions",
            data=data,
            files={"file": ("a.wav", b"fake-wave-data", "audio/wav")},
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_an_unnamed_batch_call_closes_the_session_it_created(self) -> None:
        for _ in range(3):
            self._transcribe(source="microphone")

        sessions = self.runtime.sessions.list()
        self.assertEqual(3, len(sessions))
        self.assertEqual(
            ["stopped", "stopped", "stopped"], [item.state.value for item in sessions]
        )
        self.assertFalse(any(item.writer.is_open for item in sessions))
        for session in sessions:
            part = next(
                (self.output_root / session.config.session_id).glob("transcript-part-*.md")
            )
            self.assertIn("voxink-part-status: complete", part.read_text())

    def test_a_named_batch_session_stays_open_for_more_audio(self) -> None:
        self._transcribe(session_id="named", source="microphone")
        self._transcribe(session_id="named", source="microphone")

        self.assertEqual("running", self.client.get("/v1/sessions/named").json()["state"])
        part = next((self.output_root / "named").glob("transcript-part-*.md"))
        self.assertEqual(2, part.read_text().count("[MIC]"))
