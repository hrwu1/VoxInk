from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest
import wave

from clients.windows_capture.backlog import AudioBacklog, AudioSpool, StreamClock
from clients.windows_capture.models import (
    AudioFrame,
    AudioSource,
    CaptureConfiguration,
    ReconnectDecision,
    StreamReconnectPolicy,
    SystemAudioBackend,
    SystemAudioSelection,
)


def frame(stream_id: str = "microphone-1", pcm: bytes = b"\0\0" * 320, sequence: int = 1) -> AudioFrame:
    return AudioFrame(
        session_id="demo", stream_id=stream_id, source=AudioSource.MICROPHONE,
        sequence=sequence, timestamp_ns=0, sample_rate=16_000, pcm_s16le=pcm,
    )


class SelectionAndBackendTests(unittest.TestCase):
    def test_application_identifiers_are_case_insensitive_executable_names(self) -> None:
        selection = SystemAudioSelection.for_applications(["Zoom.exe", " chrome.EXE "])
        self.assertEqual(["chrome.exe", "zoom.exe"], selection.sorted_applications)
        self.assertFalse(selection.is_all)
        self.assertTrue(SystemAudioSelection.all().is_all)

    def test_the_helpers_backend_names_map_onto_the_windows_facilities(self) -> None:
        self.assertIs(SystemAudioBackend.PROCESS_LOOPBACK, SystemAudioBackend.parse("tap"))
        self.assertIs(SystemAudioBackend.LOOPBACK, SystemAudioBackend.parse("screencapturekit"))
        self.assertIs(SystemAudioBackend.AUTO, SystemAudioBackend.parse("auto"))
        with self.assertRaisesRegex(ValueError, "Unknown backend"):
            SystemAudioBackend.parse("coreaudio")

    def test_the_default_endpoint_is_the_loopback_session_stream(self) -> None:
        configuration = CaptureConfiguration(session_id="a b", microphone_enabled=True, system_audio=None)
        self.assertEqual("ws://127.0.0.1:8300/v1/sessions/a%20b/stream", configuration.service_url)
        self.assertEqual([], configuration.selected_applications)
        with_apps = CaptureConfiguration(
            session_id="s", microphone_enabled=False,
            system_audio=SystemAudioSelection.for_applications(["b.exe", "a.exe"]),
        )
        self.assertEqual(["a.exe", "b.exe"], with_apps.selected_applications)


class ReconnectPolicyTests(unittest.TestCase):
    def test_first_use_connects_then_reconnects_with_backoff(self) -> None:
        policy = StreamReconnectPolicy(initial_delay_seconds=0.25, maximum_delay_seconds=1.0)
        self.assertEqual(ReconnectDecision.CONNECT, policy.socket_needed("s", now=0).decision)
        self.assertEqual(ReconnectDecision.RECONNECT, policy.socket_needed("s", now=1).decision)
        self.assertEqual(0.25, policy.attempt_failed("s", now=1))
        waiting = policy.socket_needed("s", now=1.1)
        self.assertEqual(ReconnectDecision.WAIT, waiting.decision)
        self.assertAlmostEqual(0.15, waiting.wait_seconds)
        self.assertEqual(2, policy.socket_needed("s", now=2).attempt)
        self.assertEqual(0.5, policy.attempt_failed("s", now=2))
        self.assertEqual(1.0, policy.attempt_failed("s", now=3))
        self.assertEqual(1.0, policy.attempt_failed("s", now=5), "capped at the maximum delay")

    def test_a_ready_stream_closes_the_reconnect_and_a_dead_session_ends_it(self) -> None:
        policy = StreamReconnectPolicy()
        policy.socket_needed("s", now=0)
        self.assertFalse(policy.stream_ready("s"), "the first connect is not a reconnect")
        policy.socket_needed("s", now=1)
        self.assertTrue(policy.stream_ready("s"))
        policy.session_unavailable("s")
        self.assertTrue(policy.is_dead("s"))
        self.assertEqual(ReconnectDecision.DEAD, policy.socket_needed("s", now=2).decision)


class StreamClockTests(unittest.TestCase):
    def test_late_bursts_are_tiled_after_the_previous_frame(self) -> None:
        clock = StreamClock()
        self.assertEqual(80.0, clock.start_ms("m", captured_at_ms=100.0, duration_ms=20.0))
        clock.advance("m", 100.0)
        # A burst-delivered frame nominally starts inside the previous one.
        self.assertEqual(100.0, clock.start_ms("m", captured_at_ms=105.0, duration_ms=20.0))
        self.assertEqual(0.0, clock.start_ms("other", captured_at_ms=10.0, duration_ms=20.0))


class BacklogTests(unittest.TestCase):
    def test_eviction_frees_a_quarter_of_the_capacity_in_capture_order(self) -> None:
        backlog = AudioBacklog(capacity_bytes=1000)
        frames = [frame(pcm=bytes(100), sequence=index) for index in range(12)]
        evicted: list[AudioFrame] = []
        for item in frames:
            evicted.extend(backlog.append(item))
        # The eleventh frame crossed the capacity; four went so that a quarter was free.
        self.assertEqual([0, 1, 2, 3], [item.sequence for item in evicted])
        self.assertEqual(800, backlog.byte_count)
        self.assertEqual([4, 5, 6, 7, 8, 9, 10, 11], [item.sequence for item in backlog.drain()])
        self.assertTrue(backlog.is_empty)
        self.assertEqual(0, backlog.byte_count)

    def test_a_single_oversized_frame_is_kept_rather_than_evicted(self) -> None:
        backlog = AudioBacklog(capacity_bytes=10)
        self.assertEqual([], backlog.append(frame(pcm=bytes(50))))
        self.assertEqual(1, len(backlog.frames))


class SpoolTests(unittest.TestCase):
    def test_the_spool_is_a_readable_wav_with_patched_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = AudioSpool(Path(directory), "system-1", 16_000)
            spool.append(b"\1\0" * 16_000)
            spool.append(b"\2\0" * 16_000)
            spool.append(b"\3\0" * 16_000)
            self.assertEqual(3.0, spool.duration_seconds)
            spool.close()
            self.assertEqual("system-1-spool-001.wav", spool.path.name)
            with wave.open(str(spool.path), "rb") as handle:
                self.assertEqual(1, handle.getnchannels())
                self.assertEqual(16_000, handle.getframerate())
                self.assertEqual(2, handle.getsampwidth())
                self.assertEqual(48_000, handle.getnframes())
            second = AudioSpool(Path(directory), "system-1", 16_000)
            self.assertEqual("system-1-spool-002.wav", second.path.name)
            second.close()

    def test_the_header_is_valid_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = AudioSpool(Path(directory), "microphone-1", 16_000)
            spool.append(bytes(64_000))
            spool.append(bytes(64_000))
            header = spool.path.read_bytes()[:44]
            self.assertEqual(b"RIFF", header[:4])
            self.assertEqual(128_000, struct.unpack("<I", header[40:44])[0])
            spool.close()


if __name__ == "__main__":
    unittest.main()
