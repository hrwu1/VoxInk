"""The MCP server over a live loopback helper, from a client with no credential."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from services.transcription.app import ServiceSettings, create_app
from voxink_cli.client import ControlClient, RequestContext
from voxink_cli.discovery import ControlEndpoint
from voxink_mcp.server import TOOLS, Session, handle, serve

from tests.integration.test_grants_service import FakeProcess
from tests.integration.test_public_cli import _LiveHelper
from tests.integration.test_transcription_service import FakeProvider


OWNER = "mcp-owner-secret"


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        native = root / "voxink-capture"
        native.touch()
        native.chmod(0o755)
        app = create_app(
            ServiceSettings(
                output_root=root / "out",
                state_root=root / "state",
                auth_secret=OWNER,
                native_capture_path=native,
                translation_model_path=None,
            ),
            provider=FakeProvider(),
        )
        self.helper = _LiveHelper(app)
        self.helper.start()
        self.addCleanup(self.helper.stop)
        self.endpoint = ControlEndpoint(base_url=self.helper.base_url, auth_secret=OWNER)

    def factory(self, context: RequestContext, token: str | None) -> ControlClient:
        return ControlClient(self.endpoint, context, token=token)

    def owner_client(self) -> ControlClient:
        return ControlClient(self.endpoint, RequestContext.create())

    def mint_grant(self, scopes: list[str]) -> str:
        """A token carrying exactly `scopes`, through the real consent flow."""
        asked = self.owner_client().request_grant(client="agent", reason="test", scopes=scopes)
        request_id = asked["request"]["id"]
        self.owner_client().approve_grant_request(request_id)
        return self.owner_client().claim_grant(request_id, asked["claim_secret"])["token"]

    def call(self, session: Session, name: str, arguments: dict | None = None, identifier: int = 1) -> dict:
        response = handle(session, {"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})
        return json.loads(response["result"]["content"][0]["text"]) | {"isError": response["result"].get("isError", False)}

    def test_initialize_and_tools_list(self) -> None:
        session = Session(token=None, owner=False, client_name="test", client_factory=self.factory)
        initialized = handle(session, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "voxink")
        self.assertIsNone(handle(session, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listed = handle(session, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, {tool["name"] for tool in TOOLS})
        self.assertIn("voxink_capture_events", names)
        self.assertEqual(handle(session, {"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_an_unauthorized_agent_can_only_ask_and_then_act_under_the_grant(self) -> None:
        session = Session(token=None, owner=False, client_name="Claude Code", client_factory=self.factory)
        status = self.call(session, "voxink_status")
        self.assertFalse(status["ok"])
        # Holding nothing is not a dead end: the refusal carries a request the
        # owner can approve, which is what `initialize` tells the agent to expect.
        self.assertEqual(status["error"]["code"], "approval_required")
        self.assertEqual(status["error"]["details"]["missing_scopes"], ["sessions.read"])
        self.assertTrue(status["error"]["details"]["request_id"])
        self.assertTrue(status["error"]["details"]["claim_secret"])
        self.assertTrue(status["error"]["retryable"])
        asked = self.call(session, "voxink_permissions_request", {"scopes": ["capture.system", "sessions.read", "transcripts.read"], "max_minutes": 15, "reason": "meeting notes"})
        self.assertTrue(asked["ok"], asked)
        request_id = asked["data"]["request_id"]
        secret = asked["data"]["claim_secret"]
        waiting = self.call(session, "voxink_permissions_claim", {"request_id": request_id, "claim_secret": secret})
        self.assertFalse(waiting["data"]["claimed"])
        # The owner approves in the application.
        self.owner_client().approve_grant_request(request_id)
        claimed = self.call(session, "voxink_permissions_claim", {"request_id": request_id, "claim_secret": secret})
        self.assertTrue(claimed["data"]["claimed"], claimed)
        self.assertTrue(session.token.startswith("vxg_"))
        status = self.call(session, "voxink_status")
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["data"]["credential"], "grant")
        process = FakeProcess()
        with patch("services.transcription.app.subprocess.Popen", return_value=process):
            started = self.call(session, "voxink_capture_start", {"sources": ["system"]})
            self.assertTrue(started["ok"], started)
            events = self.call(session, "voxink_capture_events", {"after": 0, "wait_seconds": 0})
            self.assertEqual(events["data"]["state"], "recording")
            stopped = self.call(session, "voxink_capture_stop")
            self.assertTrue(stopped["ok"], stopped)
        # Microphone was not granted: the refusal names the scope.
        refused = self.call(session, "voxink_capture_start", {"sources": ["microphone"]})
        self.assertEqual(refused["error"]["code"], "approval_required")
        self.assertEqual(refused["error"]["details"]["missing_scopes"], ["capture.microphone"])
        listed = self.call(session, "voxink_transcripts_list")
        self.assertTrue(listed["ok"], listed)

    def test_capabilities_reports_what_this_session_may_do_not_the_catalog(self) -> None:
        session = Session(token=None, owner=False, client_name="Claude Code", client_factory=self.factory)
        holding_nothing = self.call(session, "voxink_capabilities")
        self.assertTrue(holding_nothing["ok"], holding_nothing)
        self.assertEqual(holding_nothing["data"]["credential"], "none")
        # The catalog stays available to choose from, but never as this
        # session's own permissions.
        self.assertEqual(holding_nothing["data"]["permitted_scopes"], [])
        self.assertIn("capture.system", holding_nothing["data"]["requestable_scopes"])
        self.assertNotIn("grant_scopes", holding_nothing["data"])

        token = self.mint_grant(["transcripts.read"])
        granted = self.call(Session(token=token, owner=False, client_name="a", client_factory=self.factory), "voxink_capabilities")
        self.assertEqual(granted["data"]["credential"], "grant")
        self.assertEqual(granted["data"]["permitted_scopes"], ["transcripts.read"])

    def test_a_refused_capture_asks_only_for_the_sources_it_was_given(self) -> None:
        session = Session(token=None, owner=False, client_name="Claude Code", client_factory=self.factory)
        refused = self.call(session, "voxink_capture_start", {"sources": ["microphone"]})
        self.assertEqual(refused["error"]["code"], "approval_required")
        self.assertEqual(refused["error"]["details"]["missing_scopes"], ["capture.microphone"])
        pending = {item["id"]: item for item in self.owner_client().grant_requests()["requests"]}
        self.assertEqual(pending[refused["error"]["details"]["request_id"]]["scopes"], ["capture.microphone"])

    def test_the_consent_tools_never_ask_for_a_grant_of_their_own(self) -> None:
        session = Session(token=None, owner=False, client_name="Claude Code", client_factory=self.factory)
        claimed = self.call(session, "voxink_permissions_claim", {"request_id": "req_missing", "claim_secret": "nope"})
        self.assertFalse(claimed["ok"])
        self.assertNotEqual(claimed["error"]["code"], "approval_required")
        self.assertEqual(self.owner_client().grant_requests()["requests"], [])

    def test_caption_events_request_transcript_permission(self) -> None:
        session = Session(token=None, owner=False, client_name="test", client_factory=self.factory)
        refused = self.call(session, "voxink_capture_events", {"wait_seconds": 0})
        self.assertEqual(refused["error"]["code"], "approval_required")
        self.assertEqual(refused["error"]["details"]["missing_scopes"], ["transcripts.read"])

    def test_serve_reads_jsonrpc_lines_from_stdin(self) -> None:
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
            + "not json\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
        )
        stdout = io.StringIO()
        serve(stdin, stdout, owner=True, client_factory=self.factory)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(lines[1]["error"]["code"], -32700)
        self.assertEqual(lines[2]["result"], {})


if __name__ == "__main__":
    unittest.main()
