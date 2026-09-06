from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from voxink_cli.discovery import (
    ENDPOINT_ENV_VAR,
    EndpointUnavailable,
    endpoint_path,
    load,
    publish,
    revoke,
)


class ControlEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.path = self.root / "state" / "control-endpoint.json"

    def _publish(self, **overrides: object) -> Path:
        values: dict[str, object] = {
            "base_url": "http://127.0.0.1:8300",
            "auth_secret": "secret-value",
            "protocol_version": "1",
            "service_version": "0.2.0",
            "pid": 4242,
        }
        values.update(overrides)
        return publish(self.path, **values)  # type: ignore[arg-type]

    def test_published_endpoint_round_trips(self) -> None:
        self._publish()

        endpoint = load(self.path)

        self.assertEqual("http://127.0.0.1:8300", endpoint.base_url)
        self.assertEqual("secret-value", endpoint.auth_secret)
        self.assertEqual("1", endpoint.protocol_version)
        self.assertEqual(4242, endpoint.pid)

    def test_the_credential_file_is_readable_only_by_its_owner(self) -> None:
        self._publish()

        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

    def test_publishing_replaces_an_existing_endpoint_atomically(self) -> None:
        self._publish()
        self._publish(base_url="http://127.0.0.1:9000", auth_secret="rotated")

        endpoint = load(self.path)

        self.assertEqual("http://127.0.0.1:9000", endpoint.base_url)
        self.assertEqual("rotated", endpoint.auth_secret)
        self.assertEqual([self.path.name], [item.name for item in self.path.parent.iterdir()])

    def test_a_failed_write_leaves_no_temporary_credential_behind(self) -> None:
        self.path.parent.mkdir(parents=True)

        with patch("voxink_cli.discovery.json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._publish()

        self.assertEqual([], list(self.path.parent.iterdir()))

    def test_a_missing_endpoint_explains_how_to_start_a_helper(self) -> None:
        with self.assertRaises(EndpointUnavailable) as caught:
            load(self.path)

        self.assertIn("voxink-dev", str(caught.exception))

    def test_an_unreadable_endpoint_is_reported_rather_than_raising_json_errors(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")

        with self.assertRaises(EndpointUnavailable):
            load(self.path)

    def test_an_unsupported_endpoint_format_is_rejected(self) -> None:
        self._publish()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["schema_version"] = 99
        self.path.write_text(json.dumps(document), encoding="utf-8")

        with self.assertRaises(EndpointUnavailable):
            load(self.path)

    def test_a_non_loopback_endpoint_is_refused(self) -> None:
        self._publish(base_url="http://example.com:8300")

        with self.assertRaises(EndpointUnavailable) as caught:
            load(self.path)

        self.assertIn("loopback", str(caught.exception))

    def test_an_endpoint_without_an_address_is_refused(self) -> None:
        self._publish()
        self.path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

        with self.assertRaises(EndpointUnavailable):
            load(self.path)

    def test_revoking_tolerates_an_endpoint_already_gone(self) -> None:
        self._publish()

        revoke(self.path)
        revoke(self.path)

        self.assertFalse(self.path.exists())

    def test_the_environment_overrides_the_state_root(self) -> None:
        with patch.dict(os.environ, {ENDPOINT_ENV_VAR: str(self.path)}):
            self.assertEqual(self.path, endpoint_path())

        self.assertEqual(
            self.root / "control-endpoint.json", endpoint_path(state_root=self.root)
        )


if __name__ == "__main__":
    unittest.main()
