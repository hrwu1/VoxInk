"""The service surface for diarization: capability, dispatch, and status."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import wave

from fastapi.testclient import TestClient

from services.transcription.app import ServiceSettings, create_app
from voxink_core.diarization.artifacts import MARKDOWN_FILENAME, TURNS_FILENAME
from voxink_core.models import Source, TranscriptEvent

from tests.integration.test_transcription_service import FakeProvider
from tests.fixtures.transcripts import make_event, write_session
from tests.unit.voxink_core.test_diarization_audio import write_wav


SESSION_ID = "2026-08-31_14-30-00-000000"


def worker_response(payload, timeout):
    return {
        "ok": True,
        "result": {
            "model": "pyannote/speaker-diarization-community-1",
            "model_revision": "3533c8cf",
            "raw_turns": [
                {"speaker_id": "MIC_SPEAKER_00", "start_ms": 0, "end_ms": 2000}
            ],
            "exclusive_turns": [
                {"speaker_id": "MIC_SPEAKER_00", "start_ms": 0, "end_ms": 2000}
            ],
        },
    }


class DiarizationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output_root = Path(self.temporary.name) / "outputs"
        app = create_app(
            ServiceSettings(
                output_root=self.output_root,
                # Pointed at nothing on purpose. These tests drive the job with
                # a fake worker, and the real weights are an optional download,
                # so reading the project's models directory would make the
                # result depend on whether this machine happens to have them.
                diarization_model_path=Path(self.temporary.name) / "absent-model",
            ),
            provider=FakeProvider(),
        )
        self.runtime = app.state.runtime
        self.runtime.diarization._runner = worker_response
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def session_directory(self, session_id: str = SESSION_ID) -> Path:
        return self.output_root / session_id

    def prepare_finished_session(self, session_id: str = SESSION_ID) -> Path:
        directory = self.session_directory(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        write_session(
            directory,
            [make_event(0, 1000, "Hello.", session_id=session_id)],
            session_id=session_id,
        )
        write_wav(directory / "microphone-part-001.wav", 32_000)
        return directory

    # -- capability ----------------------------------------------------------

    def test_capability_reports_missing_weights_without_crashing(self):
        response = self.client.get("/v1/diarization")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertFalse(body["available"])
        self.assertEqual(body["error"]["code"], "model_unavailable")

    # -- opt-in --------------------------------------------------------------

    def test_a_session_cannot_ask_for_diarization_without_retained_audio(self):
        response = self.client.post(
            "/v1/sessions", json={"sources": ["microphone"], "diarize": True}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("audio_saved", response.json()["detail"])

    def test_a_session_can_opt_in_with_retained_audio(self):
        response = self.client.post(
            "/v1/sessions",
            json={
                "sources": ["microphone"],
                "audio_saved": True,
                "save_separate_audio": True,
                "diarize": True,
            },
        )
        self.assertEqual(response.status_code, 201)

    def test_diarization_defaults_to_off(self):
        response = self.client.post("/v1/sessions", json={"sources": ["microphone"]})
        session_id = response.json()["session_id"]
        self.assertFalse(self.runtime.sessions.get(session_id).config.diarize)

    # -- dispatch ------------------------------------------------------------

    def test_stop_dispatches_a_job_and_returns_immediately(self):
        created = self.client.post(
            "/v1/sessions",
            json={
                "sources": ["microphone"],
                "audio_saved": True,
                "save_separate_audio": True,
                "diarize": True,
            },
        )
        session_id = created.json()["session_id"]
        directory = self.prepare_finished_session(session_id)
        self.client.post(f"/v1/sessions/{session_id}/start")

        stop = self.client.post(f"/v1/sessions/{session_id}/stop")
        self.assertEqual(stop.status_code, 200)
        self.assertEqual(stop.json()["state"], "stopped")

        self.runtime.diarization.wait(session_id, timeout=10)
        status = self.client.get(f"/v1/sessions/{session_id}/diarization").json()
        self.assertEqual(status["state"], "ready")
        self.assertTrue((directory / TURNS_FILENAME).is_file())
        self.assertTrue((directory / MARKDOWN_FILENAME).is_file())

    def test_stop_does_not_dispatch_when_diarization_was_not_requested(self):
        created = self.client.post(
            "/v1/sessions",
            json={
                "sources": ["microphone"],
                "audio_saved": True,
                "save_separate_audio": True,
            },
        )
        session_id = created.json()["session_id"]
        self.prepare_finished_session(session_id)
        self.client.post(f"/v1/sessions/{session_id}/start")
        self.client.post(f"/v1/sessions/{session_id}/stop")
        status = self.client.get(f"/v1/sessions/{session_id}/diarization").json()
        self.assertEqual(status["state"], "disabled")

    # -- explicit request ----------------------------------------------------

    def test_an_unknown_session_is_not_found(self):
        response = self.client.post("/v1/sessions/does-not-exist/diarization")
        self.assertEqual(response.status_code, 404)

    def test_a_stopped_session_can_be_diarized_on_request(self):
        directory = self.prepare_finished_session()
        response = self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "processing")
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        self.assertTrue((directory / TURNS_FILENAME).is_file())

    def test_a_running_session_is_refused(self):
        created = self.client.post(
            "/v1/sessions",
            json={
                "sources": ["microphone"],
                "audio_saved": True,
                "save_separate_audio": True,
            },
        )
        session_id = created.json()["session_id"]
        self.prepare_finished_session(session_id)
        self.client.post(f"/v1/sessions/{session_id}/start")
        response = self.client.post(f"/v1/sessions/{session_id}/diarization")
        self.assertEqual(response.status_code, 409)

    def test_the_control_action_route_still_works(self):
        """`/diarization` must not be swallowed by the generic action route."""
        response = self.client.post(f"/v1/sessions/{SESSION_ID}/unknown-action")
        self.assertEqual(response.status_code, 404)

    # -- segments for the caption view ---------------------------------------

    def test_ready_status_carries_segment_times_and_speakers(self):
        """The UI matches these to captions it already shows, by time."""
        self.prepare_finished_session()
        self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        status = self.client.get(f"/v1/sessions/{SESSION_ID}/diarization").json()
        segments = status["segments"]
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start_ms"], 0)
        self.assertEqual(segments[0]["end_ms"], 1000)
        self.assertEqual(segments[0]["speaker_id"], "MIC_SPEAKER_00")

    def test_status_segments_never_carry_transcript_text(self):
        self.prepare_finished_session()
        self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        status = self.client.get(f"/v1/sessions/{SESSION_ID}/diarization").json()
        for segment in status["segments"]:
            self.assertEqual(
                set(segment), {"start_ms", "end_ms", "speaker_id", "overlap_detected", "pieces"}
            )
            for piece in segment["pieces"]:
                self.assertEqual(
                    set(piece), {"text_start", "text_end", "start_ms", "end_ms", "speaker_id"}
                )

    # -- the UI capture opt-in -----------------------------------------------

    def test_native_capture_records_the_diarization_request(self):
        self.runtime.settings.native_capture_path
        self.runtime.pending_diarization.clear()
        # Exercised directly: starting real native capture needs the Swift
        # helper and a microphone permission prompt.
        self.runtime.pending_diarization.add(SESSION_ID)
        from services.transcription.app import _diarization_requested

        self.assertTrue(
            _diarization_requested(
                self.runtime, SESSION_ID, (Source.MICROPHONE,),
                {"audio_saved": True, "save_separate_audio": True},
            )
        )

    def test_the_request_is_dropped_when_retention_cannot_support_it(self):
        from services.transcription.app import _diarization_requested

        self.runtime.pending_diarization.add(SESSION_ID)
        for retention in (
            {"audio_saved": False, "save_separate_audio": True},
            {"audio_saved": True, "save_separate_audio": False},
        ):
            with self.subTest(**retention):
                # Dropped rather than honoured: SessionConfig would reject the
                # whole session, failing a recording over an optional extra.
                self.assertFalse(
                    _diarization_requested(
                        self.runtime, SESSION_ID, (Source.MICROPHONE,), retention
                    )
                )

    def test_a_session_that_never_asked_does_not_diarize(self):
        from services.transcription.app import _diarization_requested

        self.runtime.pending_diarization.clear()
        self.assertFalse(
            _diarization_requested(
                self.runtime, SESSION_ID, (Source.MICROPHONE,),
                {"audio_saved": True, "save_separate_audio": True},
            )
        )

    # -- status --------------------------------------------------------------

    def test_status_reports_ready_from_disk_and_names_no_filenames_to_guess(self):
        self.prepare_finished_session()
        self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        status = self.client.get(f"/v1/sessions/{SESSION_ID}/diarization").json()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(
            sorted(status["artifacts"]), sorted([TURNS_FILENAME, MARKDOWN_FILENAME])
        )

    def test_status_reports_stale_after_the_transcript_changes(self):
        directory = self.prepare_finished_session()
        self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        part = directory / "transcript-part-001.md"
        part.write_text(
            part.read_text(encoding="utf-8").replace("Hello", "Goodbye"), encoding="utf-8"
        )
        status = self.client.get(f"/v1/sessions/{SESSION_ID}/diarization").json()
        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["error"]["code"], "artifact_stale")

    def test_regenerating_refreshes_a_stale_artifact(self):
        directory = self.prepare_finished_session()
        self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        part = directory / "transcript-part-001.md"
        part.write_text(
            part.read_text(encoding="utf-8").replace("Hello", "Goodbye"), encoding="utf-8"
        )
        response = self.client.post(
            f"/v1/sessions/{SESSION_ID}/diarization", json={"regenerate": True}
        )
        self.assertEqual(response.status_code, 202)
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        status = self.client.get(f"/v1/sessions/{SESSION_ID}/diarization").json()
        self.assertEqual(status["state"], "ready")

    def test_transcript_access_never_waits_for_diarization(self):
        """The transcript is readable while a job is still running."""
        import threading

        release = threading.Event()

        def blocking(payload, timeout):
            release.wait(10)
            return worker_response(payload, timeout)

        self.runtime.diarization._runner = blocking
        self.prepare_finished_session()
        self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        transcript = self.client.get(f"/v1/transcripts/{SESSION_ID}")
        self.assertEqual(transcript.status_code, 200)
        self.assertIn("Hello.", transcript.json()["text"])
        release.set()
        self.runtime.diarization.wait(SESSION_ID, timeout=10)

    def test_a_failed_job_leaves_the_transcript_readable(self):
        self.runtime.diarization._runner = lambda payload, timeout: {
            "ok": False,
            "error": {"code": "model_unavailable", "message": "no weights", "metadata": {}},
        }
        directory = self.prepare_finished_session()
        before = (directory / "transcript-part-001.md").read_bytes()
        self.client.post(f"/v1/sessions/{SESSION_ID}/diarization")
        self.runtime.diarization.wait(SESSION_ID, timeout=10)
        status = self.client.get(f"/v1/sessions/{SESSION_ID}/diarization").json()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"]["code"], "model_unavailable")
        self.assertEqual((directory / "transcript-part-001.md").read_bytes(), before)
        self.assertEqual(
            self.client.get(f"/v1/transcripts/{SESSION_ID}").status_code, 200
        )


if __name__ == "__main__":
    unittest.main()
