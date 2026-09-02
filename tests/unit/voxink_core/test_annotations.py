"""Markers, translations, and channel labels beside the canonical blocks."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from clients.python_capture.vad import EnergyVadChunker, VadConfig
from voxink_core.diarization.transcript import read_blocks
from voxink_core.models import SessionConfig, Source, TranscriptEvent
from voxink_core.persistence.audio import AudioRetentionWriter, track_name
from voxink_core.persistence.markdown import MarkdownTranscriptWriter
from voxink_core.sessions import InvalidSessionTransition, SessionManager


def _event(start_ms: int, text: str, *, source: Source = Source.MICROPHONE, **extra) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="s1",
        segment_id=f"seg-{start_ms}",
        stream_id=f"{source.value}-1",
        source=source,
        start_ms=start_ms,
        end_ms=start_ms + 1000,
        text=text,
        is_final=True,
        **extra,
    )


class AnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "s1"
        self.config = SessionConfig(session_id="s1", sources=(Source.MICROPHONE, Source.SYSTEM))

    def test_marks_and_translations_are_written_and_skipped_by_the_parser(self) -> None:
        writer = MarkdownTranscriptWriter(self.directory, self.config, durable_flush=False)
        with writer:
            writer.append(_event(0, "Hello there."))
            writer.append_translation(_event(0, "Hello there."), "zh", "你好。")
            writer.append_mark(1500, "decision: ship it")
            writer.append(_event(2000, "Second block."))
        text = (self.directory / "transcript-part-001.md").read_text(encoding="utf-8")
        self.assertIn("> [00:00:00.000 · zh] 你好。", text)
        self.assertIn("**[00:00:01.500] [MARK]**\ndecision: ship it", text)
        blocks = read_blocks(self.directory)
        self.assertEqual([block.text for block in blocks], ["Hello there.", "Second block."])

    def test_channel_label_round_trips_through_the_parser(self) -> None:
        writer = MarkdownTranscriptWriter(self.directory, self.config, durable_flush=False)
        with writer:
            writer.append(
                _event(0, "From the call.", source=Source.SYSTEM, channel="us.zoom.xos", channel_name="Zoom")
            )
        text = (self.directory / "transcript-part-001.md").read_text(encoding="utf-8")
        self.assertIn("[SYSTEM: Zoom]**", text)
        (block,) = read_blocks(self.directory)
        self.assertIs(block.source, Source.SYSTEM)
        self.assertEqual(block.text, "From the call.")

    def test_session_marks_while_paused_but_not_when_stopped(self) -> None:
        manager = SessionManager(
            Path(self.temporary.name),
            writer_factory=lambda directory, config: MarkdownTranscriptWriter(
                directory, config, durable_flush=False
            ),
        )
        manager.create(self.config)
        manager.start("s1")
        manager.pause("s1")
        manager.mark("s1", 500, "paused here")
        manager.stop("s1")
        with self.assertRaises(InvalidSessionTransition):
            manager.mark("s1", 600)

    def test_vad_exposes_the_open_utterance(self) -> None:
        chunker = EnergyVadChunker(
            VadConfig(threshold_rms=100, min_speech_duration_ms=20, silence_duration_ms=100)
        )
        self.assertIsNone(chunker.active_audio())
        loud = b"\x10\x27" * 320  # 20 ms of a constant 10000-sample tone
        chunker.feed(loud * 3)
        pcm, start, end = chunker.active_audio()
        self.assertEqual((start, end), (0, 960))
        self.assertEqual(len(pcm), 3 * 640)


class ChannelTrackTests(unittest.TestCase):
    def test_track_names(self) -> None:
        self.assertEqual(track_name(Source.MICROPHONE), "microphone")
        self.assertEqual(track_name(Source.SYSTEM, "us.zoom.xos"), "system-us.zoom.xos")
        self.assertEqual(track_name(Source.SYSTEM, "../x y"), "system-x_y")
        self.assertEqual(track_name(Source.SYSTEM, "///"), "system-channel")

    def test_two_channels_get_separate_tracks_and_a_combined_mix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            writer = AudioRetentionWriter(directory, durable_flush=False)
            tone = b"\x00\x10" * 160  # 10 ms at 16 kHz
            writer.append_pcm(Source.SYSTEM, tone, start_ms=0, channel="us.zoom.xos")
            writer.append_pcm(Source.SYSTEM, tone, start_ms=0, channel="com.apple.Safari")
            writer.append_pcm(Source.MICROPHONE, tone, start_ms=0)
            writer.close()
            names = sorted(path.name for path in directory.glob("*.wav"))
            self.assertEqual(
                names,
                [
                    "microphone-part-001.wav",
                    "system-com.apple.Safari-part-001.wav",
                    "system-us.zoom.xos-part-001.wav",
                ],
            )
            outputs = writer.generate_combined()
            self.assertEqual([path.name for path in outputs], ["combined-part-001.wav"])
            combined = (directory / "combined-part-001.wav").read_bytes()[44:]
            # Three tracks of 0x1000 sum to 0x3000 per sample.
            self.assertEqual(combined[:2], b"\x00\x30")


if __name__ == "__main__":
    unittest.main()
