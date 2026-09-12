from __future__ import annotations

import io
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from voxink_cli.client import GRANT_TOKEN_FILE_ENV, ControlClient
from voxink_cli.discovery import ControlEndpoint
from voxink_cli.envelope import ErrorCode, Failure
from voxink_mcp.__main__ import main
from voxink_mcp.server import TOOLS, Session, handle, serve


class CredentialTests(unittest.TestCase):
    def test_invalid_selected_files_fail_before_serving_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "grant"
            for payload in (b"", b" \n", b"\xff", None):
                if payload is None:
                    source.unlink()
                else:
                    source.write_bytes(payload)
                with self.subTest(payload=payload):
                    output = io.StringIO()
                    with self.assertRaises(Failure) as caught:
                        serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), output, token_file=source)
                    self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)
                    self.assertEqual(output.getvalue(), "")

    def test_no_selection_can_enter_consent_without_owner_access(self) -> None:
        endpoint = ControlEndpoint("http://127.0.0.1:8300", "synthetic-owner")
        session = Session(
            token=None, owner=False, client_name="test",
            client_factory=lambda context, token: ControlClient(endpoint, context, token=token),
        )
        self.assertIsNone(session.client().credential)
        with patch.dict(os.environ, {}, clear=True):
            output = io.StringIO()
            serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'), output)
        tools = json.loads(output.getvalue())["result"]["tools"]
        self.assertIn("voxink_permissions_request", {tool["name"] for tool in tools})

    def test_entrypoint_reports_credential_failure_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["voxink-mcp", "--token-file", str(Path(directory) / "missing")]):
                with patch("sys.stderr", new_callable=io.StringIO) as error:
                    self.assertEqual(main(), 5)
        self.assertIn("permission_denied", error.getvalue())


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = MagicMock()
        self.session = Session(token=None, owner=True, client_name="test", client_factory=self.factory)

    def call(self, name: object, arguments: object) -> dict:
        return handle(self.session, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })

    def test_tool_schemas_and_handlers_describe_the_same_arguments(self) -> None:
        for tool in TOOLS:
            handler = getattr(self.session, f"tool_{tool['name'].removeprefix('voxink_')}")
            parameters = inspect.signature(handler).parameters
            self.assertEqual(set(parameters), set(tool["inputSchema"]["properties"]))
            required = {key for key, value in parameters.items() if value.default is inspect.Parameter.empty}
            self.assertEqual(required, set(tool["inputSchema"]["required"]))

    def test_invalid_arguments_are_rejected_before_any_client_call(self) -> None:
        cases = [
            ("voxink_capture_start", {}),
            ("voxink_capture_start", {"sources": "microphone"}),
            ("voxink_capture_start", {"sources": ["unknown"]}),
            ("voxink_capture_start", {"sources": [True]}),
            ("voxink_capture_start", {"sources": ["microphone"], "retain_audio": "false"}),
            ("voxink_capture_events", {"after": True}),
            ("voxink_capture_events", {"after": -1}),
            ("voxink_capture_events", {"wait_seconds": "oops"}),
            ("voxink_capture_events", {"wait_seconds": float("nan")}),
            ("voxink_capture_events", {"wait_seconds": float("inf")}),
            ("voxink_capture_events", {"wait_seconds": 31}),
            ("voxink_transcript_show", {"session_id": "test", "part": 0}),
            ("voxink_permissions_request", {"scopes": [], "max_minutes": 0}),
            ("voxink_status", {"unknown": True}),
            ("voxink_status", []),
            ("voxink_status", None),
            (42, {}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self.call(name, arguments)["error"]["code"], -32602)
        self.factory.assert_not_called()

    def test_unknown_tool_cannot_dispatch_an_unadvertised_method(self) -> None:
        result = self.call("status", {})
        body = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.factory.assert_not_called()

    def test_malformed_requests_do_not_terminate_the_stdio_loop(self) -> None:
        messages = [
            "not json", "[]", "null", "false", "{}",
            '{"jsonrpc":"1.0","id":1,"method":"ping"}',
            '{"jsonrpc":"2.0","id":{},"method":"ping"}',
            '{"jsonrpc":"2.0","id":true,"method":"ping"}',
            '{"jsonrpc":"2.0","id":1,"method":23}',
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[1]}',
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"voxink_capture_events","arguments":{"wait_seconds":"oops"}}}',
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"voxink_capture_events","arguments":{"wait_seconds":NaN}}}',
        ]
        output = io.StringIO()
        ping = '{"jsonrpc":"2.0","id":"alive","method":"ping"}'
        with patch.dict(os.environ, {}, clear=True):
            serve(io.StringIO("\n".join([*messages, ping])), output, owner=True)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), len(messages) + 1)
        self.assertTrue(all("error" in reply for reply in replies[:-1]))
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": "alive", "result": {}})

    def test_notifications_get_no_reply_and_null_request_ids_do(self) -> None:
        for method, params in (("ping", {}), ("tools/list", {}), ("unknown", {}), ("tools/call", {"name": "voxink_status", "arguments": []})):
            self.assertIsNone(handle(self.session, {"jsonrpc": "2.0", "method": method, "params": params}))
        self.assertEqual(handle(self.session, {"jsonrpc": "2.0", "id": None, "method": "ping"})["result"], {})

    def test_unexpected_tool_failure_returns_internal_error(self) -> None:
        self.factory.side_effect = ValueError("synthetic internal error")
        self.assertEqual(self.call("voxink_status", {})["error"], {"code": -32603, "message": "internal error"})
        self.assertEqual(handle(self.session, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"], {})


if __name__ == "__main__":
    unittest.main()
