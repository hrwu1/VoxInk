"""`voxink speakers`: naming a session's speakers and managing remembered voices."""

from __future__ import annotations

import math

from tests.fixtures.transcripts import make_event, write_session
from tests.integration.test_public_cli import _CommandLineHarness
from tests.unit.voxink_core.test_diarization_audio import write_wav
from voxink_cli.envelope import EXIT_NOT_FOUND


class SpeakerCommandTests(_CommandLineHarness):
    def test_name_remember_list_rename_and_forget(self) -> None:
        session_id = "named-session"
        directory = self.output_root / session_id
        directory.mkdir()
        write_session(directory, [make_event(0, 1000, "Hello.", session_id=session_id)], session_id=session_id)
        write_wav(directory / "microphone-part-001.wav", 32_000)
        vector = [math.cos(0.1), math.sin(0.1), 0.0, 0.0]
        runtime = self.helper._server.config.app.state.runtime
        runtime.diarization._runner = lambda payload, timeout: {
            "ok": True,
            "result": {
                "model": "pyannote/speaker-diarization-community-1",
                "model_revision": "3533c8cf",
                "raw_turns": [{"speaker_id": "MIC_SPEAKER_00", "start_ms": 0, "end_ms": 1000}],
                "exclusive_turns": [{"speaker_id": "MIC_SPEAKER_00", "start_ms": 0, "end_ms": 1000}],
                "embeddings": {"MIC_SPEAKER_00": vector},
            },
        }
        self._ok("diarization", "run", session_id, "--wait")
        named = self._ok("speakers", "name", session_id, "MIC_SPEAKER_00", "Wei", "--remember")["data"]
        self.assertEqual("Wei", named["name"])
        registry_id = named["registry_id"]
        listed = self._ok("speakers", "list")["data"]["speakers"]
        self.assertEqual([item["id"] for item in listed], [registry_id])
        self._ok("speakers", "rename", registry_id, "Wei Zhang")
        self._ok("speakers", "forget", registry_id)
        self.assertEqual(self._ok("speakers", "list")["data"]["speakers"], [])
        code, _ = self._error("speakers", "forget", registry_id)
        self.assertEqual(EXIT_NOT_FOUND, code)
