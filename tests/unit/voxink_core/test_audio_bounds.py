"""Storage bounds on audio retention.

Retention is enabled by default and writes for as long as a session runs, so
the question these cover is what happens when it cannot keep writing. The
answer has to be that the session carries on: the transcript is the artifact,
and the recording is a convenience.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import wave

from voxink_core.artifacts import read_transcript
from voxink_core.models import SessionConfig, Source, TranscriptEvent
from voxink_core.persistence.audio import AudioRetentionWriter
from voxink_core.persistence.markdown import MarkdownTranscriptWriter

SAMPLE_RATE = 16_000
ONE_SECOND = b"\x01\x00" * SAMPLE_RATE


def transcript_writer(session_dir: Path, session_id: str) -> MarkdownTranscriptWriter:
    config = SessionConfig(session_id, (Source.MICROPHONE,))
    return MarkdownTranscriptWriter(session_dir, config, durable_flush=False)


def spoken(session_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id,
        segment_id=f"{session_id}-1",
        stream_id="microphone-1",
        source=Source.MICROPHONE,
        text=text,
        start_ms=0,
        end_ms=1_000,
        is_final=True,
    )


class StorageBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temporary.name) / "session"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def writer(self, **kwargs) -> AudioRetentionWriter:
        options = {
            "sample_rate": SAMPLE_RATE,
            "durable_flush": False,
            "free_space_check_bytes": 1,
            "free_space_probe": lambda _path: 10 * 1024**3,
        }
        options.update(kwargs)
        return AudioRetentionWriter(self.session_dir, **options)

    def test_retains_normally_when_there_is_room(self) -> None:
        writer = self.writer()
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        writer.close()
        self.assertFalse(writer.retention_stopped)
        self.assertEqual(len(ONE_SECOND), writer.retained_bytes)

    def test_stops_retaining_when_free_space_falls_below_the_reserve(self) -> None:
        writer = self.writer(
            min_free_bytes=1024**3,
            free_space_probe=lambda _path: 100 * 1024**2,
        )
        touched = writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        self.assertEqual((), touched)
        self.assertTrue(writer.retention_stopped)
        self.assertEqual("low_disk", writer.stopped_reason)

    def test_stops_retaining_at_the_session_limit(self) -> None:
        writer = self.writer(max_session_bytes=len(ONE_SECOND) + 1)
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        self.assertFalse(writer.retention_stopped)
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=1_000)
        self.assertTrue(writer.retention_stopped)
        self.assertEqual("session_limit", writer.stopped_reason)

    def test_audio_written_before_the_bound_stays_playable(self) -> None:
        """Stopping must close the WAV, not abandon it mid-write.

        A truncated header would make the part unreadable, which would lose the
        audio the bound was reached while protecting.
        """
        writer = self.writer(max_session_bytes=len(ONE_SECOND) + 1)
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=1_000)
        writer.close()
        parts = sorted(self.session_dir.glob("microphone-part-*.wav"))
        self.assertEqual(1, len(parts))
        with wave.open(str(parts[0])) as handle:
            self.assertEqual(SAMPLE_RATE, handle.getframerate())
            self.assertEqual(SAMPLE_RATE, handle.getnframes())

    def test_further_audio_is_dropped_rather_than_failing(self) -> None:
        """A stopped writer stays quiet: a full disk must not raise per chunk."""
        writer = self.writer(
            min_free_bytes=1024**3, free_space_probe=lambda _path: 0
        )
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        for offset in range(1, 5):
            self.assertEqual(
                (), writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=offset * 1_000)
            )
        writer.close()

    def test_the_transcript_survives_the_bound(self) -> None:
        """The point of the whole exercise, asserted end to end."""
        writer = self.writer(min_free_bytes=1024**3, free_space_probe=lambda _path: 0)
        transcript = transcript_writer(self.session_dir, "bounded")
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        transcript.append(spoken("bounded", "the disk filled but this was still written"))
        transcript.close()
        writer.close()

        self.assertTrue(writer.retention_stopped)
        artifacts = read_transcript(self.session_dir.parent, self.session_dir.name)
        self.assertIsNotNone(artifacts)
        self.assertIn("still written", artifacts.parts[0].path.read_text())

    def test_an_unreadable_destination_is_not_treated_as_a_full_disk(self) -> None:
        def probe(_path: Path) -> int:
            raise OSError("destination is not readable")

        writer = self.writer(min_free_bytes=1024**3, free_space_probe=probe)
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        writer.close()
        self.assertFalse(writer.retention_stopped)

    def test_free_space_is_probed_before_the_first_write(self) -> None:
        """A session started on a full disk must not write an interval first."""
        probes: list[Path] = []

        def probe(path: Path) -> int:
            probes.append(path)
            return 0

        writer = self.writer(
            min_free_bytes=1024**3,
            free_space_check_bytes=4 * 1024 * 1024,
            free_space_probe=probe,
        )
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        self.assertEqual(1, len(probes))
        self.assertTrue(writer.retention_stopped)

    def test_probing_is_not_done_on_every_write(self) -> None:
        probes: list[Path] = []

        def probe(path: Path) -> int:
            probes.append(path)
            return 10 * 1024**3

        writer = self.writer(
            free_space_check_bytes=len(ONE_SECOND) * 3, free_space_probe=probe
        )
        for offset in range(4):
            writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=offset * 1_000)
        writer.close()
        self.assertLess(len(probes), 4)

    def test_rejects_incoherent_bounds(self) -> None:
        for kwargs in (
            {"min_free_bytes": -1},
            {"max_session_bytes": 0},
            {"free_space_check_bytes": 0},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    AudioRetentionWriter(self.session_dir, **kwargs)


class RetainedSizeReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reports_audio_alongside_the_transcript(self) -> None:
        session_dir = self.root / "sized"
        transcript = transcript_writer(session_dir, "sized")
        transcript.append(spoken("sized", "hello"))
        transcript.close()
        writer = AudioRetentionWriter(
            session_dir, sample_rate=SAMPLE_RATE, durable_flush=False
        )
        writer.append_pcm(Source.MICROPHONE, ONE_SECOND, start_ms=0)
        writer.close()

        artifacts = read_transcript(self.root, "sized")
        self.assertIsNotNone(artifacts)
        self.assertGreaterEqual(artifacts.audio_bytes, len(ONE_SECOND))
        self.assertEqual(
            artifacts.total_bytes, artifacts.size_bytes + artifacts.audio_bytes
        )
        # Audio dwarfs the transcript; that asymmetry is why it is reported.
        self.assertGreater(artifacts.audio_bytes, artifacts.size_bytes)
        self.assertEqual(artifacts.audio_bytes, artifacts.to_dict()["audio_bytes"])

    def test_a_session_without_audio_reports_zero(self) -> None:
        session_dir = self.root / "textonly"
        transcript = transcript_writer(session_dir, "textonly")
        transcript.append(spoken("textonly", "no audio kept"))
        transcript.close()

        artifacts = read_transcript(self.root, "textonly")
        self.assertIsNotNone(artifacts)
        self.assertEqual(0, artifacts.audio_bytes)
        self.assertEqual(artifacts.size_bytes, artifacts.total_bytes)


if __name__ == "__main__":
    unittest.main()
