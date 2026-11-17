"""Read identity and recovery through the public Inbox tool seam."""

from copy import deepcopy
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from inbox.test_mcp_lifecycle import BEARER, FakeClock, FakeNotifier, Inbox, InboxError
from paraphe.inbox import http as http_module

ENVELOPE_KEYS = {
    "request_id",
    "status",
    "version",
    "external_id",
    "choices",
    "expires_at",
    "response",
    "processed_at",
    "execution_status",
    "kind",
    "pending",
    "created_at",
}


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

    def test_approval_reads_expose_the_effective_offered_choices(self):
        created = self.inbox.call_tool("request_approval", {
            "title": "Proceed with the change?", "external_id": "approval-1",
        }, bearer=BEARER)
        rid = created["request_id"]
        pending = self.inbox.call_tool("get_response", {"external_id": "approval-1"})
        self.assertEqual(pending["choices"], ["Approve", "Deny"])
        self.assertEqual(self.inbox.call_tool("list_pending", {}), [pending])
        self.inbox.record_tap(rid, choice="Approve")
        answered = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertIn(answered["response"]["choice"], answered["choices"])
        self.assertEqual(self.inbox.call_tool("list_unprocessed", {}), [answered])

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

    def test_created_at_stored_survives_lookup_and_wire(self):
        self.rows["stored-rid"] = {
            "request_id": "stored-rid",
            "kind": "question",
            "state": "open",
            "external_id": "stored-ext",
            "question": "Proceed?",
            "choices": ["Proceed", "Hold"],
            "expires_in_seconds": 900.0,
            "expires_at": 1_700_003_600.5,
            "created_at": 1_700_000_000.25,
            "notified": True,
        }
        self.clock.now = 1_700_000_100.75
        inbox = self.new_inbox()

        by_id = inbox.call_tool("get_response", {"request_id": "stored-rid"})
        by_external = inbox.call_tool("get_response", {"external_id": "stored-ext"})
        for envelope in (by_id, by_external):
            self.assertIsInstance(envelope["created_at"], float)
            self.assertEqual(envelope["created_at"], 1_700_000_000.25)
            self.assertNotEqual(envelope["created_at"], self.clock.now)
            self.assertNotEqual(envelope["created_at"], 1_700_003_600.5 - 900.0)

        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_response",
                "arguments": {"request_id": "stored-rid", "wait_seconds": 0},
            },
        }
        response = http_module._dispatch(inbox, rpc, bearer=BEARER)
        self.assertNotIn("error", response)
        self.assertNotIn("isError", response["result"])
        # The real wire: serialize the HTTP body, then unwrap result.content[0].text.
        external = json.loads(json.dumps(response))
        wire = json.loads(external["result"]["content"][0]["text"])
        self.assertEqual(wire, by_id)
        self.assertIsInstance(wire["created_at"], float)
        self.assertEqual(wire["created_at"], 1_700_000_000.25)

    def test_created_at_legacy_derivation_stays_frozen(self):
        for rid, version in (("legacy-v1", 1), ("legacy-v2", 2)):
            self.rows[rid] = {
                "request_id": rid,
                "kind": "approval",
                "version": version,
                "state": "open",
                "external_id": f"{rid}-ext",
                "title": "Legacy",
                "expires_in_seconds": 900.0,
                "expires_at": 1_700_000_900.25,
                "notified": True,
            }
        self.clock.now = 1_700_000_000.0
        inbox = self.new_inbox()

        for rid in ("legacy-v1", "legacy-v2"):
            envelope = inbox.call_tool("get_response", {"request_id": rid})
            self.assertEqual(envelope["created_at"], 1_700_000_000.25)
            self.assertIsInstance(envelope["created_at"], float)
            self.assertEqual(self.rows[rid]["created_at"], 1_700_000_000.25)

        self.clock.now += 10.0
        for rid in ("legacy-v1", "legacy-v2"):
            ack = inbox.call_tool(
                "update_request",
                {
                    "request_id": rid,
                    "expected_version": self.rows[rid]["version"],
                    "expires_in_seconds": 1800,
                },
            )
            self.assertEqual(
                set(ack),
                {"request_id", "version", "duplicate", "pending", "status", "kind",
                 "read_with", "copy_notes"},
            )
            reread = inbox.call_tool("get_response", {"request_id": rid})
            self.assertEqual(reread["created_at"], 1_700_000_000.25)

        # v2 is the admitted estimate for a TTL-revised card, frozen once; a
        # reload must not derive it again from the moved expiry.
        restarted = self.new_inbox()
        for rid in ("legacy-v1", "legacy-v2"):
            again = restarted.call_tool("get_response", {"request_id": rid})
            self.assertEqual(again["created_at"], 1_700_000_000.25)
            self.assertEqual(self.rows[rid]["created_at"], 1_700_000_000.25)

    def test_created_at_legacy_unknown_stays_zero(self):
        missing = object()
        base = {"kind": "question", "state": "tapped", "notified": True}
        variants = (
            ("operands-missing", missing, missing),
            ("expires-null", None, 900.0),
            ("ttl-null", 1_700_000_900.25, None),
            ("expires-bool", True, 900.0),
            ("ttl-bool", 1_700_000_900.25, False),
            ("expires-text", "1700000900.25", 900.0),
            ("ttl-text", 1_700_000_900.25, "900"),
        )
        for field_state in ("absent", "null"):
            for name, expires_at, ttl in variants:
                rid = f"{field_state}-{name}"
                payload = {"request_id": rid, **base}
                if expires_at is not missing:
                    payload["expires_at"] = expires_at
                if ttl is not missing:
                    payload["expires_in_seconds"] = ttl
                if field_state == "null":
                    payload["created_at"] = None
                self.rows.clear()
                self.rows[rid] = payload
                inbox = self.new_inbox()
                if name == "expires-text":
                    response = http_module._dispatch(
                        inbox,
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "get_response",
                                "arguments": {"request_id": rid},
                            },
                        },
                        bearer=BEARER,
                    )
                    envelope = json.loads(response["result"]["content"][0]["text"])
                else:
                    envelope = inbox.call_tool("get_response", {"request_id": rid})
                self.assertIsInstance(envelope["created_at"], float)
                self.assertIsNotNone(envelope["created_at"])
                self.assertEqual(envelope["created_at"], 0.0)
                self.assertEqual(self.rows[rid]["created_at"], 0.0)

        # A stored 0.0 is final too: valid contradictory operands never rewrite it.
        self.rows.clear()
        self.rows["stored-zero"] = {
            "request_id": "stored-zero",
            "kind": "question",
            "state": "tapped",
            "notified": True,
            "created_at": 0.0,
            "expires_in_seconds": 900.0,
            "expires_at": 1_700_000_900.25,
        }
        inbox = self.new_inbox()
        envelope = inbox.call_tool("get_response", {"request_id": "stored-zero"})
        self.assertIsInstance(envelope["created_at"], float)
        self.assertEqual(envelope["created_at"], 0.0)
        self.assertNotEqual(envelope["created_at"], self.clock.now)
        self.assertEqual(self.rows["stored-zero"]["created_at"], 0.0)

    def test_created_at_is_additive_across_shared_envelopes(self):
        first = self.create()
        second = self.inbox.call_tool(
            "ask_question", {"question": "Second?", "external_id": "decision-2"},
            bearer=BEARER,
        )
        stamp = "2023-11-14T22:13:20Z"

        self.assertEqual(
            set(first),
            {"request_id", "version", "duplicate", "pending", "status", "kind",
             "read_with", "copy_notes"},
        )
        pending_first = {
            "request_id": first["request_id"],
            "status": "pending",
            "version": 1,
            "external_id": "decision-1",
            "choices": ["Proceed", "Hold"],
            "expires_at": 1_700_000_900.0,
            "response": None,
            "processed_at": None,
            "execution_status": None,
            "kind": "question",
            "pending": True,
            "created_at": 1_700_000_000.0,
        }
        pending_second = {
            "request_id": second["request_id"],
            "status": "pending",
            "version": 1,
            "external_id": "decision-2",
            "choices": ["Approve", "Deny"],
            "expires_at": 1_700_000_900.0,
            "response": None,
            "processed_at": None,
            "execution_status": None,
            "kind": "question",
            "pending": True,
            "created_at": 1_700_000_000.0,
        }

        by_id = self.inbox.call_tool("get_response", {"request_id": first["request_id"]})
        by_external = self.inbox.call_tool("get_response", {"external_id": "decision-2"})
        self.assertEqual(by_id, pending_first)
        self.assertEqual(by_external, pending_second)
        self.assertEqual(set(by_id), ENVELOPE_KEYS)
        # Two open cards keep the existing newest-first order.
        self.assertEqual(self.inbox.call_tool("list_pending", {}), [pending_second, pending_first])
        approval = self.inbox.call_tool(
            "request_approval", {"title": "Proceed?", "external_id": "approval-1"},
            bearer=BEARER,
        )
        cancelled = self.inbox.call_tool(
            "ask_question", {"question": "Cancel me?", "external_id": "cancel-1"},
            bearer=BEARER,
        )
        expired = self.inbox.call_tool(
            "ask_question", {"question": "Expire me?", "external_id": "expire-1"},
            bearer=BEARER,
        )
        answered_approval = {
            "request_id": approval["request_id"],
            "status": "answered",
            "version": 1,
            "external_id": "approval-1",
            "choices": ["Approve", "Deny"],
            "expires_at": 1_700_000_900.0,
            "response": {
                "choice": "Approve",
                "text": None,
                "responded_at": stamp,
                "responded_via": "telegram",
            },
            "processed_at": None,
            "execution_status": None,
            "kind": "approval",
            "pending": False,
            "created_at": 1_700_000_000.0,
        }
        reported_approval = dict(answered_approval, execution_status="completed")
        processed_approval = dict(
            answered_approval, status="acknowledged", processed_at=stamp,
            execution_status="completed",
        )
        cancelled_envelope = {
            "request_id": cancelled["request_id"],
            "status": "cancelled",
            "version": 1,
            "external_id": "cancel-1",
            "choices": ["Approve", "Deny"],
            "expires_at": 1_700_000_900.0,
            "response": None,
            "processed_at": None,
            "execution_status": None,
            "kind": "question",
            "pending": False,
            "created_at": 1_700_000_000.0,
        }
        answered_first = {
            "request_id": first["request_id"],
            "status": "answered",
            "version": 1,
            "external_id": "decision-1",
            "choices": ["Proceed", "Hold"],
            "expires_at": 1_700_000_900.0,
            "response": {
                "choice": "Hold",
                "text": None,
                "responded_at": stamp,
                "responded_via": "telegram",
            },
            "processed_at": None,
            "execution_status": None,
            "kind": "question",
            "pending": False,
            "created_at": 1_700_000_000.0,
        }
        processed_first = dict(
            answered_first, status="acknowledged", processed_at=stamp
        )
        expired_envelope = {
            "request_id": expired["request_id"],
            "status": "expired",
            "version": 1,
            "external_id": "expire-1",
            "choices": ["Approve", "Deny"],
            "expires_at": 1_700_000_900.0,
            "response": None,
            "processed_at": None,
            "execution_status": None,
            "kind": "question",
            "pending": False,
            "created_at": 1_700_000_000.0,
        }
        self.assertEqual(
            self.inbox.claim(
                approval["request_id"], version=1, from_id=999001, choice="Approve"
            ),
            answered_approval,
        )
        self.assertEqual(
            self.inbox.call_tool(
                "report_execution",
                {"request_id": approval["request_id"], "outcome": "completed"},
            ),
            reported_approval,
        )
        self.assertEqual(
            self.inbox.call_tool("mark_processed", {"request_id": approval["request_id"]}),
            processed_approval,
        )
        self.assertEqual(
            self.inbox.call_tool("cancel_request", {"request_id": cancelled["request_id"]}),
            cancelled_envelope,
        )
        self.assertEqual(
            self.inbox.claim(first["request_id"], version=1, from_id=999001, choice="Hold"),
            answered_first,
        )
        self.assertEqual(self.inbox.call_tool("list_unprocessed", {}), [answered_first])
        self.assertEqual(
            self.inbox.call_tool("mark_processed", {"request_id": first["request_id"]}),
            processed_first,
        )
        self.assertEqual(self.inbox.call_tool("list_unprocessed", {}), [])
        self.clock.now += 901
        self.assertEqual(
            self.inbox.call_tool("get_response", {"request_id": expired["request_id"]}),
            expired_envelope,
        )

    def test_created_at_wait_uses_shared_envelope(self):
        created = self.create()
        rid = created["request_id"]
        results: list[dict] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(
                    self.inbox.call_tool(
                        "get_response", {"request_id": rid, "wait_seconds": 5}
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced by assertions
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while not self.inbox._waiters and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.inbox._waiters, "the read never parked")
        self.inbox.claim(rid, version=1, from_id=999001, choice="Proceed")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

        parked = results[0]
        immediate = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(parked, immediate)
        self.assertIsInstance(parked["created_at"], float)
        self.assertEqual(parked["created_at"], 1_700_000_000.0)
        self.assertEqual(parked["response"], immediate["response"])

        other = self.inbox.call_tool(
            "ask_question", {"question": "Park?", "external_id": "park-2"}, bearer=BEARER
        )
        card = self.inbox._park(other["request_id"], 0.01)
        self.assertEqual(card.created_at, 1_700_000_000.0)
        self.assertEqual(self.inbox._waiters, {})

    def test_created_at_reads_do_not_consume_or_renotify(self):
        created = self.create()
        rid = created["request_id"]
        self.inbox.record_tap(rid, choice="Proceed")
        rows_before = deepcopy(self.rows[rid])
        notifications = len(self.notifier.sent)

        first = self.inbox.call_tool("get_response", {"request_id": rid})
        second = self.inbox.call_tool("get_response", {"external_id": "decision-1"})
        unprocessed = self.inbox.call_tool("list_unprocessed", {})
        self.assertEqual(first["status"], "answered")
        self.assertIsNone(first["processed_at"])
        self.assertEqual(first["response"]["choice"], "Proceed")
        self.assertEqual(first["created_at"], 1_700_000_000.0)
        self.assertIsInstance(first["created_at"], float)
        self.assertEqual(first, second)
        self.assertEqual(unprocessed, [first])
        self.assertEqual(self.rows[rid], rows_before)
        self.assertEqual(len(self.notifier.sent), notifications)

        with self.assertRaisesRegex(InboxError, "unknown request_id"):
            self.inbox.call_tool("get_response", {"request_id": "missing-rid"})
        self.assertNotIn("missing-rid", self.rows)
