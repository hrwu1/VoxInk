from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

from voxink_core.audit import AuditLog
from voxink_core.grants import GrantError, GrantStore


class GrantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = GrantStore(Path(self.temporary.name) / "state" / "grants.json")

    def test_issue_authenticate_and_revoke(self) -> None:
        grant, token = self.store.issue(label="Claude", scopes=["capture.microphone", "transcripts.read"])
        self.assertTrue(token.startswith("vxg_"))
        self.assertEqual(self.store.authenticate(token).id, grant.id)
        self.assertIsNone(self.store.authenticate("vxg_nope"))
        self.assertIsNone(self.store.authenticate(None))
        mode = stat.S_IMODE(self.store.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        stored = json.loads(self.store.path.read_text())
        self.assertNotIn(token, json.dumps(stored))
        self.store.revoke(grant.id)
        self.assertIsNone(self.store.authenticate(token))
        self.assertFalse(self.store.get(grant.id).is_valid())

    def test_single_use_and_expiry(self) -> None:
        grant, token = self.store.issue(label="once", scopes=["capture.system"], single_use=True)
        self.assertIsNotNone(self.store.authenticate(token))
        self.store.consume(grant.id)
        self.assertIsNone(self.store.authenticate(token))
        expired, expired_token = self.store.issue(label="old", scopes=["sessions.read"], expires_in_s=1)
        from datetime import datetime, timedelta, timezone

        self.assertFalse(expired.is_valid(datetime.now(timezone.utc) + timedelta(seconds=5)))
        self.assertTrue(expired.is_valid())
        self.assertIsNotNone(expired_token)

    def test_retaining_audio_implies_the_scope_and_unknown_scopes_are_refused(self) -> None:
        grant, _ = self.store.issue(label="x", scopes=["capture.microphone"], retain_audio=True)
        self.assertIn("audio.retain", grant.scopes)
        with self.assertRaises(GrantError):
            self.store.issue(label="x", scopes=["capture.everything"])
        with self.assertRaises(GrantError):
            self.store.issue(label="x", scopes=[])

    def test_request_approve_claim_flow(self) -> None:
        pending, secret = self.store.request(
            client="Claude Code", reason="take meeting notes", scopes=["capture.system"],
            applications=["us.zoom.xos"], max_duration_s=3600, retain_audio=False,
        )
        self.assertEqual([item.id for item in self.store.pending()], [pending.id])
        # Nothing to claim until the user decides.
        request, token = self.store.claim(pending.id, secret)
        self.assertIsNone(token)
        self.assertEqual(request.state, "pending")
        grant = self.store.approve(pending.id, max_duration_s=600, single_use=True)
        self.assertEqual(grant.scopes, ("capture.system",))
        self.assertEqual(grant.applications, ("us.zoom.xos",))
        self.assertEqual(grant.max_duration_s, 600)
        self.assertTrue(grant.single_use)
        with self.assertRaises(GrantError):
            self.store.claim(pending.id, "wrong-secret")
        claimed, token = self.store.claim(pending.id, secret)
        self.assertEqual(claimed.state, "claimed")
        self.assertEqual(self.store.authenticate(token).id, grant.id)
        with self.assertRaises(GrantError):
            self.store.claim(pending.id, secret)
        self.assertEqual(self.store.pending(), [])

    def test_denied_requests_cannot_be_claimed_and_cannot_be_approved_twice(self) -> None:
        pending, secret = self.store.request(client="a", reason="", scopes=["transcripts.read"])
        self.store.deny(pending.id)
        with self.assertRaises(GrantError):
            self.store.claim(pending.id, secret)
        with self.assertRaises(GrantError):
            self.store.approve(pending.id)
        with self.assertRaises(GrantError):
            self.store.approve("req_missing")


class AuditLogTests(unittest.TestCase):
    def test_entries_are_bounded_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = AuditLog(Path(temporary) / "audit.jsonl")
            log.record(
                "capture.start", client="grant:g1", result="ok", session_id="s1",
                scope="capture.system", sources=["system"], transcript_text="x" * 500,
            )
            log.record("capture.stop", client="owner", result="ok")
            entries = log.tail()
            self.assertEqual([e["operation"] for e in entries], ["capture.start", "capture.stop"])
            self.assertEqual(len(entries[0]["details"]["transcript_text"]), 120)
            self.assertEqual(stat.S_IMODE(log.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
