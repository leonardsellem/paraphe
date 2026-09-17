"""Inbox-seam tests for tap claims and revision."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BEARER = "test-mcp-bearer"
OWNER = 999001
STRANGER = 888002


def _load_inbox():
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from paraphe import inbox as module

    return module


_inbox = _load_inbox()
ClaimRefused = _inbox.ClaimRefused
FakeTelegramPort = _inbox.FakeTelegramPort
Inbox = _inbox.Inbox
InboxError = _inbox.InboxError


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestTapClaims(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.clock = FakeClock()
        self.telegram = FakeTelegramPort()
        self.inbox = Inbox(
            owner_telegram_id=OWNER,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=Path(self._tmpdir.name) / "inbox.sqlite",
            mcp_create_bearer=BEARER,
            clock=self.clock,
            telegram_port=self.telegram,
        )
        self.addCleanup(self.inbox.close)

    def _create(self) -> dict:
        return self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "claim-1", "expires_in_seconds": 900},
            bearer=BEARER,
        )

    def test_owner_live_version_on_open_records_tap(self) -> None:
        created = self._create()
        envelope = self.inbox.claim(
            created["request_id"],
            version=created["version"],
            from_id=OWNER,
            choice="Approve",
        )
        self.assertEqual(envelope["response"]["choice"], "Approve")
        self.assertEqual(envelope["status"], "answered")
        self.assertEqual(self.telegram.calls, [(created["request_id"], created["version"])])

    def test_non_owner_never_records_a_tap(self) -> None:
        created = self._create()
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim(
                created["request_id"],
                version=created["version"],
                from_id=STRANGER,
                choice="Approve",
            )
        self.assertEqual(raised.exception.reason, "not_owner")
        pending = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertIsNone(pending["response"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(self.telegram.calls, [])

    def test_expired_cancelled_and_already_tapped_refuse(self) -> None:
        expired = self.inbox.call_tool(
            "request_approval",
            {"title": "Expire", "external_id": "claim-expired", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.clock.now += 901
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim(
                expired["request_id"],
                version=expired["version"],
                from_id=OWNER,
                choice="Approve",
            )
        self.assertEqual(raised.exception.reason, "expired")
        got = self.inbox.call_tool("get_response", {"request_id": expired["request_id"]})
        self.assertEqual(got["status"], "expired")
        self.assertIsNone(got["response"])

        cancelled = self.inbox.call_tool(
            "request_approval",
            {"title": "Cancel", "external_id": "claim-cancel", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.inbox.call_tool("cancel_request", {"request_id": cancelled["request_id"]})
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim(
                cancelled["request_id"],
                version=cancelled["version"],
                from_id=OWNER,
                choice="Approve",
            )
        self.assertEqual(raised.exception.reason, "cancelled")

        tapped = self.inbox.call_tool(
            "request_approval",
            {"title": "Twice", "external_id": "claim-tapped", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.inbox.claim(
            tapped["request_id"],
            version=tapped["version"],
            from_id=OWNER,
            choice="Approve",
        )
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim(
                tapped["request_id"],
                version=tapped["version"],
                from_id=OWNER,
                choice="Deny",
            )
        self.assertEqual(raised.exception.reason, "already_tapped")
        again = self.inbox.call_tool("get_response", {"request_id": tapped["request_id"]})
        self.assertEqual(again["response"]["choice"], "Approve")

    def test_update_request_makes_previous_version_refuse(self) -> None:
        created = self._create()
        updated = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "title": "Cut over now?",
            },
        )
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim(
                created["request_id"],
                version=created["version"],
                from_id=OWNER,
                choice="Approve",
            )
        self.assertEqual(raised.exception.reason, "stale_version")
        self.assertEqual(self.telegram.calls, [])
        live = self.inbox.claim(
            created["request_id"],
            version=updated["version"],
            from_id=OWNER,
            choice="Approve",
        )
        self.assertEqual(live["response"]["choice"], "Approve")

    def test_clock_past_expires_at_expires_before_claim(self) -> None:
        created = self._create()
        self.clock.now += 900
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim(
                created["request_id"],
                version=created["version"],
                from_id=OWNER,
                choice="Approve",
            )
        self.assertEqual(raised.exception.reason, "expired")
        got = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(got["status"], "expired")
        self.assertIsNone(got["response"])
        self.assertEqual(self.telegram.calls, [(created["request_id"], created["version"])])

    def test_strip_failure_does_not_authorize_or_unrecord(self) -> None:
        created = self._create()
        self.telegram.fail = True
        envelope = self.inbox.claim(
            created["request_id"],
            version=created["version"],
            from_id=OWNER,
            choice="Approve",
        )
        self.assertEqual(envelope["response"]["choice"], "Approve")
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim(
                created["request_id"],
                version=created["version"],
                from_id=OWNER,
                choice="Deny",
            )
        self.assertEqual(raised.exception.reason, "already_tapped")
        got = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(got["response"]["choice"], "Approve")


if __name__ == "__main__":
    unittest.main()
