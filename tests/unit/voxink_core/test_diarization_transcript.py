from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from voxink_core.models import Source
from voxink_core.diarization.transcript import (
    block_digest,
    file_digest,
    parse_part,
    parse_timestamp,
    part_digests,
    read_blocks,
)

from tests.fixtures.transcripts import make_event, write_session


class ParseTimestampTests(unittest.TestCase):
    def test_parses_the_canonical_format(self):
        self.assertEqual(parse_timestamp("00:00:00.000"), 0)
        self.assertEqual(parse_timestamp("00:00:01.250"), 1250)
        self.assertEqual(parse_timestamp("01:02:03.004"), 3_723_004)

    def test_parses_hours_beyond_two_digits(self):
        """The canonical writer widens the hours field rather than truncating."""
        self.assertEqual(parse_timestamp("100:00:00.000"), 360_000_000)


class ReadBlocksTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_reads_timestamps_source_and_text(self):
        write_session(
            self.directory,
            [
                make_event(1250, 4820, "Could we review the first proposal?"),
                make_event(5000, 6000, "Yes, let's start."),
            ],
        )
        blocks = read_blocks(self.directory)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].start_ms, 1250)
        self.assertEqual(blocks[0].end_ms, 4820)
        self.assertIs(blocks[0].source, Source.MICROPHONE)
        self.assertEqual(blocks[0].text, "Could we review the first proposal?")
        self.assertIsNone(blocks[0].speaker)

    def test_block_indexes_are_one_based_within_each_part(self):
        write_session(
            self.directory,
            [make_event(i * 1000, i * 1000 + 500, f"line {i}") for i in range(3)],
        )
        blocks = read_blocks(self.directory)
        self.assertEqual([b.block for b in blocks], [1, 2, 3])
        self.assertEqual({b.part for b in blocks}, {"transcript-part-001.md"})

    def test_rotated_parts_are_ordered_numerically_and_restart_indexing(self):
        write_session(
            self.directory,
            [make_event(i * 1000, i * 1000 + 500, f"line {i}" * 20) for i in range(12)],
            max_part_bytes=700,
        )
        parts = sorted(p.name for p in self.directory.glob("transcript-part-*.md"))
        self.assertGreater(len(parts), 1, "fixture failed to rotate")
        blocks = read_blocks(self.directory)
        self.assertEqual(len(blocks), 12)
        # Timestamps stay on the session clock across rotation, so the blocks
        # come back in chronological order regardless of which part they are in.
        self.assertEqual(
            [b.start_ms for b in blocks], sorted(b.start_ms for b in blocks)
        )
        first_of_each_part = {}
        for block in blocks:
            first_of_each_part.setdefault(block.part, block.block)
        self.assertEqual(set(first_of_each_part.values()), {1})

    def test_completion_marker_is_not_read_as_transcript_text(self):
        write_session(self.directory, [make_event(0, 1000, "only line")])
        blocks = read_blocks(self.directory)
        self.assertEqual(blocks[-1].text, "only line")
        self.assertNotIn("voxink-part-status", blocks[-1].text)

    def test_multiline_text_survives_including_blank_lines(self):
        """Blocks are delimited by the next header, not by a blank line."""
        write_session(self.directory, [make_event(0, 1000, "first\n\nsecond")])
        blocks = read_blocks(self.directory)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "first\n\nsecond")

    def test_existing_canonical_speaker_is_reported(self):
        write_session(
            self.directory, [make_event(0, 1000, "hello", speaker="MIC_SPEAKER_00")]
        )
        self.assertEqual(read_blocks(self.directory)[0].speaker, "MIC_SPEAKER_00")

    def test_speaker_labels_round_trip_without_swallowing_neighboring_blocks(self):
        names = (
            "Alice", "Alice [Host]", "[主持人]", r"Alice\Host", r"Alice\[Host]",
            "Alice\\", "[", "]", "[MIC] **", "Alice\nHost", None,
        )
        events = [
            make_event(index * 1000, (index + 1) * 1000, f"utterance {index}", speaker=name)
            for index, name in enumerate(names)
        ]
        write_session(self.directory, events)
        blocks = read_blocks(self.directory)
        self.assertEqual([block.speaker for block in blocks], list(names))
        self.assertEqual([block.text for block in blocks], [event.text for event in events])
        self.assertEqual([block.start_ms for block in blocks], [event.start_ms for event in events])
        self.assertEqual([block.block for block in blocks], list(range(1, len(events) + 1)))
        self.assertEqual(blocks[0].header, "**[00:00:00.000-00:00:01.000] [MIC] [Alice]**")

    def test_legacy_headers_keep_literal_backslashes_and_brackets(self):
        # These fixtures must stay independent of the new writer: its escaping
        # must not change what an already stored speaker name means.
        names = (r"Alice\Host", r"Alice\[Host", "Alice\\", r"Alice\nHost", "Alice [Host]")
        part = self.directory / "transcript-part-001.md"
        part.write_text(
            "".join(
                f"**[00:00:0{index}.000-00:00:0{index + 1}.000] [MIC] [{name}]**\nline {index}\n\n"
                for index, name in enumerate(names)
            ),
            encoding="utf-8",
        )
        blocks = read_blocks(self.directory)
        self.assertEqual([block.speaker for block in blocks], list(names))
        self.assertEqual([block.text for block in blocks], [f"line {index}" for index in range(len(names))])

    def test_system_source_blocks_are_distinguished(self):
        write_session(
            self.directory,
            [
                make_event(0, 1000, "mic line"),
                make_event(1000, 2000, "system line", source=Source.SYSTEM),
            ],
            sources=(Source.MICROPHONE, Source.SYSTEM),
        )
        blocks = read_blocks(self.directory)
        self.assertEqual(
            [b.source for b in blocks], [Source.MICROPHONE, Source.SYSTEM]
        )

    def test_header_is_retained_verbatim(self):
        write_session(self.directory, [make_event(1250, 4820, "hello")])
        block = read_blocks(self.directory)[0]
        self.assertEqual(block.header, "**[00:00:01.250-00:00:04.820] [MIC]**")

    def test_empty_directory_yields_no_blocks(self):
        self.assertEqual(read_blocks(self.directory), ())


class DigestTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)

    def test_block_digest_changes_when_text_changes(self):
        write_session(self.directory, [make_event(0, 1000, "original")])
        before = read_blocks(self.directory)[0].sha256
        part = self.directory / "transcript-part-001.md"
        part.write_text(
            part.read_text(encoding="utf-8").replace("original", "edited"),
            encoding="utf-8",
        )
        self.assertNotEqual(read_blocks(self.directory)[0].sha256, before)

    def test_block_digest_is_stable_across_reads(self):
        write_session(self.directory, [make_event(0, 1000, "stable")])
        self.assertEqual(
            read_blocks(self.directory)[0].sha256, read_blocks(self.directory)[0].sha256
        )

    def test_block_digest_covers_the_header_not_only_the_text(self):
        """Two blocks with identical words at different times must differ."""
        write_session(
            self.directory,
            [make_event(0, 1000, "same words"), make_event(5000, 6000, "same words")],
        )
        blocks = read_blocks(self.directory)
        self.assertNotEqual(blocks[0].sha256, blocks[1].sha256)

    def test_block_digest_ignores_a_trailing_completion_marker(self):
        """Closing a part must not invalidate the blocks already inside it."""
        write_session(self.directory, [make_event(0, 1000, "line")])
        closed = read_blocks(self.directory)[0].sha256
        part = self.directory / "transcript-part-001.md"
        text = part.read_text(encoding="utf-8")
        part.write_text(text.replace("\n<!-- voxink-part-status: complete -->\n", ""), encoding="utf-8")
        self.assertEqual(read_blocks(self.directory)[0].sha256, closed)

    def test_part_digests_cover_every_part(self):
        write_session(
            self.directory,
            [make_event(i * 1000, i * 1000 + 500, f"line {i}" * 20) for i in range(12)],
            max_part_bytes=700,
        )
        digests = part_digests(self.directory)
        parts = {p.name for p in self.directory.glob("transcript-part-*.md")}
        self.assertEqual(set(digests), parts)
        for name, digest in digests.items():
            self.assertEqual(digest, file_digest(self.directory / name))

    def test_parse_part_reads_one_file(self):
        write_session(self.directory, [make_event(0, 1000, "line")])
        blocks = parse_part(self.directory / "transcript-part-001.md")
        self.assertEqual(len(blocks), 1)


class BlockDigestFunctionTests(unittest.TestCase):
    def test_is_a_sha256_hex_digest(self):
        digest = block_digest("anything")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, block_digest("anything"))
        self.assertNotEqual(digest, block_digest("anything else"))


if __name__ == "__main__":
    unittest.main()
