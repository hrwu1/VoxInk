"""The published `voxink diarization` commands, against a real loopback helper."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest

from services.transcription.app import PROTOCOL_VERSION, ServiceSettings, create_app
from voxink_cli import discovery
from voxink_cli.cli import main
from voxink_cli.envelope import EXIT_NOT_FOUND, EXIT_OK, SCHEMA_VERSION

from tests.fixtures.transcripts import make_event, write_session
from tests.integration.test_public_cli import AUTH_SECRET, _LiveHelper
from tests.integration.test_transcription_service import FakeProvider
from tests.unit.voxink_core.test_diarization_audio import write_wav


SESSION_ID = "2026-08-31_14-30-00-000000"


def fake_worker(payload, timeout):
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
        },
    }


class DiarizationCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.output_root = self.root / "transcripts"
        self.output_root.mkdir()
        self.endpoint_path = self.root / "control-endpoint.json"

        app = create_app(
            ServiceSettings(output_root=self.output_root, auth_secret=AUTH_SECRET),
            provider=FakeProvider(),
        )
        self.runtime = app.state.runtime
        self.runtime.diarization._runner = fake_worker
        self.helper = _LiveHelper(app)
        self.helper.start()
        self.addCleanup(self.helper.stop)
        discovery.publish(
            self.endpoint_path,
            base_url=self.helper.base_url,
            auth_secret=AUTH_SECRET,
            protocol_version=str(PROTOCOL_VERSION),
        )

    def _run(self, *argv: str) -> tuple[int, list[dict], str]:
        out, err = io.StringIO(), io.StringIO()
        code = main(
            [*argv, "--format", "json", "--endpoint-file", str(self.endpoint_path)],
            stdout=out,
            stderr=err,
        )
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line]
        return code, lines, err.getvalue()

    def _ok(self, *argv: str) -> dict:
        code, envelopes, stderr = self._run(*argv)
        self.assertEqual(EXIT_OK, code, f"{argv} failed: {envelopes} {stderr}")
        self.assertTrue(envelopes[0]["ok"], envelopes[0])
        self.assertEqual(SCHEMA_VERSION, envelopes[0]["schema_version"])
        return envelopes[0]

    def _prepare_session(self) -> Path:
        directory = self.output_root / SESSION_ID
        directory.mkdir(parents=True)
        write_session(
            directory,
            [
                make_event(0, 1000, "First speaker."),
                make_event(1000, 2000, "Second speaker."),
            ],
        )
        write_wav(directory / "microphone-part-001.wav", 32_000)
        return directory

    def test_capability_reports_that_weights_are_missing(self):
        envelope = self._ok("diarization", "capability")
        self.assertFalse(envelope["data"]["available"])
        self.assertEqual(envelope["data"]["error"]["code"], "model_unavailable")
        self.assertTrue(envelope["warnings"])

    def test_show_reports_disabled_for_an_untouched_session(self):
        self._prepare_session()
        envelope = self._ok("diarization", "show", SESSION_ID)
        self.assertEqual(envelope["data"]["state"], "disabled")

    def test_run_then_show_reports_ready_and_names_the_artifacts(self):
        self._prepare_session()
        self._ok("diarization", "run", SESSION_ID, "--wait")
        envelope = self._ok("diarization", "show", SESSION_ID)
        data = envelope["data"]
        self.assertEqual(data["state"], "ready")
        self.assertEqual(data["speaker_count"], 2)
        self.assertEqual(data["labeled_segments"], 2)
        # Artifact names come from the service, so no client hard-codes them.
        self.assertEqual(
            sorted(data["artifacts"]),
            ["speaker-turns.json", "transcript-with-speakers.md"],
        )

    def test_run_waits_for_the_job_to_settle(self):
        self._prepare_session()
        envelope = self._ok("diarization", "run", SESSION_ID, "--wait")
        self.assertEqual(envelope["data"]["state"], "ready")

    def test_run_without_wait_returns_while_processing(self):
        self._prepare_session()
        envelope = self._ok("diarization", "run", SESSION_ID)
        self.assertIn(envelope["data"]["state"], {"processing", "ready"})

    def test_an_unknown_session_reports_not_found(self):
        code, envelopes, _ = self._run("diarization", "run", "no-such-session")
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertFalse(envelopes[0]["ok"])

    def test_a_stale_artifact_is_surfaced_as_a_warning(self):
        directory = self._prepare_session()
        self._ok("diarization", "run", SESSION_ID, "--wait")
        part = directory / "transcript-part-001.md"
        part.write_text(
            part.read_text(encoding="utf-8").replace("First", "Third"), encoding="utf-8"
        )
        envelope = self._ok("diarization", "show", SESSION_ID)
        self.assertEqual(envelope["data"]["state"], "stale")
        self.assertTrue(
            any("artifact_stale" in warning for warning in envelope["warnings"])
        )

    def test_regenerate_refreshes_a_stale_artifact(self):
        directory = self._prepare_session()
        self._ok("diarization", "run", SESSION_ID, "--wait")
        part = directory / "transcript-part-001.md"
        part.write_text(
            part.read_text(encoding="utf-8").replace("First", "Third"), encoding="utf-8"
        )
        envelope = self._ok(
            "diarization", "run", SESSION_ID, "--regenerate", "--wait"
        )
        self.assertEqual(envelope["data"]["state"], "ready")

    def test_text_output_explains_the_state(self):
        self._prepare_session()
        out, err = io.StringIO(), io.StringIO()
        main(
            [
                "diarization",
                "run",
                SESSION_ID,
                "--wait",
                "--endpoint-file",
                str(self.endpoint_path),
            ],
            stdout=out,
            stderr=err,
        )
        text = out.getvalue()
        self.assertIn("state: ready", text)
        self.assertIn("Speaker artifacts are ready.", text)


if __name__ == "__main__":
    unittest.main()
