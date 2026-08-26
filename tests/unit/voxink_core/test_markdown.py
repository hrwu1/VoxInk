import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from voxink_core.models import SessionConfig, Source, TranscriptEvent
from voxink_core.persistence.markdown import (
    COMPLETE_MARKER,
    RECOVERED_MARKER,
    MarkdownTranscriptWriter,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def event(
    number: int,
    *,
    final: bool = True,
    source: Source = Source.MICROPHONE,
    text: str = "Hello, world.",
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="session-1",
        segment_id=f"seg-{number}",
        stream_id="mic-1",
        source=source,
        start_ms=number * 1000,
        end_ms=number * 1000 + 750,
        text=text,
        is_final=final,
    )


class MarkdownWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "session-1"
        self.config = SessionConfig(
            session_id="session-1",
            sources=(Source.MICROPHONE, Source.SYSTEM),
            applications=('Example "App"',),
        )
        self.clock = FakeClock()
        self.wall_time = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def writer(self, **kwargs: object) -> MarkdownTranscriptWriter:
        return MarkdownTranscriptWriter(
            self.directory,
            self.config,
            clock=self.clock,
            wall_clock=lambda: self.wall_time,
            durable_flush=False,
            **kwargs,
        )

    def test_writes_metadata_and_readable_source_labeled_event(self) -> None:
        writer = self.writer()
        path = writer.open()
        writer.append(event(3, source=Source.SYSTEM))
        writer.close()

        content = path.read_text()
        self.assertIn('session_id: "session-1"', content)
        self.assertIn('applications: ["Example \\"App\\""]', content)
        self.assertIn("sources: [\"microphone\", \"system\"]", content)
        self.assertIn("**[00:00:03.000-00:00:03.750] [SYSTEM]**", content)
        self.assertIn("Hello, world.", content)
        self.assertTrue(content.rstrip().endswith(COMPLETE_MARKER))

    def test_partial_events_are_not_persisted(self) -> None:
        writer = self.writer()
        path = writer.open()
        self.assertIsNone(writer.append(event(1, final=False, text="draft")))
        writer.close()
        self.assertNotIn("draft", path.read_text())

    def test_rotates_after_six_hour_style_time_limit(self) -> None:
        writer = self.writer(max_part_seconds=10)
        first_path = writer.open()
        writer.append(event(1))
        self.clock.value = 10
        second_path = writer.append(event(2))
        writer.close()

        self.assertNotEqual(first_path, second_path)
        self.assertIn("seg", event(2).segment_id)
        self.assertIn("00:00:01.000", first_path.read_text())
        self.assertNotIn("00:00:02.000", first_path.read_text())
        self.assertIn("00:00:02.000", second_path.read_text())

    def test_rotates_after_size_limit_without_empty_part_loop(self) -> None:
        writer = self.writer(max_part_bytes=1)
        first_path = writer.open()
        writer.append(event(1, text="a" * 100))
        second_path = writer.append(event(2, text="b" * 100))
        writer.close()
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(len(list(self.directory.glob("transcript-part-*.md"))), 2)

    def test_recovers_incomplete_part_then_opens_next_number(self) -> None:
        writer = self.writer()
        try:
            with writer:
                writer.append(event(1))
                raise RuntimeError("simulated crash")
        except RuntimeError:
            pass

        recovered_writer = self.writer()
        new_path = recovered_writer.open()
        recovered_writer.close()
        old_path = self.directory / "transcript-part-001.md"
        self.assertIn(RECOVERED_MARKER, old_path.read_text())
        self.assertEqual(new_path.name, "transcript-part-002.md")

    def test_rejects_event_from_disabled_source_or_other_session(self) -> None:
        config = SessionConfig("session-1", (Source.MICROPHONE,))
        writer = MarkdownTranscriptWriter(
            self.directory, config, durable_flush=False
        )
        with self.assertRaises(ValueError):
            writer.append(event(1, source=Source.SYSTEM))
        other = event(1).to_dict()
        other["session_id"] = "another"
        with self.assertRaises(ValueError):
            writer.append(TranscriptEvent.from_dict(other))


if __name__ == "__main__":
    unittest.main()
