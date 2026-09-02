"""End-to-end diarization through the real isolated worker process.

This is the only test that loads Community-1. It is skipped when the weights
are not installed, because they are gated and cannot be committed; point
VOXINK_DIARIZATION_MODEL at a local package to run it from a worktree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

from voxink_core.diarization.artifacts import MARKDOWN_FILENAME, TURNS_FILENAME
from voxink_core.diarization.jobs import WORKING_DIRECTORY, DiarizationJobManager
from voxink_core.diarization.models import JobState

from tests.fixtures.audio import SAMPLE_RATE_HZ, load_speech_sample
from tests.fixtures.transcripts import SESSION_ID, make_event, write_session


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = Path(
    os.environ.get(
        "VOXINK_DIARIZATION_MODEL",
        PROJECT_ROOT / "models" / "pyannote-speaker-diarization-community-1",
    )
)


ALIGNER_PATH = Path(
    os.environ.get(
        "VOXINK_ALIGNER_MODEL",
        PROJECT_ROOT / "models" / "Qwen3-ForcedAligner-0.6B",
    )
)


def _model_installed() -> bool:
    return (MODEL_PATH / "voxink-model-manifest.json").is_file() and (
        MODEL_PATH / "config.yaml"
    ).is_file()


def _aligner_installed() -> bool:
    return (ALIGNER_PATH / "voxink-model-manifest.json").is_file() and (
        ALIGNER_PATH / "model.safetensors"
    ).is_file()


@unittest.skipUnless(_model_installed(), f"Community-1 weights not installed at {MODEL_PATH}")
class RealWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pcm = load_speech_sample()

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        duration_ms = len(self.pcm) // 2 * 1000 // SAMPLE_RATE_HZ
        write_session(
            self.directory,
            [
                make_event(0, duration_ms // 2, "First half of the sample."),
                make_event(duration_ms // 2, duration_ms, "Second half of the sample."),
            ],
        )
        with wave.open(str(self.directory / "microphone-part-001.wav"), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(SAMPLE_RATE_HZ)
            writer.writeframes(self.pcm)

    def test_a_real_worker_produces_valid_artifacts(self):
        manager = DiarizationJobManager(model_path=MODEL_PATH)
        canonical = self.directory / "transcript-part-001.md"
        before = canonical.read_bytes()

        manager.request(self.directory, SESSION_ID)
        manager.wait(SESSION_ID, timeout=600)
        job = manager.get(SESSION_ID)

        self.assertIs(job.state, JobState.READY, job.error)
        self.assertGreaterEqual(job.speaker_count, 1)
        self.assertEqual(job.total_segments, 2)

        document = json.loads((self.directory / TURNS_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "ready")
        self.assertEqual(
            document["producer"]["model_id"], "pyannote/speaker-diarization-community-1"
        )
        self.assertTrue(document["raw_turns"], "no speech was detected in the sample")
        for turn in document["raw_turns"]:
            self.assertTrue(turn["speaker_id"].startswith("MIC_SPEAKER_"))
            self.assertLess(turn["start_ms"], turn["end_ms"])

        self.assertTrue((self.directory / MARKDOWN_FILENAME).is_file())
        self.assertEqual(canonical.read_bytes(), before, "canonical transcript changed")
        self.assertFalse((self.directory / WORKING_DIRECTORY).exists())

    @unittest.skipUnless(_aligner_installed(), f"aligner weights not installed at {ALIGNER_PATH}")
    def test_the_real_aligner_times_words_offline(self):
        """The second stage runs in the same isolated worker, offline.

        The fixture text is not what the sample says, so nothing is asserted
        about where the words land; what matters is that the stage ran, said
        so, and left the block-level result intact.
        """
        manager = DiarizationJobManager(model_path=MODEL_PATH, aligner_path=ALIGNER_PATH)
        self.assertTrue(manager.capability()["alignment_available"])
        manager.request(self.directory, SESSION_ID)
        manager.wait(SESSION_ID, timeout=600)
        job = manager.get(SESSION_ID)
        self.assertIs(job.state, JobState.READY, job.error)
        self.assertIsNone(job.alignment_error)
        document = json.loads((self.directory / TURNS_FILENAME).read_text(encoding="utf-8"))
        aligner = document["producer"]["aligner"]
        self.assertEqual(aligner["model_id"], "Qwen/Qwen3-ForcedAligner-0.6B")
        self.assertTrue(aligner["model_revision"])
        for segment in document["segments"]:
            for piece in segment.get("pieces", ()):
                self.assertNotIn("text", piece)

    def test_weights_load_with_the_hub_forced_offline(self):
        """Succeeding under HF_HUB_OFFLINE=1 is what proves nothing was fetched.

        The hub library raises rather than downloading when that flag is set,
        so a job that reaches `ready` cannot have reached the network for a
        weight it was missing.
        """
        captured: dict[str, str] = {}
        manager = DiarizationJobManager(model_path=MODEL_PATH)
        real_runner = manager._run_worker

        def capture_env(payload, timeout):
            captured.update(
                {key: os.environ.get(key, "") for key in ("HF_HUB_OFFLINE",)}
            )
            return real_runner(payload, timeout)

        manager._runner = capture_env
        manager.request(self.directory, SESSION_ID)
        manager.wait(SESSION_ID, timeout=600)
        self.assertIs(manager.get(SESSION_ID).state, JobState.READY)
        self.assertEqual(captured["HF_HUB_OFFLINE"], "1")

    def test_a_missing_model_fails_without_touching_the_transcript(self):
        manager = DiarizationJobManager(model_path=self.directory / "absent-model")
        canonical = self.directory / "transcript-part-001.md"
        before = canonical.read_bytes()
        manager.request(self.directory, SESSION_ID)
        manager.wait(SESSION_ID, timeout=600)
        job = manager.get(SESSION_ID)
        self.assertIs(job.state, JobState.FAILED)
        self.assertEqual(job.error["code"], "model_unavailable")
        self.assertEqual(canonical.read_bytes(), before)
        self.assertFalse((self.directory / TURNS_FILENAME).exists())

    def test_a_worker_that_exceeds_its_budget_is_terminated(self):
        manager = DiarizationJobManager(
            model_path=MODEL_PATH, min_timeout_seconds=0.05, timeout_factor=0.0001
        )
        manager.request(self.directory, SESSION_ID)
        manager.wait(SESSION_ID, timeout=600)
        job = manager.get(SESSION_ID)
        self.assertIs(job.state, JobState.FAILED)
        self.assertEqual(job.error["code"], "worker_timed_out")
        self.assertFalse((self.directory / WORKING_DIRECTORY).exists())


if __name__ == "__main__":
    unittest.main()
