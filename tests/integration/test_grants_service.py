"""Consent-gated agent access: grants, scopes, approval flow, deadline, audit."""

from __future__ import annotations

from collections import deque
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

    def test_read_routes_apply_the_same_scope_policy_to_every_principal(self) -> None:
        runtime = self.runtime
        runtime.native_session_id = "scope-session"
        runtime.ui_events["scope-session"] = deque([{"type": "transcript", "text": "private fixture"}])
        runtime.ui_events_total["scope-session"] = 1
        self.client.post("/v1/sessions", json={"session_id": "scope-session"}, headers=self.owner())
        routes = {
            "/v1/sessions": "sessions.read",
            "/v1/sessions/scope-session": "sessions.read",
            "/v1/ui/capture/status?include_events=false": "sessions.read",
            "/v1/ui/capture/status": "transcripts.read",
            "/v1/transcripts": "transcripts.read",
            "/v1/sessions/scope-session/diarization": "transcripts.read",
        }
        identities = [("owner", self.owner()), ("anonymous", {})]
        for scope in ("models.switch", "sessions.read", "transcripts.read"):
            _, token = self.issue(scopes=[scope])
            identities.append((scope, self.bearer(token)))
        with patch.object(runtime.diarization, "describe", return_value={"state": "disabled"}):
            for identity, headers in identities:
                for route, scope in routes.items():
                    with self.subTest(identity=identity, route=route):
                        response = self.client.get(route, headers=headers)
                        allowed = identity in {"owner", scope} or (identity == "transcripts.read" and "include_events=false" in route)
                        expected = 401 if identity == "anonymous" else (200 if allowed else 403)
                        self.assertEqual(response.status_code, expected, response.text)
                        if expected == 200 and "capture/status" in route:
                            self.assertEqual(response.json()["events"], [] if "false" in route else list(runtime.ui_events["scope-session"]))

    def test_legacy_mutations_require_owner_before_producing_side_effects(self) -> None:
        for scopes in (["models.switch"], ["transcripts.read"], ["capture.microphone"]):
            _, token = self.issue(scopes=scopes)
            headers = self.bearer(token)
            with self.subTest(scopes=scopes):
                created = self.client.post("/v1/sessions", json={"session_id": "not-created"}, headers=headers)
                self.assertEqual(created.status_code, 403)
                transcribed = self.client.post("/v1/transcriptions", files={"file": ("test.wav", b"fake input")}, headers=headers)
                self.assertEqual(transcribed.status_code, 403)
                diarized = self.client.post("/v1/sessions/not-created/diarization", headers=headers)
                self.assertEqual(diarized.status_code, 403)
        self.assertEqual(self.runtime.provider.calls, 0)
        self.assertEqual(self.runtime.sessions.list(), ())
        created = self.client.post("/v1/sessions", json={"session_id": "owner-created"}, headers=self.owner())
        self.assertEqual(created.status_code, 201, created.text)
        transcribed = self.client.post("/v1/transcriptions", files={"file": ("test.wav", b"fake input")}, headers=self.owner())
        self.assertEqual(transcribed.status_code, 200, transcribed.text)

    def test_capture_actions_do_not_disclose_text_to_control_only_grants(self) -> None:
        _, token = self.issue(scopes=["capture.system"])
        headers = self.bearer(token)
        with patch("services.transcription.app.subprocess.Popen", return_value=self.fake_process()):
            started = self.client.post("/v1/ui/capture/start", json={"sources": ["system"]}, headers=headers)
            self.assertEqual(started.status_code, 200, started.text)
            sid = started.json()["session_id"]
            self.runtime.ui_events[sid].append({"type": "transcript", "text": "private fixture"})
            self.runtime.ui_events_total[sid] = 1
            for action in ("pause", "resume", "stop"):
                response = self.client.post(f"/v1/ui/capture/{action}", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["events"], [])
                status = self.client.get("/v1/ui/capture/status?include_events=false", headers=headers)
                self.assertEqual(status.status_code, 200, status.text)
                self.assertEqual(status.json()["events"], [])

    def test_capture_controls_require_the_active_sources_and_application_limits(self) -> None:
        _, system_token = self.issue(scopes=["capture.system"])
        _, app_token = self.issue(scopes=["capture.applications"], applications=["com.allowed"])
        process = self.fake_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process):
            self.client.post("/v1/ui/capture/start", json={"sources": ["microphone"]}, headers=self.owner())
            refused = self.client.post("/v1/ui/capture/pause", headers=self.bearer(system_token))
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(process.signals, [])
            self.client.post("/v1/ui/capture/stop", headers=self.owner())
        process = self.fake_process()
        with patch("services.transcription.app.subprocess.Popen", return_value=process):
            self.client.post("/v1/ui/capture/start", json={"sources": ["system"], "applications": ["com.other"]}, headers=self.owner())
            refused = self.client.post("/v1/ui/capture/resume", headers=self.bearer(app_token))
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(process.signals, [])

    def test_capture_status_restarts_cursor_when_the_observed_session_changes(self) -> None:
        self.runtime.native_session_id = "new-session"
        self.runtime.ui_events["new-session"] = deque([{"type": "transcript", "text": "first"}])
        self.runtime.ui_events_total["new-session"] = 1
        response = self.client.get("/v1/ui/capture/status?after=99&session_id=old-session", headers=self.owner())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["events"][0]["text"], "first")
        response = self.client.get("/v1/ui/capture/status?after=1&session_id=new-session", headers=self.owner())
        self.assertEqual(response.json()["events"], [])

    def test_capture_clock_reconciles_pause_resume_stop_and_new_recording(self) -> None:
        now = [0.0]
        with patch.object(self.runtime.capture_state, "_clock", side_effect=lambda: now[0]):
            for turn in range(2):
                process = self.fake_process()
                with patch("services.transcription.app.subprocess.Popen", return_value=process):
                    response = self.client.post("/v1/ui/capture/start", json={"sources": ["microphone"]}, headers=self.owner())
                    self.assertEqual(response.json()["elapsed_ms"], 0)
                    now[0] += 2.5
                    paused = self.client.post("/v1/ui/capture/pause", headers=self.owner()).json()
                    self.assertEqual(paused["elapsed_ms"], 2500)
                    now[0] += 10
                    paused = self.client.get("/v1/ui/capture/status", headers=self.owner()).json()
                    self.assertEqual(paused["elapsed_ms"], 2500)
                    self.client.post("/v1/ui/capture/resume", headers=self.owner())
                    now[0] += 1.5
                    stopped = self.client.post("/v1/ui/capture/stop", headers=self.owner()).json()
                    self.assertEqual(stopped["elapsed_ms"], 4000)
                    now[0] += 20
                    stopped = self.client.get("/v1/ui/capture/status", headers=self.owner()).json()
                    self.assertEqual(stopped["elapsed_ms"], 4000)

    def test_self_reports_the_callers_own_standing_not_the_catalog(self) -> None:
        catalog = self.client.get("/v1/grants/scopes").json()["scopes"]

        anonymous = self.client.get("/v1/grants/self").json()
        self.assertEqual(anonymous["credential"], "none")
        # The catalog is offered to choose from, never as the caller's own.
        self.assertEqual(anonymous["scopes"], [])
        self.assertEqual(anonymous["requestable_scopes"], catalog)

        owner = self.client.get("/v1/grants/self", headers=self.owner()).json()
        self.assertEqual(owner["credential"], "owner")
        self.assertEqual(owner["scopes"], catalog)

        grant, token = self.issue(scopes=["transcripts.read"], label="agent")
        held = self.client.get("/v1/grants/self", headers=self.bearer(token)).json()
        self.assertEqual(held["credential"], "grant")
        self.assertEqual(held["scopes"], ["transcripts.read"])
        self.assertEqual(held["grant_id"], grant["id"])

        # A token that is not a credential at all is nobody, not a grant.
        bogus = self.client.get("/v1/grants/self", headers=self.bearer("vxg_bogus")).json()
        self.assertEqual(bogus["credential"], "none")

    def test_a_caller_holding_nothing_is_told_how_to_ask(self) -> None:
        refused = self.client.get("/v1/transcripts")
        self.assertEqual(refused.status_code, 401)
        detail = refused.json()["detail"]
        self.assertEqual(detail["code"], "permission_denied")
        # The way out is named, so the refusal is actionable rather than prose.
        self.assertEqual(detail["consent"]["request"], "POST /v1/grants/requests")
        self.assertIn("/v1/grants/requests", detail["remediation"])

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
