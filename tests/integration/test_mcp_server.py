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
        self.assertEqual(status["error"]["code"], "permission_denied")
        asked = self.call(session, "voxink_permissions_request", {"scopes": ["capture.system", "transcripts.read"], "max_minutes": 15, "reason": "meeting notes"})
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
