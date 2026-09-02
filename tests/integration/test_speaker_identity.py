"""Speakers that keep their names: embeddings, the registry, naming, recognition."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from services.transcription.app import ServiceSettings, create_app
from voxink_core.diarization.artifacts import MARKDOWN_FILENAME, TURNS_FILENAME
from voxink_core.diarization.jobs import DiarizationJobManager
from voxink_core.diarization.models import JobState
from voxink_core.speakers import SpeakerRegistry

from tests.fixtures.transcripts import make_event, write_session
from tests.integration.test_transcription_service import FakeProvider
from tests.unit.voxink_core.test_diarization_audio import write_wav


OWNER = "speaker-owner"


def _vector(angle: float, dimension: int = 6) -> list[float]:
    values = [0.0] * dimension
    values[0] = math.cos(angle)
    values[1] = math.sin(angle)
    return values


def _worker_response(embeddings: dict[str, list[float]]):
    def runner(payload, timeout):
        return {
            "ok": True,
            "result": {
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
                "embeddings": embeddings,
            },
        }

    return runner


def _finished_session(root: Path, session_id: str) -> Path:
    directory = root / session_id
    directory.mkdir(parents=True, exist_ok=True)
    write_session(
        directory,
        [
            make_event(0, 1000, "Hello there.", session_id=session_id),
            make_event(1000, 2000, "Hi, welcome.", session_id=session_id),
        ],
        session_id=session_id,
    )
    write_wav(directory / "microphone-part-001.wav", 32_000)
    return directory


class RegistryInJobsTests(unittest.TestCase):
    def test_a_remembered_voice_is_named_and_one_voice_claims_one_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = SpeakerRegistry(root / "speakers.json")
            registry.enroll("Wei", _vector(0.0), model_id="pyannote/speaker-diarization-community-1")
            directory = _finished_session(root, "s1")
            manager = DiarizationJobManager(
                model_path=root / "model",
                registry=registry,
                # Both session speakers resemble Wei; only the closer one gets the name.
                runner=_worker_response({"MIC_SPEAKER_00": _vector(0.1), "MIC_SPEAKER_01": _vector(0.3)}),
            )
            manager.request(directory, "s1")
            manager.wait("s1", timeout=10)
            job = manager.get("s1")
            self.assertIs(job.state, JobState.READY)
            self.assertEqual(job.recognized, {"MIC_SPEAKER_00": "Wei"})
            document = json.loads((directory / TURNS_FILENAME).read_text())
            self.assertEqual(document["speakers"]["MIC_SPEAKER_00"]["name"], "Wei")
            self.assertIsNone(document["speakers"]["MIC_SPEAKER_01"]["name"])
            self.assertEqual(len(document["speakers"]["MIC_SPEAKER_01"]["embedding"]), 6)
            markdown = (directory / MARKDOWN_FILENAME).read_text()
            self.assertIn("[MIC] [Wei]**", markdown)
            self.assertIn("[MIC] [MIC_SPEAKER_01]**", markdown)
            self.assertIn('  MIC_SPEAKER_00: "Wei"', markdown)
            described = manager.describe(directory, "s1")
            self.assertEqual(described["speakers"]["MIC_SPEAKER_00"]["name"], "Wei")
            self.assertNotIn("embedding", described["speakers"]["MIC_SPEAKER_00"])


class NamingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        app = create_app(
            ServiceSettings(
                output_root=self.root / "out",
                state_root=self.root / "state",
                auth_secret=OWNER,
                diarization_model_path=self.root / "absent-model",
                translation_model_path=None,
            ),
            provider=FakeProvider(),
        )
        self.runtime = app.state.runtime
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": f"Bearer {OWNER}"}

    def diarize(self, session_id: str, embeddings: dict[str, list[float]]) -> None:
        self.runtime.diarization._runner = _worker_response(embeddings)
        _finished_session(self.root / "out", session_id)
        response = self.client.post(f"/v1/sessions/{session_id}/diarization", headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.runtime.diarization.wait(session_id, timeout=10)

    def test_name_then_remember_then_recognize_in_the_next_session(self) -> None:
        self.diarize("first", {"MIC_SPEAKER_00": _vector(0.0), "MIC_SPEAKER_01": _vector(1.5)})
        # Name without remembering: the view changes, the registry does not.
        named = self.client.post(
            "/v1/sessions/first/speakers/MIC_SPEAKER_01",
            json={"name": "Guest", "remember": False},
            headers=self.headers,
        )
        self.assertEqual(named.status_code, 200, named.text)
        self.assertIsNone(named.json()["registry_id"])
        self.assertEqual(self.client.get("/v1/speakers", headers=self.headers).json()["speakers"], [])
        markdown = (self.root / "out" / "first" / MARKDOWN_FILENAME).read_text()
        self.assertIn("[MIC] [Guest]**", markdown)
        # Name and remember.
        remembered = self.client.post(
            "/v1/sessions/first/speakers/MIC_SPEAKER_00",
            json={"name": "Wei", "remember": True},
            headers=self.headers,
        )
        self.assertEqual(remembered.status_code, 200, remembered.text)
        registry_id = remembered.json()["registry_id"]
        self.assertTrue(registry_id.startswith("spk_"))
        known = self.client.get("/v1/speakers", headers=self.headers).json()["speakers"]
        self.assertEqual([item["name"] for item in known], ["Wei"])
        self.assertNotIn("embedding", known[0])
        status = self.client.get("/v1/sessions/first/diarization", headers=self.headers).json()
        self.assertEqual(status["speakers"]["MIC_SPEAKER_00"]["name"], "Wei")
        self.assertEqual(status["speakers"]["MIC_SPEAKER_01"]["name"], "Guest")
        # The next session's similar voice is recognized without being asked.
        self.diarize("second", {"MIC_SPEAKER_00": _vector(1.5), "MIC_SPEAKER_01": _vector(0.15)})
        status = self.client.get("/v1/sessions/second/diarization", headers=self.headers).json()
        self.assertEqual(status["speakers"]["MIC_SPEAKER_01"]["name"], "Wei")
        self.assertEqual(status["speakers"]["MIC_SPEAKER_01"]["registry_id"], registry_id)
        self.assertIsNone(status["speakers"]["MIC_SPEAKER_00"]["name"])
        self.assertIn("[MIC] [Wei]**", (self.root / "out" / "second" / MARKDOWN_FILENAME).read_text())
        # Forget the voice: later sessions stop recognizing it.
        self.assertEqual(self.client.delete(f"/v1/speakers/{registry_id}", headers=self.headers).status_code, 200)
        self.diarize("third", {"MIC_SPEAKER_00": _vector(0.1)})
        status = self.client.get("/v1/sessions/third/diarization", headers=self.headers).json()
        self.assertIsNone(status["speakers"]["MIC_SPEAKER_00"]["name"])
        # The canonical transcript never changed.
        self.assertNotIn("Wei", (self.root / "out" / "first" / "transcript-part-001.md").read_text())

    def test_naming_needs_artifacts_and_a_known_speaker(self) -> None:
        self.assertEqual(
            self.client.post("/v1/sessions/none/speakers/MIC_SPEAKER_00", json={"name": "X"}, headers=self.headers).status_code,
            404,
        )
        self.diarize("only", {"MIC_SPEAKER_00": _vector(0.0)})
        self.assertEqual(
            self.client.post("/v1/sessions/only/speakers/MIC_SPEAKER_09", json={"name": "X"}, headers=self.headers).status_code,
            404,
        )
        # MIC_SPEAKER_01 has turns but no embedding: nameable, not rememberable.
        self.assertEqual(
            self.client.post("/v1/sessions/only/speakers/MIC_SPEAKER_01", json={"name": "X", "remember": True}, headers=self.headers).status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/v1/sessions/only/speakers/MIC_SPEAKER_01", json={"name": "X"}, headers=self.headers).status_code,
            200,
        )
        # Grants cannot enrol voices.
        grant = self.client.post("/v1/grants", json={"scopes": ["transcripts.read"]}, headers=self.headers).json()
        self.assertEqual(
            self.client.post(
                "/v1/sessions/only/speakers/MIC_SPEAKER_00", json={"name": "Y"},
                headers={"Authorization": f"Bearer {grant['token']}"},
            ).status_code,
            403,
        )


if __name__ == "__main__":
    unittest.main()
