"""Read identity and recovery through the public Inbox tool seam."""

from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from inbox.test_mcp_lifecycle import BEARER, FakeClock, FakeNotifier, Inbox, InboxError


class TestResponseIdentity(unittest.TestCase):
    def setUp(self):
        self.rows = {}
        self.store = Mock()
        self.store.load_cards.side_effect = lambda: deepcopy(list(self.rows.values()))
        self.store.load_notification_ids.return_value = []
        self.store.save_card.side_effect = lambda row: self.rows.update(
            {row["request_id"]: deepcopy(row)}
        )
        patcher = patch("paraphe.inbox.Store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = FakeClock()
        self.notifier = FakeNotifier()
        self.inbox = self.new_inbox()

    def new_inbox(self):
        inbox = Inbox(owner_telegram_id=999001, default_ttl_seconds=900,
                      floor_ttl_seconds=900, mcp_create_bearer=BEARER,
                      notifier=self.notifier, clock=self.clock)
        self.addCleanup(inbox.close)
        return inbox

    def create(self):
        return self.inbox.call_tool("ask_question", {
            "question": "Proceed?", "external_id": "decision-1",
            "choices": ["Proceed", "Hold"],
        }, bearer=BEARER)

    def test_external_lookup_recovers_lost_ack_after_reload_without_notify(self):
        created = self.create()  # Caller loses this ACK; only its external id survives.
        restarted = self.new_inbox()
        recovered = restarted.call_tool("get_response", {"external_id": "decision-1"}, bearer=BEARER)
        self.assertEqual(recovered["request_id"], created["request_id"])
        self.assertEqual(recovered, restarted.call_tool("get_response", {
            "request_id": created["request_id"],
        }))
        self.assertEqual(len(self.notifier.sent), 1)
        restarted.call_tool("update_request", {
            "request_id": recovered["request_id"], "expected_version": 1,
            "external_id": "not-a-new-identity", "choices": ["Ship", "Wait"],
        })
        updated = restarted.call_tool("get_response", {"external_id": "decision-1"})
        self.assertEqual(updated["external_id"], "decision-1")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["choices"], ["Ship", "Wait"])
        restarted.record_tap(updated["request_id"], choice="Wait")
        answered = restarted.call_tool("get_response", {
            "external_id": "decision-1", "wait_seconds": 0.01,
        })
        self.assertEqual(answered["response"]["choice"], "Wait")
        self.assertEqual(answered["version"], 2)
        self.assertIsNone(answered["processed_at"])
        self.assertEqual(restarted.call_tool("list_unprocessed", {}), [answered])

    def test_lookup_requires_exactly_one_valid_identifier(self):
        created = self.create()
        for args in ({}, {"request_id": created["request_id"], "external_id": "decision-1"},
                     {"request_id": created["request_id"], "external_id": None}):
            with self.subTest(args=args), self.assertRaisesRegex(InboxError, "exactly one"):
                self.inbox.call_tool("get_response", args)
        for key in ("request_id", "external_id"):
            for value in (None, "", " ", 1, True, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(InboxError):
                    self.inbox.call_tool("get_response", {key: value})
        for value in ("missing", " decision-1", "not-a-new-identity"):
            with self.subTest(value=value), self.assertRaisesRegex(InboxError, "unknown external_id"):
                self.inbox.call_tool("get_response", {"external_id": value})
        with self.assertRaisesRegex(InboxError, "too long"):
            self.inbox.call_tool("get_response", {"external_id": "x" * 201})

    def test_lookup_refreshes_expiry_and_cannot_record_an_owner_answer(self):
        self.create()
        with self.assertRaises(InboxError):
            self.inbox.call_tool("get_response", {
                "external_id": "decision-1", "choice": "Proceed",
            }, bearer=BEARER)
        self.clock.now += 901
        expired = self.inbox.call_tool("get_response", {"external_id": "decision-1"})
        self.assertEqual(expired["status"], "expired")
        self.assertIsNone(expired["response"])
        self.assertEqual(self.inbox.call_tool("list_pending", {}), [])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_tool_schema_and_help_advertise_exclusive_lookup(self):
        descriptor = next(tool for tool in self.inbox.list_tool_descriptors()
                          if tool["name"] == "get_response")
        schema = descriptor["inputSchema"]
        self.assertEqual(schema["oneOf"], [
            {"required": ["request_id"]}, {"required": ["external_id"]},
        ])
        self.assertEqual(schema["properties"]["external_id"]["maxLength"], 200)
        self.assertNotIn("required", schema)
        self.assertIn("exactly one", descriptor["description"])
        self.assertIn("get_response with external_id", self.inbox.call_tool("how_to_use", {}))

    def test_reads_expose_identity_revision_choices_and_expiry_without_aliasing(self):
        created = self.create()
        rid = created["request_id"]
        pending = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(pending["external_id"], "decision-1")
        self.assertEqual(pending["version"], 1)
        self.assertEqual(pending["choices"], ["Proceed", "Hold"])
        self.assertEqual(pending["expires_at"], 1_700_000_900.0)
        self.assertEqual(self.inbox.call_tool("list_pending", {}), [pending])
        pending["choices"].append("Injected")
        self.inbox.record_tap(rid, choice="Hold")
        answered = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(answered["choices"], ["Proceed", "Hold"])
        self.assertEqual(answered["external_id"], "decision-1")
        self.assertEqual(answered["version"], 1)
        self.assertEqual(answered["response"]["choice"], "Hold")
        self.assertEqual(self.inbox.call_tool("list_unprocessed", {}), [answered])
