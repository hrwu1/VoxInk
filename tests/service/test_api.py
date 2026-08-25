from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from service.app import ServiceSettings, create_app
from voxink.models import TranscriptEvent


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


if __name__ == "__main__":
    unittest.main()
