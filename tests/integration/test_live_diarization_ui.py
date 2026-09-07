"""Opt-in live Diart service wiring, without recording or loading any models.

These tests exercise real HTTP/WebSocket routes and the existing provider and
persistence pipeline. The disposable Diart worker and native capture process
are fakes; real model quality and capture validation are separate checks.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from services.transcription.app import ServiceSettings, create_app
from services.transcription.diart_bridge import LiveTurn
from tests.integration.test_live_features import _frame_then_drain
from tests.integration.test_transcription_service import (
    SILENCE,
    VOICED,
    ScriptedProvider,
    _send_frame,
    _start_message,
)


class FakeBridge:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.feed_results: deque[list[LiveTurn]] = deque()
        self.flush_results: deque[list[LiveTurn]] = deque()
        self.feeds: list[tuple[bytes, int]] = []
        self.flush_count = 0
        self.closed = False
        self.start_error: Exception | None = None
        self.feed_error: Exception | None = None

    async def start(self) -> None:
        self.actions.append("diart.start")
        if self.start_error:
            raise self.start_error

    async def feed(self, pcm: bytes, start_ms: int, *, sequence: int | None = None) -> list[LiveTurn]:
        self.feeds.append((pcm, start_ms))
        if self.feed_error:
            raise self.feed_error
        return self.feed_results.popleft() if self.feed_results else []

    async def flush(self) -> list[LiveTurn]:
        self.flush_count += 1
        return self.flush_results.popleft() if self.flush_results else []

    def close(self) -> None:
        self.closed = True


def _turn(start_ms: int, *speakers: str) -> LiveTurn:
    return LiveTurn(
        pcm=VOICED,
        start_ms=start_ms,
        end_ms=start_ms + 20,
        audio_start_ms=start_ms,
        audio_end_ms=start_ms + 20,
        speakers=tuple(speakers),
    )


def _until(websocket, expected: str) -> list[dict]:
    received = []
    for _ in range(20):
        message = websocket.receive_json()
        received.append(message)
        if message["type"] == expected:
            return received
    raise AssertionError(f"Did not receive {expected!r}: {received}")


def _live_frame(websocket, pcm: bytes, start_ms: int) -> list[dict]:
    """A live frame can emit several turns before its acknowledgement."""
    websocket.send_json({
        "type": "audio.frame", "sequence": start_ms // 20,
        "stream_id": "microphone-1", "start_ms": start_ms,
        "duration_ms": len(pcm) // 32, "byte_length": len(pcm),
    })
    websocket.send_bytes(pcm)
    return _until(websocket, "audio.accepted")


class LiveDiarizationUiTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        executable = root / "voxink-capture"
        executable.touch()
        executable.chmod(0o755)
        self.output_root = root / "outputs"
        self.actions: list[str] = []
        self.bridge = FakeBridge(self.actions)
        self.provider = ScriptedProvider([
            "First participant begins.", "A brief reply.",
            "The first participant returns.", "Words without a clear speaker.",
        ])
        self.process = MagicMock()
        self.process.pid = 12345
        self.process.stderr = None
        self.process.returncode = 0
        self.process.poll.return_value = None

        def spawn(*args, **kwargs):
            self.actions.append("capture.start")
            return self.process

        self.availability = self.enterContext(patch(
            "services.transcription.app.diart_availability",
            return_value={"available": True, "error": None},
        ))
        self.make_bridge = self.enterContext(patch(
            "services.transcription.app.DiartBridge", return_value=self.bridge,
        ))
        self.spawned = self.enterContext(patch(
            "services.transcription.app.subprocess.Popen", side_effect=spawn,
        ))
        app = create_app(ServiceSettings(
            output_root=self.output_root,
            state_root=root / "state",
            native_capture_path=executable,
            live_diarization_runtime=root / "experiment-runtime",
            partial_captions=False,
            echo_suppression=False,
            live_speaker_names=False,
            translation_model_path=None,
            stream_grace_seconds=0,
        ), provider=self.provider)
        self.app = app
        self.runtime = app.state.runtime
        self.client = self.enterContext(TestClient(app))

    def start(self, *, enabled: bool = True, sources: list[str] | None = None):
        return self.client.post("/v1/ui/capture/start", json={
            "sources": sources or ["microphone"],
            "live_diarization": enabled,
            "retain_audio": False,
        })

    def start_session(self, *, sources: list[str] | None = None) -> str:
        response = self.start(sources=sources)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["session_id"]

    def transcript(self, session_id: str) -> str:
        return (self.output_root / session_id / "transcript-part-001.md").read_text()

    def test_capabilities_are_cheap_and_explicit_about_availability(self) -> None:
        response = self.client.get("/v1/ui/capture/capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["live_diarization"]["available"])
        self.make_bridge.assert_not_called()
        self.spawned.assert_not_called()

    def test_default_start_keeps_the_existing_recording_path(self) -> None:
        response = self.client.post("/v1/ui/capture/start", json={
            "sources": ["microphone"], "retain_audio": False,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["live_diarization"]["requested"])
        self.make_bridge.assert_not_called()
        self.assertEqual(self.actions, ["capture.start"])

    def test_opt_in_prewarms_diart_before_starting_capture(self) -> None:
        response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.actions, ["diart.start", "capture.start"])
        self.assertTrue(response.json()["live_diarization"]["requested"])
        self.assertIsNone(response.json()["live_diarization"]["error"])
        self.assertEqual(self.make_bridge.call_args.kwargs["session_id"], response.json()["session_id"])
        self.assertIn("--mic", self.spawned.call_args.args[0])

    def test_system_only_live_diarization_is_rejected_before_startup(self) -> None:
        response = self.start(sources=["system"])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("microphone", response.text.lower())
        self.make_bridge.assert_not_called()
        self.spawned.assert_not_called()

    def test_missing_runtime_is_reported_without_falling_back(self) -> None:
        self.availability.return_value = {"available": False, "error": "owned model is missing"}
        response = self.start()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("missing", response.text)
        self.make_bridge.assert_not_called()
        self.spawned.assert_not_called()

    def test_prewarm_failure_never_starts_capture_and_closes_worker(self) -> None:
        self.bridge.start_error = RuntimeError("model failed to load")
        response = self.start()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("model failed to load", response.text)
        self.spawned.assert_not_called()
        self.assertTrue(self.bridge.closed)

    def test_native_start_failure_closes_the_prewarmed_worker(self) -> None:
        self.spawned.side_effect = OSError("capture executable failed")
        response = self.start()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertTrue(self.bridge.closed)

    def test_stop_cancels_prewarm_and_rejects_a_concurrent_start(self) -> None:
        async def scenario() -> None:
            entered = asyncio.Event()
            suspended = asyncio.Event()

            async def prewarm() -> None:
                entered.set()
                await suspended.wait()

            self.bridge.start = prewarm
            async with AsyncClient(
                transport=ASGITransport(app=self.app), base_url="http://127.0.0.1:8300",
            ) as client:
                options = {"sources": ["microphone"], "live_diarization": True, "retain_audio": False}
                original = asyncio.create_task(client.post("/v1/ui/capture/start", json=options))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    second = await asyncio.wait_for(client.post("/v1/ui/capture/start", json=options), timeout=2)
                    self.assertEqual(second.status_code, 409, second.text)
                    self.assertTrue(self.runtime.native_starting)
                    self.make_bridge.assert_called_once()
                    self.spawned.assert_not_called()

                    stopped = await asyncio.wait_for(client.post("/v1/ui/capture/stop"), timeout=2)
                    self.assertEqual(stopped.status_code, 200, stopped.text)
                    self.assertEqual(stopped.json()["state"], "ready")
                    cancelled = await asyncio.wait_for(original, timeout=2)
                    self.assertEqual(cancelled.status_code, 409, cancelled.text)
                    self.assertIn("cancel", cancelled.text.lower())
                    self.assertTrue(self.bridge.closed)
                    self.assertFalse(self.runtime.native_starting)
                    self.assertIsNone(self.runtime.native_start_task)
                    self.assertEqual(self.runtime.live_diarizers, {})
                    self.spawned.assert_not_called()
                finally:
                    if not original.done():
                        original.cancel()
                    await asyncio.gather(original, return_exceptions=True)

        asyncio.run(scenario())

    def test_initial_invalid_microphone_format_closes_pending_worker(self) -> None:
        session_id = self.start_session()
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            start = _start_message(session_id, "microphone")
            start["audio"]["sample_rate_hz"] = 48000
            websocket.send_json(start)
            self.assertEqual(websocket.receive_json()["code"], "invalid_message")
        self.assertTrue(self.bridge.closed)
        self.assertEqual(self.runtime.live_diarizers, {})
        self.assertEqual(self.provider.calls, 0)

    def test_rejected_second_microphone_stream_preserves_active_worker(self) -> None:
        session_id = self.start_session()
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as first:
            first.send_json(_start_message(session_id, "microphone"))
            self.assertEqual(first.receive_json()["type"], "stream.ready")
            with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as second:
                start = _start_message(session_id, "microphone")
                start["stream_id"] = "microphone-2"
                second.send_json(start)
                self.assertEqual(second.receive_json()["code"], "invalid_message")
            self.assertFalse(self.bridge.closed)
            _live_frame(first, VOICED, 0)
            self.assertEqual(self.bridge.feeds, [(VOICED, 0)])
            first.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 20})
            _until(first, "stream.stopped")
        self.assertTrue(self.bridge.closed)

    def test_helper_exit_before_stream_connects_closes_pending_worker(self) -> None:
        self.start_session()
        self.process.poll.return_value = 1
        status = self.client.get("/v1/ui/capture/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["state"], "failed")
        self.assertTrue(self.bridge.closed)
        self.assertEqual(self.runtime.live_diarizers, {})

    def test_turns_survive_pause_gap_and_labels_remain_provisional(self) -> None:
        self.bridge.feed_results.extend([
            [_turn(1000, "microphone:speaker0")], [],
            [_turn(3000, "microphone:speaker0")],
        ])
        self.bridge.flush_results.extend([
            [_turn(1020, "microphone:speaker1")], [_turn(3020)],
        ])
        session_id = self.start_session()
        append = self.enterContext(patch.object(self.runtime.sessions, "append", wraps=self.runtime.sessions.append))
        finals = []
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            self.assertEqual(websocket.receive_json()["type"], "stream.ready")
            for start_ms in (1000, 1020):
                finals.extend(m["event"] for m in _live_frame(websocket, VOICED, start_ms) if m["type"] == "transcript.final")
            websocket.send_json({"type": "stream.pause", "stream_id": "microphone-1", "at_ms": 1040})
            finals.extend(m["event"] for m in _until(websocket, "stream.paused") if m["type"] == "transcript.final")
            self.assertEqual(_send_frame(websocket, "microphone", VOICED, 2000)["type"], "audio.ignored")
            websocket.send_json({"type": "stream.resume", "stream_id": "microphone-1", "at_ms": 3000})
            self.assertEqual(websocket.receive_json()["type"], "stream.resumed")
            finals.extend(m["event"] for m in _live_frame(websocket, VOICED, 3000) if m["type"] == "transcript.final")
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 3040})
            finals.extend(m["event"] for m in _until(websocket, "stream.stopped") if m["type"] == "transcript.final")

        self.assertEqual(self.bridge.feeds, [(VOICED, 1000), (VOICED, 1020), (VOICED, 3000)])
        self.assertEqual(self.bridge.flush_count, 2)
        self.assertTrue(self.bridge.closed)
        self.assertEqual(self.provider.calls, 4)
        self.assertEqual([event["start_ms"] for event in finals], [1000, 1020, 3000, 3020])
        self.assertEqual([event["speaker"] for event in finals], ["Speaker 1", "Speaker 2", "Speaker 1", None])
        self.assertTrue(all(event["speaker_provisional"] for event in finals))
        self.assertEqual(finals[0]["speakers"], ["microphone:speaker0"])
        self.assertEqual(finals[-1]["speakers"], [])
        self.assertEqual(len(append.call_args_list), 4)
        self.assertTrue(all(call.args[0].speaker is None for call in append.call_args_list))
        feed = self.client.get("/v1/ui/capture/status").json()["events"]
        self.assertEqual([event["speaker"] for event in feed if event["type"] == "transcript"], ["Speaker 1", "Speaker 2", "Speaker 1", None])
        text = self.transcript(session_id)
        self.assertNotIn("[Speaker", text)
        self.assertEqual(text.count("[MIC]"), 4)

    def test_overlap_is_preserved_without_claiming_one_speaker(self) -> None:
        self.bridge.feed_results.append([_turn(0, "microphone:speaker0", "microphone:speaker1")])
        session_id = self.start_session()
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            websocket.receive_json()
            event = next(m["event"] for m in _live_frame(websocket, VOICED, 0) if m["type"] == "transcript.final")
            self.assertTrue(event["speaker_overlap"])
            self.assertTrue(event["speaker_provisional"])
            self.assertEqual(event["speakers"], ["microphone:speaker0", "microphone:speaker1"])
            self.assertNotEqual(event["speaker"], "Speaker 1")
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 20})
            _until(websocket, "stream.stopped")

    def test_text_timestamps_describe_qwen_context_separately_from_turn(self) -> None:
        self.bridge.feed_results.append([LiveTurn(
            pcm=VOICED * 3, start_ms=1020, end_ms=1040,
            audio_start_ms=1000, audio_end_ms=1060,
            speakers=("microphone:speaker0",),
        )])
        session_id = self.start_session()
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            websocket.receive_json()
            event = next(m["event"] for m in _live_frame(websocket, VOICED * 3, 1000) if m["type"] == "transcript.final")
            self.assertEqual((event["start_ms"], event["end_ms"]), (1000, 1060))
            self.assertEqual((event["audio_start_ms"], event["audio_end_ms"]), (1000, 1060))
            self.assertEqual((event["turn_start_ms"], event["turn_end_ms"]), (1020, 1040))
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 1060})
            _until(websocket, "stream.stopped")

    def _repeated_reply_with_padding(self, second_speakers: tuple[str, ...]) -> list[dict]:
        self.provider.texts = ["Okay.", "Okay."]
        self.bridge.feed_results.append([
            LiveTurn(VOICED * 60, 0, 1000, 0, 1200, ("microphone:speaker0",)),
            LiveTurn(VOICED * 70, 1000, 2000, 800, 2200, second_speakers),
        ])
        session_id = self.start_session()
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            websocket.receive_json()
            finals = [m["event"] for m in _live_frame(websocket, VOICED * 110, 0) if m["type"] == "transcript.final"]
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 2200})
            _until(websocket, "stream.stopped")
        self.assertEqual(self.provider.calls, 2)
        return finals

    def test_distinct_speakers_can_repeat_a_brief_reply_despite_context_overlap(self) -> None:
        finals = self._repeated_reply_with_padding(("microphone:speaker1",))
        self.assertEqual([event["text"] for event in finals], ["Okay.", "Okay."])
        self.assertEqual([event["speaker"] for event in finals], ["Speaker 1", "Speaker 2"])

    def test_same_speaker_keeps_existing_context_overlap_deduplication(self) -> None:
        finals = self._repeated_reply_with_padding(("microphone:speaker0",))
        self.assertEqual([event["text"] for event in finals], ["Okay."])

    def test_system_audio_bypasses_diart_in_an_opted_in_session(self) -> None:
        session_id = self.start_session(sources=["microphone", "system"])
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "system", sources=["microphone", "system"]))
            websocket.receive_json()
            messages = []
            for index, pcm in enumerate((VOICED, SILENCE, SILENCE)):
                messages.extend(_frame_then_drain(websocket, "system", pcm, index * 20))
            finals = [message["event"] for message in messages if message["type"] == "transcript.final"]
            self.assertEqual(len(finals), 1)
            self.assertEqual(finals[0]["source"], "system")
            self.assertFalse(finals[0].get("speaker_provisional", False))
            self.assertEqual(self.bridge.feeds, [])
            websocket.send_json({"type": "stream.stop", "stream_id": "system-1", "at_ms": 60})
            _until(websocket, "stream.stopped")

    def test_other_microphone_sessions_do_not_reuse_opted_in_worker(self) -> None:
        self.start_session()
        other = "unrelated-microphone"
        with self.client.websocket_connect(f"/v1/sessions/{other}/stream") as websocket:
            websocket.send_json(_start_message(other, "microphone"))
            websocket.receive_json()
            messages = []
            for index, pcm in enumerate((VOICED, SILENCE, SILENCE)):
                messages.extend(_frame_then_drain(websocket, "microphone", pcm, index * 20))
            self.assertTrue(any(message["type"] == "transcript.final" for message in messages))
            self.assertEqual(self.bridge.feeds, [])
            websocket.send_json({"type": "stream.stop", "stream_id": "microphone-1", "at_ms": 60})
            _until(websocket, "stream.stopped")

    def test_worker_failure_is_visible_and_does_not_transcribe_without_labels(self) -> None:
        self.bridge.feed_error = RuntimeError("Diart worker exited unexpectedly")
        session_id = self.start_session()
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            websocket.receive_json()
            message = _send_frame(websocket, "microphone", VOICED, 0)
            self.assertEqual(message["type"], "error")
            self.assertIn("Diart worker exited", message["message"])
        status = self.client.get("/v1/ui/capture/status").json()["live_diarization"]
        self.assertEqual(status["state"], "failed")
        self.assertIn("Diart worker exited", status["error"])
        self.assertTrue(self.bridge.closed)
        self.assertEqual(self.provider.calls, 0)

    def test_disconnect_closes_the_session_worker(self) -> None:
        session_id = self.start_session()
        with self.client.websocket_connect(f"/v1/sessions/{session_id}/stream") as websocket:
            websocket.send_json(_start_message(session_id, "microphone"))
            websocket.receive_json()
        self.assertTrue(self.bridge.closed)

    def test_api_stop_closes_a_worker_even_before_first_audio(self) -> None:
        self.start_session()
        response = self.client.post("/v1/ui/capture/stop")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.bridge.closed)

    def test_shutdown_closes_a_worker_even_before_first_audio(self) -> None:
        self.start_session()
        self.client.__exit__(None, None, None)
        self.assertTrue(self.bridge.closed)


if __name__ == "__main__":
    unittest.main()
