from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from voxink_core.models import Source, WordTimestamp
from voxink_core.diarization.alignment import reconcile
from voxink_core.diarization.artifacts import (
    MARKDOWN_FILENAME,
    TURNS_FILENAME,
    build_document,
    read_document,
    stale_sources,
    validate_document,
    write_artifacts,
)
from voxink_core.diarization.models import DiarizationError, DiarizationResult, SpeakerTurn
from voxink_core.diarization.transcript import part_digests, read_blocks

from tests.fixtures.transcripts import SESSION_ID, make_event, write_session


CREATED_AT = datetime(2026, 8, 31, 18, 42, 10, tzinfo=timezone.utc)


def turn(speaker, start_ms, end_ms, *, exclusive=True):
    return SpeakerTurn(speaker, start_ms, end_ms, Source.MICROPHONE, exclusive)


class ArtifactTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        write_session(
            self.directory,
            [
                make_event(0, 1000, "Could we review the first proposal?"),
                make_event(1000, 2000, "Yes, let's start."),
                make_event(2000, 3000, "Ambiguous crosstalk here."),
            ],
        )
        self.blocks = read_blocks(self.directory)
        self.result = DiarizationResult(
            model="pyannote/speaker-diarization-community-1",
            model_revision="3533c8cf",
            raw_turns=(
                turn("MIC_SPEAKER_00", 0, 1000, exclusive=False),
                turn("MIC_SPEAKER_01", 1000, 2000, exclusive=False),
                turn("MIC_SPEAKER_00", 2000, 2500, exclusive=False),
                turn("MIC_SPEAKER_01", 2400, 3000, exclusive=False),
            ),
            exclusive_turns=(
                turn("MIC_SPEAKER_00", 0, 1000),
                turn("MIC_SPEAKER_01", 1000, 2000),
                turn("MIC_SPEAKER_00", 2000, 2500),
                turn("MIC_SPEAKER_01", 2500, 3000),
            ),
        )
        self.labels = reconcile(
            self.blocks,
            exclusive_turns=self.result.exclusive_turns,
            raw_turns=self.result.raw_turns,
            source=Source.MICROPHONE,
        )
        self.sources = part_digests(self.directory)

    def write(self, **overrides):
        values = dict(
            session_id=SESSION_ID,
            source=Source.MICROPHONE,
            result=self.result,
            labels=self.labels,
            source_artifacts=self.sources,
            created_at=CREATED_AT,
        )
        values.update(overrides)
        return write_artifacts(self.directory, **values)


class WriteArtifactsTests(ArtifactTestCase):
    def test_writes_both_derived_files(self):
        paths = self.write()
        self.assertTrue(paths.turns.is_file())
        self.assertTrue(paths.markdown.is_file())
        self.assertEqual(paths.turns.name, TURNS_FILENAME)
        self.assertEqual(paths.markdown.name, MARKDOWN_FILENAME)

    def test_canonical_transcript_bytes_are_untouched(self):
        part = self.directory / "transcript-part-001.md"
        before = part.read_bytes()
        self.write()
        self.assertEqual(part.read_bytes(), before)

    def test_artifacts_are_private_to_the_user(self):
        paths = self.write()
        for path in (paths.turns, paths.markdown):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"{path.name} is {oct(mode)}")

    def test_no_temporary_files_are_left_behind(self):
        self.write()
        leftovers = [p.name for p in self.directory.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_rewriting_replaces_in_place(self):
        first = self.write().turns.read_text(encoding="utf-8")
        second = self.write(created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertNotEqual(second.turns.read_text(encoding="utf-8"), first)
        self.assertEqual(
            len(list(self.directory.glob("speaker-turns*.json"))),
            1,
            "a rewrite must not accumulate files",
        )


class AtomicityTests(ArtifactTestCase):
    def test_a_failed_json_publish_leaves_the_previous_version_intact(self):
        self.write()
        original = (self.directory / TURNS_FILENAME).read_bytes()
        real = os.replace

        def fail_on_json(source, target):
            if Path(target).name == TURNS_FILENAME:
                raise OSError("disk went away")
            return real(source, target)

        with mock.patch("voxink_core.diarization.artifacts.os.replace", side_effect=fail_on_json):
            with self.assertRaises(DiarizationError) as caught:
                self.write(created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(
            caught.exception.code.value, "artifact_write_failed"
        )
        self.assertEqual((self.directory / TURNS_FILENAME).read_bytes(), original)
        self.assertEqual(
            [p.name for p in self.directory.iterdir() if p.name.endswith(".tmp")], []
        )

    def test_an_invalid_document_is_never_published(self):
        broken = DiarizationResult(model="m", model_revision="r", raw_turns=(), exclusive_turns=())
        with mock.patch(
            "voxink_core.diarization.artifacts.build_document",
            return_value={"schema_version": 1},
        ):
            with self.assertRaises(ValueError):
                self.write(result=broken)
        self.assertFalse((self.directory / TURNS_FILENAME).exists())
        self.assertFalse((self.directory / MARKDOWN_FILENAME).exists())


class DocumentTests(ArtifactTestCase):
    def test_records_producer_and_provenance(self):
        document = json.loads(self.write().turns.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["status"], "ready")
        self.assertEqual(document["session_id"], SESSION_ID)
        self.assertEqual(document["source"], "microphone")
        self.assertEqual(document["created_at"], "2026-08-31T18:42:10Z")
        self.assertEqual(
            document["producer"]["model_id"], "pyannote/speaker-diarization-community-1"
        )
        self.assertEqual(document["producer"]["model_revision"], "3533c8cf")

    def test_preserves_both_raw_and_exclusive_turns(self):
        document = json.loads(self.write().turns.read_text(encoding="utf-8"))
        self.assertEqual(len(document["raw_turns"]), 4)
        self.assertEqual(len(document["exclusive_turns"]), 4)
        self.assertFalse(document["raw_turns"][0]["exclusive"])
        self.assertTrue(document["exclusive_turns"][0]["exclusive"])

    def test_every_segment_cites_a_canonical_block(self):
        document = json.loads(self.write().turns.read_text(encoding="utf-8"))
        self.assertEqual(len(document["segments"]), len(self.blocks))
        for segment, block in zip(document["segments"], self.blocks):
            self.assertEqual(segment["canonical_ref"]["part"], block.part)
            self.assertEqual(segment["canonical_ref"]["block"], block.block)
            self.assertEqual(segment["canonical_ref"]["sha256"], block.sha256)

    def test_lists_source_artifact_digests(self):
        document = json.loads(self.write().turns.read_text(encoding="utf-8"))
        recorded = {e["path"]: e["sha256"] for e in document["source_artifacts"]}
        self.assertEqual(recorded, self.sources)

    def test_carries_no_transcript_text(self):
        payload = self.write().turns.read_text(encoding="utf-8")
        self.assertNotIn("Could we review", payload)
        self.assertNotIn("Yes, let's start", payload)


class ValidationTests(unittest.TestCase):
    def _document(self, **overrides):
        document = build_document(
            session_id=SESSION_ID,
            source=Source.MICROPHONE,
            result=DiarizationResult("m", "r", (), ()),
            labels=(),
            source_artifacts={},
            created_at=CREATED_AT,
        )
        document.update(overrides)
        return document

    def test_accepts_a_well_formed_document(self):
        validate_document(self._document())

    def test_rejects_missing_keys(self):
        document = self._document()
        del document["producer"]
        with self.assertRaises(ValueError):
            validate_document(document)

    def test_rejects_an_unknown_schema_version(self):
        with self.assertRaises(ValueError):
            validate_document(self._document(schema_version=2))

    def test_rejects_an_unknown_status(self):
        with self.assertRaises(ValueError):
            validate_document(self._document(status="finished"))

    def test_rejects_empty_producer_fields(self):
        document = self._document()
        document["producer"]["model_revision"] = ""
        with self.assertRaises(ValueError):
            validate_document(document)

    def test_rejects_an_invalid_turn_range(self):
        document = self._document(
            raw_turns=[{"speaker_id": "MIC_SPEAKER_00", "start_ms": 500, "end_ms": 500}]
        )
        with self.assertRaises(ValueError):
            validate_document(document)

    def test_rejects_a_coverage_outside_zero_to_one(self):
        document = self._document(
            segments=[
                {
                    "canonical_ref": {"part": "p.md", "block": 1, "sha256": "x"},
                    "speaker_coverage": 1.5,
                }
            ]
        )
        with self.assertRaises(ValueError):
            validate_document(document)

    def test_rejects_transcript_text_and_credentials(self):
        """The privacy rule is enforced, not merely documented."""
        for key in ("text", "token", "authorization"):
            with self.subTest(key=key):
                document = self._document()
                document["segments"] = [
                    {
                        "canonical_ref": {"part": "p.md", "block": 1, "sha256": "x"},
                        "speaker_coverage": 1.0,
                        key: "leaked",
                    }
                ]
                with self.assertRaises(ValueError):
                    validate_document(document)


class MarkdownTests(ArtifactTestCase):
    def test_identifies_itself_as_derived(self):
        text = self.write().markdown.read_text(encoding="utf-8")
        self.assertIn("derived: true", text)
        self.assertIn(f'speaker_turns: "{TURNS_FILENAME}"', text)
        self.assertIn('producer: "voxink-pyannote-community-1"', text)
        self.assertNotIn("part:", text.split("---")[1])

    def test_reproduces_canonical_blocks_in_order(self):
        text = self.write().markdown.read_text(encoding="utf-8")
        positions = [
            text.index("Could we review the first proposal?"),
            text.index("Yes, let's start."),
            text.index("Ambiguous crosstalk here."),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_labels_confident_blocks_and_leaves_others_bare(self):
        text = self.write().markdown.read_text(encoding="utf-8")
        self.assertIn(
            "**[00:00:00.000-00:00:01.000] [MIC] [MIC_SPEAKER_00]**", text
        )
        self.assertIn(
            "**[00:00:01.000-00:00:02.000] [MIC] [MIC_SPEAKER_01]**", text
        )
        # The third block splits 500/500 between two speakers: a tie stays bare.
        self.assertIn("**[00:00:02.000-00:00:03.000] [MIC]**", text)

    def test_records_the_source_digests_it_derived_from(self):
        text = self.write().markdown.read_text(encoding="utf-8")
        for name, digest in self.sources.items():
            self.assertIn(name, text)
            self.assertIn(digest, text)

    def test_does_not_masquerade_as_a_canonical_part(self):
        paths = self.write()
        self.assertFalse(paths.markdown.name.startswith("transcript-part-"))
        parts = sorted(p.name for p in self.directory.glob("transcript-part-*.md"))
        self.assertEqual(parts, ["transcript-part-001.md"])


class StalenessTests(ArtifactTestCase):
    def test_unchanged_sources_are_not_stale(self):
        self.write()
        document = read_document(self.directory)
        self.assertEqual(stale_sources(self.directory, document), ())

    def test_an_edited_transcript_part_is_stale(self):
        self.write()
        document = read_document(self.directory)
        part = self.directory / "transcript-part-001.md"
        part.write_text(
            part.read_text(encoding="utf-8").replace("Yes", "No"), encoding="utf-8"
        )
        self.assertEqual(
            stale_sources(self.directory, document), ("transcript-part-001.md",)
        )

    def test_a_deleted_source_is_stale(self):
        self.write()
        document = read_document(self.directory)
        (self.directory / "transcript-part-001.md").unlink()
        self.assertEqual(
            stale_sources(self.directory, document), ("transcript-part-001.md",)
        )

    def test_read_document_returns_none_when_absent(self):
        self.assertIsNone(read_document(self.directory))


class SplitBlockArtifactTests(unittest.TestCase):
    """A block split by word alignment is rendered as its pieces."""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        write_session(self.directory, [make_event(0, 15_000, "可能太香了。真的好吃。")])
        self.blocks = read_blocks(self.directory)
        self.result = DiarizationResult(
            model="pyannote/speaker-diarization-community-1",
            model_revision="3533c8cf",
            raw_turns=(turn("MIC_SPEAKER_01", 0, 1_100, exclusive=False), turn("MIC_SPEAKER_00", 1_100, 2_000, exclusive=False)),
            exclusive_turns=(turn("MIC_SPEAKER_01", 0, 1_100), turn("MIC_SPEAKER_00", 1_100, 2_000)),
        )
        words = tuple(
            WordTimestamp(c, 100 + i * 200, 300 + i * 200) for i, c in enumerate("可能太香了真的好吃")
        )
        self.labels = reconcile(
            self.blocks,
            exclusive_turns=self.result.exclusive_turns,
            raw_turns=self.result.raw_turns,
            source=Source.MICROPHONE,
            alignments={(self.blocks[0].part, self.blocks[0].block): words},
        )
        self.aligner = {"model_id": "Qwen/Qwen3-ForcedAligner-0.6B", "model_revision": "c7cbfc20"}

    def write(self):
        return write_artifacts(
            self.directory,
            session_id=SESSION_ID,
            source=Source.MICROPHONE,
            result=self.result,
            labels=self.labels,
            source_artifacts=part_digests(self.directory),
            created_at=CREATED_AT,
            aligner=self.aligner,
        )

    def test_markdown_shows_one_sub_block_per_piece_with_the_text_intact(self):
        paths = self.write()
        markdown = paths.markdown.read_text(encoding="utf-8")
        self.assertIn("**[00:00:00.100-00:00:01.100] [MIC] [MIC_SPEAKER_01]**\n可能太香了。", markdown)
        self.assertIn("**[00:00:01.100-00:00:01.900] [MIC] [MIC_SPEAKER_00]**\n真的好吃。", markdown)
        self.assertNotIn("[00:00:00.000-00:00:15.000]", markdown)
        self.assertIn('aligner_model_id: "Qwen/Qwen3-ForcedAligner-0.6B"', markdown)

    def test_document_records_pieces_as_ranges_and_the_aligner(self):
        self.write()
        document = read_document(self.directory)
        self.assertEqual(document["producer"]["version"], "2")
        self.assertEqual(document["producer"]["aligner"]["model_id"], "Qwen/Qwen3-ForcedAligner-0.6B")
        segment = document["segments"][0]
        self.assertEqual(segment["unlabeled_reason"], "multiple_speakers")
        self.assertEqual(
            [(p["text_start"], p["text_end"], p["speaker_id"]) for p in segment["pieces"]],
            [(0, 6, "MIC_SPEAKER_01"), (6, 11, "MIC_SPEAKER_00")],
        )
        self.assertNotIn('"text":', json.dumps(document))

    def test_a_canonical_speaker_is_never_split(self):
        write_session(self.directory, [make_event(0, 15_000, "可能太香了。真的好吃。", speaker="Host")])
        self.blocks = read_blocks(self.directory)
        words = tuple(
            WordTimestamp(c, 100 + i * 200, 300 + i * 200) for i, c in enumerate("可能太香了真的好吃")
        )
        self.labels = reconcile(
            self.blocks,
            exclusive_turns=self.result.exclusive_turns,
            raw_turns=self.result.raw_turns,
            source=Source.MICROPHONE,
            alignments={(self.blocks[0].part, self.blocks[0].block): words},
        )
        markdown = self.write().markdown.read_text(encoding="utf-8")
        self.assertIn("[MIC] [Host]**\n可能太香了。真的好吃。", markdown)

    def test_validation_rejects_pieces_that_do_not_tile_the_block(self):
        document = build_document(
            session_id=SESSION_ID,
            source=Source.MICROPHONE,
            result=self.result,
            labels=self.labels,
            source_artifacts=part_digests(self.directory),
            created_at=CREATED_AT,
        )
        validate_document(document)
        document["segments"][0]["pieces"][1]["text_start"] = 7
        with self.assertRaises(ValueError):
            validate_document(document)

    def test_no_aligner_means_no_producer_entry(self):
        self.aligner = None
        self.write()
        self.assertIsNone(read_document(self.directory)["producer"]["aligner"])


if __name__ == "__main__":
    unittest.main()
