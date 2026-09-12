from __future__ import annotations

import argparse
import unittest
from unittest.mock import MagicMock

from voxink_cli.cli import _capture_start
from voxink_cli.envelope import ErrorCode, Failure
from voxink_mcp.server import Session


class CaptureOptionContractTests(unittest.TestCase):
    """The same public input must reach the same request across adapters."""

    def invoke(self, adapter: str, client: MagicMock, sources: list[str], applications: list[str]) -> None:
        if adapter == "cli":
            _capture_start(argparse.Namespace(sources=sources, applications=applications, retain_audio=False), client)
        else:
            session = Session(token="synthetic-grant", owner=False, client_name="test", client_factory=lambda context, token: client)
            session.call("voxink_capture_start", {"sources": sources, "applications": applications})

    def test_shared_valid_cases_produce_identical_wire_inputs(self) -> None:
        cases = [
            (["microphone"], [], ["microphone"], []),
            (["system"], [], ["system"], []),
            (["applications"], ["com.test"], ["system"], ["com.test"]),
            (["microphone", "system", "microphone"], [], ["microphone", "system"], []),
            (["microphone", "applications"], [" com.test ", "com.test", "com.other"], ["microphone", "system"], ["com.test", "com.other"]),
        ]
        for sources, apps, wire_sources, wire_apps in cases:
            for adapter in ("cli", "mcp"):
                with self.subTest(adapter=adapter, sources=sources, applications=apps):
                    client = MagicMock()
                    client.start_capture.return_value = {}
                    self.invoke(adapter, client, sources, apps)
                    request = client.start_capture.call_args.kwargs
                    self.assertEqual(request["sources"], wire_sources)
                    self.assertEqual(request["applications"], wire_apps)

    def test_ambiguous_or_incomplete_requests_fail_before_transport(self) -> None:
        for sources, apps in [
            (["applications"], []),
            (["applications"], [" "]),
            (["system"], ["com.test"]),
            (["microphone"], ["com.test"]),
            (["system", "applications"], ["com.test"]),
        ]:
            for adapter in ("cli", "mcp"):
                with self.subTest(adapter=adapter, sources=sources, applications=apps):
                    client = MagicMock()
                    with self.assertRaises(Failure) as caught:
                        self.invoke(adapter, client, sources, apps)
                    self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
                    client.start_capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
