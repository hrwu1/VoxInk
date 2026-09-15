from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import wave

from clients.windows_capture.models import AudioFrame, AudioSource, CaptureError, CaptureStatusEvent
from clients.windows_capture.service_client import WebSocketAudioFrameSink


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.incoming: asyncio.Queue[str | Exception | None] = asyncio.Queue()
        self.fail_kind: str | None = None
        self.acknowledge_stop = True
        self.closed = False

    async def send(self, value: str | bytes) -> None:
        kind = json.loads(value)["type"] if isinstance(value, str) else "binary"
        if kind == self.fail_kind:
            raise ConnectionError("fake transport failure")
        self.sent.append(value)
        if kind == "stream.start":
            self.receive({"type": "stream.ready", "stream_id": json.loads(value)["stream_id"]})
        if kind == "stream.stop" and self.acknowledge_stop:
            self.receive({"type": "stream.stopped"})

    def receive(self, event: dict) -> None:
        self.incoming.put_nowait(json.dumps(event))

    def kinds(self) -> list[str]:
        return [json.loads(value)["type"] if isinstance(value, str) else "binary" for value in self.sent]

    def headers(self, kind: str) -> list[dict]:
        return [json.loads(value) for value in self.sent if isinstance(value, str) and json.loads(value)["type"] == kind]

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)


class FakeWebsockets:
    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.refuse = False
        self.headers: list[dict | None] = []

    async def connect(self, url: str, additional_headers=None) -> FakeSocket:
        self.headers.append(additional_headers)
        if self.refuse:
            raise ConnectionRefusedError("service down")
        socket = FakeSocket()
        self.sockets.append(socket)
        return socket


def frame(
    stream_id: str = "microphone-1", sequence: int = 1, source: AudioSource = AudioSource.MICROPHONE,
    pcm: bytes = b"\0\0" * 320, timestamp_ns: int | None = None, **extra,
) -> AudioFrame:
    return AudioFrame(
        session_id="demo", stream_id=stream_id, source=source, sequence=sequence,
        timestamp_ns=timestamp_ns if timestamp_ns is not None else time.monotonic_ns(),
        sample_rate=16_000, pcm_s16le=pcm, **extra,
    )


class ServiceClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.websockets = FakeWebsockets()
        self.statuses: list[CaptureStatusEvent] = []
        self.transcripts: list[dict] = []
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sink = self.make_sink()

    def make_sink(self, **overrides) -> WebSocketAudioFrameSink:
        options = dict(
            endpoint="ws://127.0.0.1:8300/v1/sessions/demo/stream",
            auth_token="secret",
            spool_directory=Path(self.directory.name),
            transcript_handler=self.transcripts.append,
            status_handler=self.statuses.append,
            websockets=self.websockets,
        )
        options.update(overrides)
        sink = WebSocketAudioFrameSink(**options)
        sink.STOP_ACK_TIMEOUT_SECONDS = 0.2
        sink.TRANSPORT_TIMEOUT_SECONDS = 0.5
        return sink

    def codes(self) -> list[str]:
        return [status.code for status in self.statuses]

    def test_only_loopback_websocket_endpoints_are_accepted(self) -> None:
        for url in ("http://127.0.0.1:8300/x", "ws://example.com/x", "ws://10.0.0.5/x"):
            with self.assertRaises(CaptureError):
                WebSocketAudioFrameSink(url, websockets=self.websockets)
        WebSocketAudioFrameSink("wss://localhost:1/x", websockets=self.websockets)

    async def test_stream_start_carries_the_session_shape_and_the_credential(self) -> None:
        sink = self.make_sink(applications=["zoom.exe"], retain_audio=True, language="zh")
        await sink.connect("demo", [AudioSource.MICROPHONE, AudioSource.SYSTEM])
        await sink.send(frame())
        await sink.send(frame(
            stream_id="system-zoom.exe", source=AudioSource.SYSTEM,
            application_identifiers=("zoom.exe",), channel="zoom.exe", channel_name="Zoom",
        ))
        self.assertEqual([{"Authorization": "Bearer secret"}] * 2, self.websockets.headers)
        microphone, system = (socket.headers("stream.start")[0] for socket in self.websockets.sockets)
        self.assertEqual("microphone-1", microphone["stream_id"])
        self.assertEqual(["microphone", "system"], microphone["session_sources"])
        self.assertEqual(["zoom.exe"], microphone["application_bundle_identifiers"])
        self.assertNotIn("channel", microphone)
        self.assertEqual({"audio": True, "separate": True, "combined": False}, microphone["retention"])
        self.assertEqual("zh", microphone["language"])
        self.assertEqual("frames", microphone["stream_mode"])
        self.assertEqual(700, microphone["vad"]["silence_duration_ms"])
        self.assertEqual("voxink-windows-capture", microphone["client"]["name"])
        self.assertEqual("zoom.exe", system["channel"])
        self.assertEqual("Zoom", system["channel_name"])
        self.assertEqual("system", system["source"])
        await sink.disconnect("demo", "test")

    async def test_frames_are_header_binary_pairs_on_a_contiguous_timeline(self) -> None:
        await self.sink.connect("demo", [AudioSource.MICROPHONE])
        started = self.sink._session_started_ns
        await self.sink.send(frame(sequence=1, timestamp_ns=started + 20_000_000))
        await self.sink.send(frame(sequence=2, timestamp_ns=started + 25_000_000))
        socket = self.websockets.sockets[0]
        self.assertEqual(["stream.start", "audio.frame", "binary", "audio.frame", "binary"], socket.kinds())
        first, second = socket.headers("audio.frame")
        self.assertEqual((1, 0.0, 20.0, 640), (first["sequence"], first["start_ms"], first["duration_ms"], first["byte_length"]))
        self.assertEqual(20.0, second["start_ms"], "a burst-delivered frame is tiled after the previous one")
        await self.sink.disconnect("demo", "test")
        self.assertEqual("stream.stop", socket.kinds()[-1])
        self.assertTrue(socket.closed)
        self.assertEqual([], self.codes())

    async def test_an_unreachable_service_keeps_audio_and_delivers_it_on_reconnect(self) -> None:
        self.websockets.refuse = True
        await self.sink.connect("demo", [AudioSource.MICROPHONE])
        await self.sink.send(frame(sequence=1))
        await self.sink.send(frame(sequence=2))
        self.assertEqual(["service_unreachable"], self.codes())
        self.assertEqual(2, len(self.sink._backlogs["microphone-1"].frames))
        self.websockets.refuse = False
        await asyncio.sleep(0.3)
        await self.sink.send(frame(sequence=3))
        await asyncio.sleep(0)
        self.assertEqual(["service_unreachable", "service_reconnecting", "service_reconnected"], self.codes())
        socket = self.websockets.sockets[0]
        self.assertEqual([1, 2, 3], [header["sequence"] for header in socket.headers("audio.frame")])
        await self.sink.disconnect("demo", "test")

    async def test_a_send_failure_drops_the_socket_and_keeps_the_frame(self) -> None:
        await self.sink.connect("demo", [AudioSource.MICROPHONE])
        await self.sink.send(frame(sequence=1))
        socket = self.websockets.sockets[0]
        socket.fail_kind = "binary"
        await self.sink.send(frame(sequence=2))
        await asyncio.sleep(0)
        self.assertTrue(socket.closed)
        self.assertEqual([2], [item.sequence for item in self.sink._backlogs["microphone-1"].frames])
        await asyncio.sleep(0.3)
        await self.sink.send(frame(sequence=3))
        replacement = self.websockets.sockets[1]
        self.assertEqual([2, 3], [header["sequence"] for header in replacement.headers("audio.frame")])
        self.assertIn("service_reconnecting", self.codes())
        await self.sink.disconnect("demo", "test")

    async def test_a_finalized_session_spools_the_backlog_and_later_audio_to_wav(self) -> None:
        await self.sink.connect("demo", [AudioSource.SYSTEM])
        await self.sink.send(frame(stream_id="system-1", source=AudioSource.SYSTEM, sequence=1, pcm=b"\1\0" * 320))
        socket = self.websockets.sockets[0]
        socket.fail_kind = "binary"
        await self.sink.send(frame(stream_id="system-1", source=AudioSource.SYSTEM, sequence=2, pcm=b"\2\0" * 320))
        await asyncio.sleep(0)
        self.sink._handle_envelope({"type": "error", "code": "session_unavailable", "message": "gone"}, "system-1")
        await self.sink.send(frame(stream_id="system-1", source=AudioSource.SYSTEM, sequence=3, pcm=b"\3\0" * 320))
        await self.sink.disconnect("demo", "test")
        self.assertEqual(["audio_spooled", "session_unavailable", "audio_spooled"], self.codes())
        spool = Path(self.directory.name) / "system-1-spool-001.wav"
        with wave.open(str(spool), "rb") as handle:
            self.assertEqual(640, handle.getnframes())
            data = handle.readframes(640)
        self.assertEqual(b"\2\0" * 320 + b"\3\0" * 320, data)
        self.assertIn("0.0 s of audio", self.statuses[-1].message)
        self.assertIn(str(spool), self.statuses[-1].message)

    async def test_stop_waits_for_the_acknowledgement_and_bounds_a_missing_one(self) -> None:
        await self.sink.connect("demo", [AudioSource.MICROPHONE])
        await self.sink.send(frame())
        socket = self.websockets.sockets[0]
        socket.acknowledge_stop = False
        started = time.monotonic()
        await self.sink.disconnect("demo", "test")
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(["stop_timeout"], self.codes())
        self.assertTrue(socket.closed)

    async def test_pause_and_resume_reach_every_open_stream_after_the_audio(self) -> None:
        await self.sink.connect("demo", [AudioSource.MICROPHONE, AudioSource.SYSTEM])
        await self.sink.send(frame())
        await self.sink.send(frame(stream_id="system-1", source=AudioSource.SYSTEM))
        await self.sink.pause("demo")
        await self.sink.resume("demo")
        for socket in self.websockets.sockets:
            self.assertEqual(["stream.start", "audio.frame", "binary", "stream.pause", "stream.resume"], socket.kinds())
        pause = self.websockets.sockets[0].headers("stream.pause")[0]
        self.assertEqual("microphone-1", pause["stream_id"])
        self.assertGreaterEqual(pause["at_ms"], 0.0)
        await self.sink.disconnect("demo", "test")

    async def test_transcripts_and_service_errors_are_surfaced(self) -> None:
        await self.sink.connect("demo", [AudioSource.MICROPHONE])
        await self.sink.send(frame())
        socket = self.websockets.sockets[0]
        socket.receive({"type": "transcript.final", "event": {"text": "hello", "is_final": True, "source": "microphone"}})
        socket.receive({"type": "error", "code": "transcript_not_stored", "message": "disk full"})
        socket.receive({"type": "error", "code": "live_speakers_stopped", "message": "gave up"})
        await asyncio.sleep(0.05)
        self.assertEqual([{"text": "hello", "is_final": True, "source": "microphone"}], self.transcripts)
        self.assertEqual(["transcript_not_stored", "live_speakers_stopped"], self.codes())
        self.assertEqual("error", self.statuses[0].level.value)
        self.assertEqual("warning", self.statuses[1].level.value)
        await self.sink.disconnect("demo", "test")

    async def test_an_unexpected_close_is_reported_and_the_next_frame_reconnects(self) -> None:
        await self.sink.connect("demo", [AudioSource.MICROPHONE])
        await self.sink.send(frame(sequence=1))
        socket = self.websockets.sockets[0]
        socket.incoming.put_nowait(ConnectionError("lost helper"))
        await asyncio.sleep(0.05)
        self.assertEqual(["service_connection_closed"], self.codes())
        await self.sink.send(frame(sequence=2))
        self.assertEqual(2, len(self.websockets.sockets))
        self.assertEqual([2], [header["sequence"] for header in self.websockets.sockets[1].headers("audio.frame")])
        await self.sink.disconnect("demo", "test")


if __name__ == "__main__":
    unittest.main()
