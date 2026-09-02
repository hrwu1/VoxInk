"""Partial captions, echo suppression, channels, markers, and translation."""

from __future__ import annotations

from collections import deque
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from fastapi.testclient import TestClient

from services.transcription.app import ServiceSettings, create_app
from tests.integration.test_transcription_service import (
    SILENCE,
    VOICED,
    FakeProvider,
    _send_frame,
    _start_message,
)


class FakeTranslator:
    name = "fake-translate"
    is_loaded = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def load(self) -> None:
        pass

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        self.calls.append((text, source_language, target_language))
        return f"[{target_language}] {text}"

    def unload(self) -> None:
        pass


def _pattern(seed: int, amplitude: float = 4000.0) -> bytes:
    generator = np.random.default_rng(seed)
    return np.clip(generator.normal(0.0, amplitude, 320), -32768, 32767).astype("<i2").tobytes()


def _attenuate(pcm: bytes, factor: float) -> bytes:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * factor
    return samples.astype("<i2").tobytes()


def _frame_then_drain(websocket, source: str, pcm: bytes, start_ms: int, *, stream_id: str | None = None) -> list[dict]:
    """Send one frame and collect every message up to its acknowledgement."""
    websocket.send_json(
        {
            "type": "audio.frame",
            "sequence": start_ms // 20,
            "stream_id": stream_id or f"{source}-1",
            "start_ms": start_ms,
            "duration_ms": 20,
            "byte_length": len(pcm),
        }
    )
    websocket.send_bytes(pcm)
    received = []
    while True:
        message = websocket.receive_json()
        received.append(message)
        if message["type"] in {"audio.accepted", "transcript.final", "audio.echo_suppressed", "audio.ignored"}:
            return received


class LiveFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output_root = Path(self.temporary.name) / "outputs"
        self.provider = FakeProvider()
        self.translator = FakeTranslator()
        app = create_app(
            ServiceSettings(
                output_root=self.output_root,
                partial_interval_ms=0,
                translation_model_path=None,
            ),
            provider=self.provider,
            translator=self.translator,
        )
        self.runtime = app.state.runtime
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def transcript(self, session_id: str) -> str:
        return (self.output_root / session_id / "transcript-part-001.md").read_text(encoding="utf-8")

    # -- partial captions ------------------------------------------------------

    def test_partials_precede_the_final_under_one_segment_id(self) -> None:
        self.assertTrue(self.client.get("/health").json()["capabilities"]["partial_results"])
        session_id = "partials"
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            self.assertTrue(websocket.receive_json()["partial_results"])
            # Room tone first, so the gate learns the room rather than the voice.
            for index in range(10):
                _frame_then_drain(websocket, "microphone", SILENCE, index * 20)
            partials = []
            for index in range(10, 50):
                for message in _frame_then_drain(websocket, "microphone", VOICED, index * 20):
                    if message["type"] == "transcript.partial":
                        partials.append(message["event"])
            finals = []
            for index in range(50, 54):
                for message in _frame_then_drain(websocket, "microphone", SILENCE, index * 20):
                    if message["type"] == "transcript.final":
                        finals.append(message["event"])
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 1100})
            self.assertEqual(websocket.receive_json()["type"], "stream.stopped")
        self.assertGreaterEqual(
            len(partials), 1, f"no partial caption was emitted ({self.runtime.partial_error})"
        )
        self.assertEqual(len(finals), 1)
        self.assertFalse(partials[0]["is_final"])
        self.assertEqual({p["segment_id"] for p in partials}, {finals[0]["segment_id"]})
        self.assertTrue(finals[0]["is_final"])
        # Partial text never reaches the canonical transcript.
        self.assertEqual(self.transcript(session_id).count("Local transcription works."), 1)

    def test_partials_can_be_switched_off(self) -> None:
        app = create_app(
            ServiceSettings(output_root=self.output_root, partial_captions=False, translation_model_path=None),
            provider=FakeProvider(),
        )
        with TestClient(app) as client:
            self.assertFalse(client.get("/health").json()["capabilities"]["partial_results"])
            with client.websocket_connect("/v1/sessions/nopartials/stream") as websocket:
                websocket.send_json(_start_message("nopartials", "microphone"))
                websocket.receive_json()
                types = set()
                for index in range(10):
                    _frame_then_drain(websocket, "microphone", SILENCE, index * 20)
                for index in range(10, 50):
                    for message in _frame_then_drain(websocket, "microphone", VOICED, index * 20):
                        types.add(message["type"])
                self.assertNotIn("transcript.partial", types)

    # -- echo suppression ------------------------------------------------------

    def test_room_echo_of_the_system_channel_is_dropped(self) -> None:
        session_id = "echo"
        played = _pattern(seed=1)
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as system:
            system.send_json(_start_message(session_id, "system", sources=["microphone", "system"]))
            system.receive_json()
            with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as microphone:
                microphone.send_json(_start_message(session_id, "microphone", sources=["microphone", "system"]))
                microphone.receive_json()
                # The system played a burst at 1000 ms; the mic heard a quieter
                # copy 40 ms later, then room tone.
                _frame_then_drain(system, "system", played, 1000)
                for offset in range(1, 4):
                    _frame_then_drain(system, "system", SILENCE, 1000 + offset * 20)
                messages = _frame_then_drain(microphone, "microphone", _attenuate(played, 0.4), 1040)
                messages += _frame_then_drain(microphone, "microphone", SILENCE, 1060)
                messages += _frame_then_drain(microphone, "microphone", SILENCE, 1080)
                suppressed = [m for m in messages if m["type"] == "audio.echo_suppressed"]
                self.assertEqual(len(suppressed), 1, messages)
                self.assertTrue(suppressed[0]["echo"])
                self.assertEqual(suppressed[0]["channel"], "system-1")
                self.assertAlmostEqual(suppressed[0]["lag_ms"], 40, delta=5)
                self.assertFalse([m for m in messages if m["type"] == "transcript.final"])
                # Speech of the user's own is still transcribed.
                own = _pattern(seed=2)
                messages = _frame_then_drain(microphone, "microphone", own, 3000)
                messages += _frame_then_drain(microphone, "microphone", SILENCE, 3020)
                messages += _frame_then_drain(microphone, "microphone", SILENCE, 3040)
                self.assertEqual(len([m for m in messages if m["type"] == "transcript.final"]), 1)
                self.assertFalse([m for m in messages if m["type"] == "audio.echo_suppressed"])
                microphone.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 3100})
                microphone.receive_json()
            system.send_json({"type": "stream.stop", "stream_id": "system-1", "at_ms": 3100})
            system.receive_json()
        text = self.transcript(session_id)
        self.assertEqual(text.count("[MIC]"), 1)

    # -- channels --------------------------------------------------------------

    def test_a_per_application_stream_is_labeled_and_retained_separately(self) -> None:
        session_id = "channels"
        start = _start_message(session_id, "system")
        start.update(
            {
                "stream_id": "system-us.zoom.xos",
                "channel": "us.zoom.xos",
                "channel_name": "Zoom",
                "application_bundle_identifiers": ["us.zoom.xos"],
                "retention": {"audio": True, "separate": True, "combined": False},
            }
        )
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(start)
            websocket.receive_json()
            finals = []
            for offset, pcm in enumerate((VOICED, SILENCE, SILENCE)):
                for message in _frame_then_drain(websocket, "system", pcm, offset * 20, stream_id="system-us.zoom.xos"):
                    if message["type"] == "transcript.final":
                        finals.append(message["event"])
            websocket.send_json({"type": "stream.stop", "stream_id": "system-us.zoom.xos", "at_ms": 100})
            websocket.receive_json()
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["channel"], "us.zoom.xos")
        self.assertEqual(finals[0]["channel_name"], "Zoom")
        self.assertIn("[SYSTEM: Zoom]**", self.transcript(session_id))
        self.assertTrue((self.output_root / session_id / "system-us.zoom.xos-part-001.wav").is_file())

    # -- markers ---------------------------------------------------------------

    def test_mark_writes_into_the_open_transcript_and_the_feed(self) -> None:
        session_id = "marked"
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            websocket.receive_json()
            self.runtime.native_process = MagicMock(poll=MagicMock(return_value=None))
            self.runtime.native_session_id = session_id
            self.runtime.ui_events[session_id] = deque(maxlen=50)
            self.runtime.ui_events_total[session_id] = 0
            response = self.client.post("/v1/ui/capture/mark", json={"label": "Decision: ship"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["label"], "Decision: ship")
            status = self.client.get("/v1/ui/capture/status").json()
            self.assertEqual([e["type"] for e in status["events"]], ["mark"])
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 100})
            websocket.receive_json()
        self.assertIn("[MARK]**\nDecision: ship", self.transcript(session_id))
        self.runtime.native_process = None

    def test_mark_needs_a_running_capture(self) -> None:
        self.assertEqual(self.client.post("/v1/ui/capture/mark", json={}).status_code, 409)

    # -- reveal ----------------------------------------------------------------

    def test_reveal_opens_the_last_session_folder(self) -> None:
        session_id = "revealed"
        (self.output_root / session_id).mkdir(parents=True)
        self.runtime.last_session_id = session_id
        with patch("services.transcription.app.sys.platform", "darwin"), patch(
            "services.transcription.app.subprocess.run"
        ) as run:
            response = self.client.post("/v1/ui/actions/reveal", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(run.call_args.args[0], ["open", str(self.output_root / session_id)])
            self.assertEqual(
                self.client.post("/v1/ui/actions/reveal", json={"session_id": "missing"}).status_code, 404
            )

    # -- translation -----------------------------------------------------------

    def test_translation_follows_the_final_and_is_quoted_in_markdown(self) -> None:
        self.assertTrue(self.client.get("/v1/translation").json()["available"])
        session_id = "translated"
        self.runtime.translate_sessions.add(session_id)
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            websocket.receive_json()
            messages = []
            for offset, pcm in enumerate((VOICED, SILENCE, SILENCE, SILENCE, SILENCE)):
                messages += _frame_then_drain(websocket, "microphone", pcm, offset * 20)
            translations = [m for m in messages if m["type"] == "transcript.translation"]
            if not translations:
                # The translation thread may still be finishing; one more frame flushes it.
                messages = _frame_then_drain(websocket, "microphone", SILENCE, 120)
                translations = [m for m in messages if m["type"] == "transcript.translation"]
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 200})
            websocket.receive_json()
        self.assertEqual(len(translations), 1, messages)
        event = translations[0]["event"]
        self.assertEqual(event["language"], "zh")
        self.assertEqual(event["text"], "[zh] Local transcription works.")
        self.assertEqual(self.translator.calls[0][1:], ("en", "zh"))
        self.assertIn("> [00:00:00.000 · zh] [zh] Local transcription works.", self.transcript(session_id))

    def test_translation_is_refused_when_no_model_is_installed(self) -> None:
        app = create_app(
            ServiceSettings(output_root=self.output_root, translation_model_path=None),
            provider=FakeProvider(),
        )
        with TestClient(app) as client:
            self.assertFalse(client.get("/v1/translation").json()["available"])
            self.assertFalse(client.get("/health").json()["capabilities"]["translation"])


if __name__ == "__main__":
    unittest.main()
