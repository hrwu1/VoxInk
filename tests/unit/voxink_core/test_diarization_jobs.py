from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from voxink_core.diarization.pyannote import resolve_device
from voxink_core.models import Source
from voxink_core.diarization.artifacts import MARKDOWN_FILENAME, TURNS_FILENAME
from voxink_core.diarization.jobs import WORKING_DIRECTORY, DiarizationJobManager
from voxink_core.diarization.models import (
    DiarizationError,
    DiarizationErrorCode,
    JobState,
)

from tests.fixtures.transcripts import SESSION_ID, make_event, write_session
from tests.unit.voxink_core.test_diarization_audio import write_wav


def worker_result(**overrides):
    payload = {
        "model": "pyannote/speaker-diarization-community-1",
        "model_revision": "3533c8cf",
        "raw_turns": [
            {"speaker_id": "MIC_SPEAKER_00", "start_ms": 0, "end_ms": 1000},
            {"speaker_id": "MIC_SPEAKER_01", "start_ms": 1000, "end_ms": 2000},
        ],
        "exclusive_turns": [
            {"speaker_id": "MIC_SPEAKER_00", "start_ms": 0, "end_ms": 1000},
            {"speaker_id": "MIC_SPEAKER_01", "start_ms": 1000, "end_ms": 2000},
        ],
    }
    payload.update(overrides)
    return {"ok": True, "result": payload}


class JobTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        write_session(
            self.directory,
            [
                make_event(0, 1000, "Could we review the first proposal?"),
                make_event(1000, 2000, "Yes, let's start."),
            ],
        )
        write_wav(self.directory / "microphone-part-001.wav", 32_000)
        self.calls: list[tuple[dict, float]] = []

    def manager(self, response=None, *, runner=None, **kwargs):
        def default_runner(payload, timeout):
            self.calls.append((payload, timeout))
            if isinstance(response, Exception):
                raise response
            return response if response is not None else worker_result()

        return DiarizationJobManager(
            model_path=self.directory / "model",
            runner=runner or default_runner,
            **kwargs,
        )

    def run_job(self, manager, **kwargs):
        manager.request(self.directory, SESSION_ID, **kwargs)
        manager.wait(SESSION_ID, timeout=10)
        return manager.get(SESSION_ID)


class SuccessTests(JobTestCase):
    def test_produces_both_artifacts_and_reports_ready(self):
        job = self.run_job(self.manager())
        self.assertIs(job.state, JobState.READY)
        self.assertTrue((self.directory / TURNS_FILENAME).is_file())
        self.assertTrue((self.directory / MARKDOWN_FILENAME).is_file())
        self.assertEqual(job.speaker_count, 2)
        self.assertEqual(job.total_segments, 2)
        self.assertEqual(job.labeled_segments, 2)

    def test_stop_does_not_wait_for_the_model(self):
        """The point of the whole design: dispatch returns before work starts."""
        release = threading.Event()
        started = threading.Event()

        def blocking_runner(payload, timeout):
            started.set()
            release.wait(10)
            return worker_result()

        manager = self.manager(runner=blocking_runner)
        job = manager.request(self.directory, SESSION_ID)
        self.assertTrue(started.wait(5))
        # The worker is mid-flight and dispatch has already returned.
        self.assertIs(job.state, JobState.PROCESSING)
        # The transcript is readable the whole time.
        self.assertIn(
            "Yes, let's start.",
            (self.directory / "transcript-part-001.md").read_text(encoding="utf-8"),
        )
        release.set()
        manager.wait(SESSION_ID, timeout=10)
        self.assertIs(manager.get(SESSION_ID).state, JobState.READY)

    def test_the_worker_receives_the_joined_audio_and_a_budget(self):
        self.run_job(self.manager())
        payload, timeout = self.calls[0]
        self.assertTrue(payload["audio_path"].endswith("microphone-joined.wav"))
        self.assertEqual(payload["source"], "microphone")
        self.assertEqual(payload["device"], "auto")
        self.assertGreaterEqual(timeout, 600.0)

    def test_the_timeout_scales_with_a_long_recording(self):
        manager = self.manager(min_timeout_seconds=1.0, timeout_factor=2.0)
        self.run_job(manager)
        _, timeout = self.calls[0]
        # Two seconds of audio, factor two.
        self.assertAlmostEqual(timeout, 4.0)

    def test_temporary_audio_is_removed(self):
        self.run_job(self.manager())
        self.assertFalse((self.directory / WORKING_DIRECTORY).exists())

    def test_canonical_transcript_bytes_never_change(self):
        part = self.directory / "transcript-part-001.md"
        before = part.read_bytes()
        self.run_job(self.manager())
        self.assertEqual(part.read_bytes(), before)

    def test_retained_audio_is_never_deleted(self):
        self.run_job(self.manager())
        self.assertTrue((self.directory / "microphone-part-001.wav").is_file())


class FailureTests(JobTestCase):
    def test_a_typed_worker_error_is_preserved(self):
        job = self.run_job(
            self.manager(
                {
                    "ok": False,
                    "error": {
                        "code": "model_unavailable",
                        "message": "weights are missing",
                        "metadata": {},
                    },
                }
            )
        )
        self.assertIs(job.state, JobState.FAILED)
        self.assertEqual(job.error["code"], "model_unavailable")

    def test_a_timeout_is_reported_as_such(self):
        job = self.run_job(
            self.manager(
                DiarizationError(DiarizationErrorCode.WORKER_TIMED_OUT, "over budget")
            )
        )
        self.assertIs(job.state, JobState.FAILED)
        self.assertEqual(job.error["code"], "worker_timed_out")

    def test_an_unexpected_crash_does_not_escape_the_job(self):
        job = self.run_job(self.manager(RuntimeError("segfault surrogate")))
        self.assertIs(job.state, JobState.FAILED)
        self.assertEqual(job.error["code"], "worker_crashed")

    def test_failure_leaves_the_transcript_intact_and_no_artifacts(self):
        part = self.directory / "transcript-part-001.md"
        before = part.read_bytes()
        self.run_job(self.manager(RuntimeError("boom")))
        self.assertEqual(part.read_bytes(), before)
        self.assertFalse((self.directory / TURNS_FILENAME).exists())
        self.assertFalse((self.directory / MARKDOWN_FILENAME).exists())

    def test_temporary_audio_is_removed_after_failure(self):
        self.run_job(self.manager(RuntimeError("boom")))
        self.assertFalse((self.directory / WORKING_DIRECTORY).exists())

    def test_missing_retained_audio_fails_before_any_worker_runs(self):
        (self.directory / "microphone-part-001.wav").unlink()
        job = self.run_job(self.manager())
        self.assertIs(job.state, JobState.FAILED)
        self.assertEqual(job.error["code"], "audio_unavailable")
        self.assertEqual(self.calls, [])


class OwnershipTests(JobTestCase):
    def test_a_second_request_joins_the_running_job(self):
        release = threading.Event()
        started = threading.Event()

        def blocking_runner(payload, timeout):
            self.calls.append((payload, timeout))
            started.set()
            release.wait(10)
            return worker_result()

        manager = self.manager(runner=blocking_runner)
        first = manager.request(self.directory, SESSION_ID)
        self.assertTrue(started.wait(5))
        second = manager.request(self.directory, SESSION_ID)
        self.assertIs(first, second)
        release.set()
        manager.wait(SESSION_ID, timeout=10)
        self.assertEqual(len(self.calls), 1, "a duplicate worker was started")

    def test_a_completed_job_is_not_redone_unless_asked(self):
        manager = self.manager()
        self.run_job(manager)
        manager.request(self.directory, SESSION_ID)
        manager.wait(SESSION_ID, timeout=10)
        self.assertEqual(len(self.calls), 1)

    def test_regenerate_runs_it_again(self):
        manager = self.manager()
        self.run_job(manager)
        self.run_job(manager, regenerate=True)
        self.assertEqual(len(self.calls), 2)

    def test_a_disabled_manager_never_dispatches(self):
        manager = self.manager(enabled=False)
        job = manager.request(self.directory, SESSION_ID)
        self.assertIs(job.state, JobState.DISABLED)
        self.assertEqual(self.calls, [])


class DescribeTests(JobTestCase):
    def test_reports_disabled_when_nothing_has_happened(self):
        manager = self.manager()
        state = manager.describe(self.directory, SESSION_ID)
        self.assertEqual(state["state"], "disabled")

    def test_reads_a_previous_run_back_from_disk(self):
        """Artifacts outlive the process; a restart still reports ready."""
        self.run_job(self.manager())
        fresh = self.manager()
        state = fresh.describe(self.directory, SESSION_ID)
        self.assertEqual(state["state"], "ready")
        self.assertEqual(state["total_segments"], 2)
        self.assertEqual(sorted(state["artifacts"]), sorted([MARKDOWN_FILENAME, TURNS_FILENAME]))

    def test_an_edited_transcript_makes_the_artifact_stale(self):
        self.run_job(self.manager())
        part = self.directory / "transcript-part-001.md"
        part.write_text(
            part.read_text(encoding="utf-8").replace("Yes", "No"), encoding="utf-8"
        )
        state = self.manager().describe(self.directory, SESSION_ID)
        self.assertEqual(state["state"], "stale")
        self.assertEqual(state["error"]["code"], "artifact_stale")

    def test_a_failed_job_is_reported_as_failed(self):
        manager = self.manager(RuntimeError("boom"))
        self.run_job(manager)
        self.assertEqual(manager.describe(self.directory, SESSION_ID)["state"], "failed")


class CapabilityTests(JobTestCase):
    def test_reports_unavailable_without_weights(self):
        capability = self.manager().capability()
        self.assertFalse(capability["available"])
        self.assertEqual(capability["error"]["code"], "model_unavailable")

    def test_reports_disabled_when_switched_off(self):
        capability = self.manager(enabled=False).capability()
        self.assertFalse(capability["enabled"])
        self.assertFalse(capability["available"])

    def test_reports_the_device_that_will_actually_be_used(self):
        capability = self.manager().capability()
        self.assertEqual(capability["device"], "auto")
        self.assertIn(capability["resolved_device"], {"cpu", "mps", "cuda"})

    def test_an_explicit_device_is_reported_unchanged(self):
        capability = self.manager(device="cpu").capability()
        self.assertEqual(capability["device"], "cpu")
        self.assertEqual(capability["resolved_device"], "cpu")


class DeviceResolutionTests(unittest.TestCase):
    def test_an_explicit_preference_is_never_overridden(self):
        self.assertEqual(resolve_device("cpu"), "cpu")
        self.assertEqual(resolve_device("mps"), "mps")
        self.assertEqual(resolve_device("cuda"), "cuda")

    def test_auto_resolves_to_a_device_that_exists(self):
        self.assertIn(resolve_device("auto"), {"cpu", "mps"})

    def test_auto_falls_back_to_cpu_when_probing_fails(self):
        """An accelerator that cannot be probed must not fail the job."""
        with mock.patch.dict(sys.modules, {"torch": None}):
            self.assertEqual(resolve_device("auto"), "cpu")


if __name__ == "__main__":
    unittest.main()
