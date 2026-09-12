from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voxink_cli.client import GRANT_TOKEN_FILE_ENV, ControlClient
from voxink_cli.discovery import ControlEndpoint
from voxink_cli.envelope import ErrorCode, Failure
from voxink_mcp.__main__ import main
from voxink_mcp.server import Session, serve


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


if __name__ == "__main__":
    unittest.main()
