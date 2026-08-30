from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from voxink_core.artifacts import (
    discover_transcripts,
    read_transcript,
    read_transcript_text,
)
from voxink_core.models import SessionConfig, Source, TranscriptEvent
from voxink_core.persistence.markdown import MarkdownTranscriptWriter


def _config(session_id: str) -> SessionConfig:
    return SessionConfig(session_id=session_id, sources=(Source.MICROPHONE,))


def _event(session_id: str, text: str, start_ms: int = 0) -> TranscriptEvent:
    return TranscriptEvent(
        session_id=session_id,
        segment_id=f"seg-{start_ms}",
        stream_id="microphone-1",
        source=Source.MICROPHONE,
        start_ms=start_ms,
        end_ms=start_ms + 1000,
        text=text,
        is_final=True,
    )


class TranscriptArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _write(self, session_id: str, texts: list[str], *, close: bool = True) -> None:
        writer = MarkdownTranscriptWriter(
            self.root / session_id, _config(session_id), durable_flush=False
        )
        writer.open()
        for index, text in enumerate(texts):
            writer.append(_event(session_id, text, start_ms=index * 1000))
        if close:
            writer.close()
        else:
            # The documented way to simulate an interrupted part: the handle is
            # closed without the completion marker the writer would have added.
            writer.__exit__(RuntimeError, RuntimeError("interrupted"), None)

    def test_discovers_sessions_newest_first_with_part_inventory(self) -> None:
        self._write("2026-01-01_10-00-00", ["first"])
        self._write("2026-01-02_10-00-00", ["second"])

        found = discover_transcripts(self.root)

        self.assertEqual(
            ["2026-01-02_10-00-00", "2026-01-01_10-00-00"],
            [item.session_id for item in found],
        )
        self.assertEqual(1, len(found[0].parts))
        self.assertEqual("complete", found[0].parts[0].status)
        self.assertGreater(found[0].size_bytes, 0)

    def test_reports_front_matter_without_a_yaml_dependency(self) -> None:
        self._write("2026-01-01_10-00-00", ["hello"])

        artifacts = read_transcript(self.root, "2026-01-01_10-00-00")

        assert artifacts is not None
        self.assertEqual("2026-01-01_10-00-00", artifacts.metadata["session_id"])
        self.assertEqual("1", artifacts.metadata["schema_version"])

    def test_marks_a_part_the_writer_never_closed_as_interrupted(self) -> None:
        self._write("2026-01-01_10-00-00", ["hello"], close=False)

        artifacts = read_transcript(self.root, "2026-01-01_10-00-00")

        assert artifacts is not None
        self.assertEqual("interrupted", artifacts.parts[0].status)
        self.assertTrue(artifacts.is_interrupted)

    def test_text_omits_front_matter_and_status_markers(self) -> None:
        self._write("2026-01-01_10-00-00", ["hello there"])

        text = read_transcript_text(self.root, "2026-01-01_10-00-00")

        self.assertIn("hello there", text)
        self.assertNotIn("schema_version", text)
        self.assertNotIn("voxink-part-status", text)

    def test_one_part_can_be_read_alone(self) -> None:
        session_id = "2026-01-01_10-00-00"
        writer = MarkdownTranscriptWriter(
            self.root / session_id,
            _config(session_id),
            max_part_bytes=1,
            durable_flush=False,
        )
        writer.open()
        writer.append(_event(session_id, "part one", start_ms=0))
        writer.append(_event(session_id, "part two", start_ms=1000))
        writer.close()

        self.assertIn("part two", read_transcript_text(self.root, session_id, part=2))
        self.assertNotIn("part two", read_transcript_text(self.root, session_id, part=1))
        with self.assertRaises(FileNotFoundError):
            read_transcript_text(self.root, session_id, part=99)

    def test_a_session_identifier_cannot_escape_the_output_root(self) -> None:
        (self.root / "outside").mkdir()

        self.assertIsNone(read_transcript(self.root / "inside", "../outside"))
        self.assertIsNone(read_transcript(self.root, "../"))
        self.assertIsNone(read_transcript(self.root, ""))

    def test_a_directory_without_parts_is_not_a_transcript(self) -> None:
        (self.root / "empty").mkdir()

        self.assertEqual((), discover_transcripts(self.root))
        self.assertIsNone(read_transcript(self.root, "empty"))

    def test_a_missing_output_root_lists_nothing(self) -> None:
        self.assertEqual((), discover_transcripts(self.root / "absent"))


if __name__ == "__main__":
    unittest.main()
