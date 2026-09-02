"""Consent-gated agent access: grants, scopes, approval flow, deadline, audit."""

from __future__ import annotations

from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from services.transcription.app import ServiceSettings, create_app
from tests.integration.test_transcription_service import FakeProvider


OWNER = "owner-secret"


class FakeProcess:
    """A capture helper that runs until it is signalled, like the real one."""

    def __init__(self) -> None:
        self.pid = 4321
        self.returncode: int | None = None
        self.stderr = None
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        # Pause and resume are SIGUSR1/SIGUSR2; only an interrupt ends it.
        if signum in {signal.SIGINT, signal.SIGTERM}:
            self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0


class GrantServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.native = root / "voxink-capture"
        self.native.touch()
        self.native.chmod(0o755)
        app = create_app(
            ServiceSettings(
                output_root=root / "out",
                state_root=root / "state",
                auth_secret=OWNER,
                native_capture_path=self.native,
                translation_model_path=None,
            ),
            provider=FakeProvider(),
        )
        self.runtime = app.state.runtime
        # Entered as a context manager so one event loop lives for the whole
        # test: background work such as the grant deadline runs on it.
        self.client = TestClient(app).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def owner(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {OWNER}"}

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def issue(self, **kwargs) -> tuple[dict, str]:
        body = {"label": "Claude", "scopes": ["capture.system"], **kwargs}
        response = self.client.post("/v1/grants", json=body, headers=self.owner())
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        return payload["grant"], payload["token"]

    def fake_process(self) -> MagicMock:
        return FakeProcess()

    # -- who may do what --------------------------------------------------------

    def test_grant_tokens_authenticate_but_only_the_owner_manages_grants(self) -> None:
        grant, token = self.issue()
        self.assertEqual(self.client.get("/v1/grants", headers=self.owner()).json()["grants"][0]["id"], grant["id"])
        refused = self.client.get("/v1/grants", headers=self.bearer(token))
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["detail"]["code"], "permission_denied")
        self.assertEqual(self.client.post("/v1/grants", json={"scopes": ["sessions.read"]}, headers=self.bearer(token)).status_code, 403)
        self.assertEqual(self.client.get("/v1/models", headers=self.bearer("vxg_bogus")).status_code, 401)
        # Discovery is open to a grant.
        self.assertEqual(self.client.get("/v1/ui/capture/capabilities", headers=self.bearer(token)).status_code, 200)
        self.assertEqual(self.client.get("/v1/ui/capture/capabilities").status_code, 200)

    def test_a_grant_is_held_to_its_scopes_and_a_refusal_files_a_request(self) -> None:
        grant, token = self.issue(scopes=["capture.system"])
        refused = self.client.get("/v1/transcripts", headers=self.bearer(token))
        self.assertEqual(refused.status_code, 403)
        detail = refused.json()["detail"]
        self.assertEqual(detail["code"], "approval_required")
        self.assertEqual(detail["missing_scopes"], ["transcripts.read"])
        self.assertTrue(detail["request_id"].startswith("req_"))
        pending = self.client.get("/v1/grants/requests", headers=self.owner()).json()["requests"]
        self.assertEqual([item["id"] for item in pending], [detail["request_id"]])
        self.assertEqual(pending[0]["scopes"], ["transcripts.read"])
        # Approve it, claim it with the secret the refusal carried, and read.
        approved = self.client.post(
            f"/v1/grants/requests/{detail['request_id']}/approve", json={"single_use": False}, headers=self.owner()
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        claimed = self.client.post(
            f"/v1/grants/requests/{detail['request_id']}/claim", json={"claim_secret": detail["claim_secret"]}
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        new_token = claimed.json()["token"]
        self.assertEqual(self.client.get("/v1/transcripts", headers=self.bearer(new_token)).status_code, 200)

    def test_capture_start_needs_the_matching_scopes_and_honours_the_allow_list(self) -> None:
        grant, token = self.issue(scopes=["capture.applications"], applications=["us.zoom.xos"])
        with patch("services.transcription.app.subprocess.Popen", return_value=self.fake_process()) as spawned:
            # Microphone is not in the grant.
            refused = self.client.post(
                "/v1/ui/capture/start", json={"sources": ["microphone"]}, headers=self.bearer(token)
            )
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["detail"]["missing_scopes"], ["capture.microphone"])
            # Safari is not on the allow-list.
            refused = self.client.post(
                "/v1/ui/capture/start",
                json={"sources": ["system"], "applications": ["com.apple.Safari"]},
                headers=self.bearer(token),
            )
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["detail"]["code"], "permission_denied")
            # Retaining audio needs its own scope.
            refused = self.client.post(
                "/v1/ui/capture/start",
                json={"sources": ["system"], "applications": ["us.zoom.xos"], "retain_audio": True},
                headers=self.bearer(token),
            )
            self.assertEqual(refused.json()["detail"]["missing_scopes"], ["audio.retain"])
            started = self.client.post(
                "/v1/ui/capture/start",
                json={"sources": ["system"], "applications": ["us.zoom.xos"]},
                headers=self.bearer(token),
            )
            self.assertEqual(started.status_code, 200, started.text)
            self.assertIn("us.zoom.xos", spawned.call_args.args[0])
            self.assertEqual(self.runtime.capture_grant.id, grant["id"])
            # The grant may control what it started.
            self.assertEqual(self.client.post("/v1/ui/capture/pause", headers=self.bearer(token)).status_code, 200)
            self.assertEqual(self.client.post("/v1/ui/capture/stop", headers=self.bearer(token)).status_code, 200)
        operations = [entry["operation"] for entry in self.client.get("/v1/audit", headers=self.owner()).json()["entries"]]
        self.assertEqual(operations.count("capture.start"), 4)
        self.assertIn("capture.pause", operations)
        self.assertIn("capture.stop", operations)
        self.assertEqual(self.client.get("/v1/audit", headers=self.bearer(token)).status_code, 403)

    def test_single_use_grants_are_consumed_by_starting(self) -> None:
        grant, token = self.issue(scopes=["capture.system"], single_use=True)
        with patch("services.transcription.app.subprocess.Popen", return_value=self.fake_process()):
            self.assertEqual(
                self.client.post("/v1/ui/capture/start", json={"sources": ["system"]}, headers=self.bearer(token)).status_code,
                200,
            )
            self.client.post("/v1/ui/capture/stop", headers=self.owner())
            self.assertEqual(
                self.client.post("/v1/ui/capture/start", json={"sources": ["system"]}, headers=self.bearer(token)).status_code,
                401,
            )

    def test_the_maximum_duration_stops_the_capture(self) -> None:
        grant, token = self.issue(scopes=["capture.system"], max_duration_s=1)
        process = self.fake_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process):
            started = self.client.post("/v1/ui/capture/start", json={"sources": ["system"]}, headers=self.bearer(token))
            self.assertEqual(started.status_code, 200, started.text)
            deadline = time.monotonic() + 5.0
            while self.runtime.native_capture_state == "recording" and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertNotEqual(self.runtime.native_capture_state, "recording")
            self.assertTrue(process.signals)
        stops = [
            entry for entry in self.client.get("/v1/audit", headers=self.owner()).json()["entries"]
            if entry["operation"] == "capture.stop" and entry["client"] == "deadline"
        ]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["details"]["max_duration_s"], 1)

    def test_revoking_the_grant_stops_its_capture(self) -> None:
        grant, token = self.issue(scopes=["capture.system"])
        process = self.fake_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process):
            self.client.post("/v1/ui/capture/start", json={"sources": ["system"]}, headers=self.bearer(token))
            revoked = self.client.delete(f"/v1/grants/{grant['id']}", headers=self.owner())
            self.assertEqual(revoked.status_code, 200)
            self.assertTrue(process.signals)
        self.assertEqual(self.client.post("/v1/ui/capture/mark", json={}, headers=self.bearer(token)).status_code, 401)

    def test_anyone_on_loopback_may_ask_and_the_owner_may_deny(self) -> None:
        asked = self.client.post(
            "/v1/grants/requests",
            json={"client": "Claude Code", "reason": "notes", "scopes": ["capture.system"], "max_duration_s": 900},
        )
        self.assertEqual(asked.status_code, 201, asked.text)
        request_id = asked.json()["request"]["id"]
        secret = asked.json()["claim_secret"]
        waiting = self.client.post(f"/v1/grants/requests/{request_id}/claim", json={"claim_secret": secret})
        self.assertIsNone(waiting.json()["token"])
        self.assertEqual(self.client.post(f"/v1/grants/requests/{request_id}/deny", headers=self.owner()).status_code, 200)
        denied = self.client.post(f"/v1/grants/requests/{request_id}/claim", json={"claim_secret": secret})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(self.client.post("/v1/grants/requests", json={"scopes": ["nope"]}).status_code, 422)

    def test_the_owner_may_revoke_but_not_approve_twice(self) -> None:
        asked = self.client.post("/v1/grants/requests", json={"scopes": ["sessions.read"]}).json()
        self.assertEqual(self.client.post(f"/v1/grants/requests/{asked['request']['id']}/approve", headers=self.owner()).status_code, 200)
        self.assertEqual(self.client.post(f"/v1/grants/requests/{asked['request']['id']}/approve", headers=self.owner()).status_code, 409)
        self.assertEqual(self.client.delete("/v1/grants/grant_missing", headers=self.owner()).status_code, 404)


if __name__ == "__main__":
    unittest.main()
