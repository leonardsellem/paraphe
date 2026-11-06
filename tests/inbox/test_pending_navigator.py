"""Pending navigator tests: order, pages, card view, answers, prompts, refresh."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from inbox.test_telegram_port import BEARER, FakeBotAPI, FakeClock, OWNER, STRANGER, TOKEN
from paraphe import inbox as inbox_mod
from paraphe.adapters import telegram as tg
from paraphe.inbox import Inbox, NotifyRejected, TOOL_NAMES, TOOL_SCHEMAS
from paraphe.inbox.store import Store

EMPTY = {"inline_keyboard": []}


class NavigatorAPI(FakeBotAPI):
    """The Telegram fake, extended with the navigator's five methods."""

    def __init__(self) -> None:
        super().__init__()
        self.pins: list[tuple[int, int]] = []
        self.unpins: list[tuple[int, int]] = []
        self.texts: list[dict] = []
        self.commands: list[dict] = []
        self.default_commands: list[dict] = []
        self.command_sets: list[dict] = []
        self.command_reads: list[dict] = []
        self.send_errors: list[BaseException] = []
        self.edit_errors: list[BaseException] = []
        self.strip_errors: dict[int, BaseException] = {}
        self.unpin_errors: list[BaseException] = []
        self.pin_errors: list[BaseException] = []
        self.on_edit = None
        self.on_strip = None
        self.command_error: BaseException | None = None

    def send_message(self, chat_id, text, reply_markup, parse_mode=None):
        if self.send_errors:
            raise self.send_errors.pop(0)
        return super().send_message(chat_id, text, reply_markup, parse_mode)

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup) -> None:
        error = self.strip_errors.pop(message_id, None)
        if error is not None:
            raise error
        if self.on_strip is not None and not reply_markup.get("inline_keyboard"):
            self.on_strip(chat_id, message_id)
        super().edit_message_reply_markup(chat_id, message_id, reply_markup)

    def edit_message_text(self, chat_id, message_id, text, reply_markup, parse_mode="HTML"):
        self.texts.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )
        if self.on_edit is not None:
            self.on_edit()
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        for message in self.messages:
            if message["message_id"] == message_id:
                message["text"] = text
                message["reply_markup"] = reply_markup

    def pin_chat_message(self, chat_id: int, message_id: int) -> None:
        if self.pin_errors:
            raise self.pin_errors.pop(0)
        self.pins.append((chat_id, message_id))

    def unpin_chat_message(self, chat_id: int, message_id: int) -> None:
        if self.unpin_errors:
            raise self.unpin_errors.pop(0)
        self.unpins.append((chat_id, message_id))

    def get_my_commands(self, scope):
        self.command_reads.append(scope)
        if self.command_error is not None:
            error, self.command_error = self.command_error, None
            raise error
        return [dict(item) for item in (self.commands if scope.get("type") == "chat" else self.default_commands)]

    def set_my_commands(self, commands, scope) -> None:
        if self.command_error is not None:
            error, self.command_error = self.command_error, None
            raise error
        self.command_sets.append({"commands": commands, "scope": scope})


def legacy_payload(
    request_id: str,
    *,
    version: int = 1,
    expires_at: float = 1_000_000.0,
    ttl: float = 900.0,
    title: str = "Legacy",
    state: str = "open",
) -> dict:
    """A card payload written before `created_at` existed."""
    return {
        "request_id": request_id,
        "kind": "approval",
        "version": version,
        "title": title,
        "expires_in_seconds": ttl,
        "expires_at": expires_at,
        "state": state,
        "notified": True,
    }


class NavigatorCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.clock = FakeClock()
        self.api = NavigatorAPI()
        self.inbox = self._inbox(self.clock)
        self.adapter = tg.TelegramAdapter(self.inbox, self.api, owner_id=OWNER, bot_token=TOKEN)
        self.inbox._notifier = self.adapter
        self.inbox._telegram = self.adapter

    def _inbox(self, clock: FakeClock) -> Inbox:
        inbox = Inbox(
            owner_telegram_id=OWNER,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=Path(self._tmpdir.name) / "inbox.sqlite",
            mcp_create_bearer=BEARER,
            bot_token=TOKEN,
            clock=clock,
        )
        self.addCleanup(inbox.close)
        return inbox

    def restart(self, *, advance: float = 0.0) -> None:
        self.inbox.close()
        self.api = NavigatorAPI()
        self.clock = FakeClock(self.clock.now + advance)
        self.inbox = self._inbox(self.clock)
        self.adapter = tg.TelegramAdapter(self.inbox, self.api, owner_id=OWNER, bot_token=TOKEN)
        self.inbox._notifier = self.adapter
        self.inbox._telegram = self.adapter

    # --- helpers ---------------------------------------------------------

    def create(self, title: str = "Cut over?", **fields):
        args = {"title": title, "external_id": f"card-{uuid4()}"}
        args.update(fields)
        return self.inbox.call_tool("request_approval", args, bearer=BEARER)

    def create_many(self, count: int) -> list[dict]:
        """Create `count` Cards one clock second apart, so their order is the list order."""
        cards = []
        for index in range(count):
            self.clock.now += 1
            cards.append(self.create(f"Card {index}"))
        return cards

    def ask(self, question: str, **fields):
        args = {"question": question, "external_id": f"ask-{uuid4()}"}
        args.update(fields)
        return self.inbox.call_tool("ask_question", args, bearer=BEARER)

    def pending(self) -> None:
        replies = self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        self.assertEqual(replies, [])

    def navigator(self) -> dict:
        return self.inbox.load_navigator() or {}

    def nav_id(self) -> int:
        return self.navigator()["message_id"]

    def markup(self) -> dict:
        return self.navigator().get("published_markup") or EMPTY

    def button(self, label: str) -> str:
        for row in self.markup().get("inline_keyboard", []):
            for button in row:
                if label in button["text"]:
                    return button["callback_data"]
        raise AssertionError(f"no button {label!r} in {self.markup()}")

    def has_button(self, label: str) -> bool:
        return any(
            label in button["text"]
            for row in self.markup().get("inline_keyboard", [])
            for button in row
        )

    def page_ids(self) -> list[str]:
        rows = self.markup().get("inline_keyboard", [])
        controls = {"Previous", "Refresh", "Next"}
        return [
            tg.decode_navigator_callback(row[0]["callback_data"])["card_id"]
            for row in rows
            if row and not any(control in row[0]["text"] for control in controls)
        ]

    def screen(self) -> str:
        message_id = self.nav_id()
        for message in reversed(self.api.messages):
            if message["message_id"] == message_id:
                return message["text"]
        return self.navigator().get("published_text") or ""

    def tap(self, data: str, *, message_id: int | None = None, callback_id: str = "cb") -> None:
        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": OWNER},
                    "data": data,
                    "message": {
                        "message_id": self.nav_id() if message_id is None else message_id,
                        "chat": {"id": OWNER, "type": "private"},
                    },
                }
            }
        )

    def reply(self, message_id: int, text: str = "my own answer") -> None:
        self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "reply_to_message": {"message_id": message_id},
                    "text": text,
                }
            }
        )

    def tick(self, advance: float = 1.0) -> None:
        self.adapter.refresh_navigator(time.time() + advance)

    def stored(self, request_id: str) -> dict:
        with closing(sqlite3.connect(self.inbox.store_path)) as conn:
            row = conn.execute(
                "SELECT payload FROM cards WHERE request_id = ?", (request_id,)
            ).fetchone()
        return json.loads(row[0])

    def seed(self, payloads: list[dict]) -> None:
        self.inbox.close()
        store = Store(self.inbox.store_path)
        store.open()
        try:
            for payload in payloads:
                store.save_card(payload)
        finally:
            store.close()

    def card_message_id(self, request_id: str) -> int:
        return self.stored(request_id)["telegram_message_id"]

    def open_card(self, request_id: str) -> None:
        self.tap(tg.encode_navigator_callback("O", request_id))

    def answer_button(self, request_id: str, version: int, choice: int) -> str:
        return tg.encode_navigator_callback("A", request_id, version, choice)


class TestPendingOrder(NavigatorCase):
    def test_pending_order_is_creation_order_across_restart_and_update(self):
        first = self.create("First")
        self.clock.now += 10
        second = self.create("Second")
        self.clock.now += 10
        third = self.create("Third")
        self.assertEqual(
            [card.request_id for card in self.inbox.pending_cards()],
            [first["request_id"], second["request_id"], third["request_id"]],
        )
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": third["request_id"],
                "expected_version": third["version"],
                "title": "Third, revised",
                "expires_in_seconds": 3600,
            },
        )
        created_before = {
            card["request_id"]: self.stored(card["request_id"])["created_at"]
            for card in (first, second, third)
        }
        self.restart(advance=500)
        self.assertEqual(
            [card.request_id for card in self.inbox.pending_cards()],
            [first["request_id"], second["request_id"], third["request_id"]],
        )
        for request_id, created_at in created_before.items():
            self.assertEqual(self.stored(request_id)["created_at"], created_at)

    def test_unedited_legacy_card_orders_by_true_creation(self):
        older, younger = str(uuid4()), str(uuid4())
        self.seed(
            [
                # 900 s of life would already be over: keep both in the future.
                legacy_payload(older, expires_at=1_700_007_200.0, ttl=14_400.0),
                legacy_payload(younger, expires_at=1_700_010_000.0, ttl=14_400.0),
            ]
        )
        self.clock.now = 1_700_000_000.0
        fresh = self.create("Fresh")
        self.assertEqual(
            [card.request_id for card in self.inbox.pending_cards()],
            [older, younger, fresh["request_id"]],
        )
        self.assertEqual(self.stored(older)["created_at"], 1_699_992_800.0)
        self.assertEqual(self.stored(younger)["created_at"], 1_699_995_600.0)

    def test_revised_legacy_card_uses_fixed_estimate(self):
        revised, younger = str(uuid4()), str(uuid4())
        self.seed(
            [
                # A TTL edit moved this Card's expiry, so its true creation is
                # gone: the estimate is expires_at - expires_in_seconds, and it
                # lands AFTER a Card whose creation is actually later.
                legacy_payload(revised, version=2, expires_at=1_700_007_200.0, ttl=14_400.0),
                legacy_payload(younger, version=1, expires_at=1_700_004_000.0, ttl=14_400.0),
            ]
        )
        self.clock.now = 1_700_000_000.0
        order = [card.request_id for card in self.inbox.pending_cards()]
        self.assertEqual(order, [younger, revised])
        self.assertEqual(self.stored(revised)["created_at"], 1_699_992_800.0)
        self.assertEqual(self.stored(younger)["created_at"], 1_699_989_600.0)
        self.assertEqual(self.stored(revised)["version"], 2)

        self.inbox.call_tool(
            "update_request",
            {
                "request_id": revised,
                "expected_version": 2,
                "expires_in_seconds": 3600,
            },
        )
        self.restart(advance=50)
        self.assertEqual(self.stored(revised)["created_at"], 1_699_992_800.0)
        self.assertEqual(
            [card.request_id for card in self.inbox.pending_cards()], order
        )

    def test_revised_legacy_card_load_failure_serves_no_estimate(self):
        revised = str(uuid4())
        self.seed(
            [
                legacy_payload(
                    revised, version=2, expires_at=1_700_007_200.0, ttl=14_400.0
                )
            ]
        )
        with mock.patch.object(Store, "save_card", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.inbox.call_tool("list_pending", {})
        self.assertIsNone(self.inbox._store)
        pending = self.inbox.call_tool("list_pending", {})
        self.assertEqual([item["request_id"] for item in pending], [revised])
        self.assertEqual(self.stored(revised)["created_at"], 1_699_992_800.0)
        self.restart()
        self.assertEqual(
            [card.request_id for card in self.inbox.pending_cards()], [revised]
        )

    def test_closed_anchor_resolves_by_retained_card_key(self):
        cards = self.create_many(3)
        first, second, third = (card["request_id"] for card in cards)
        self.inbox.call_tool("cancel_request", {"request_id": first})
        page, total, next_anchor, _expired = self.inbox.pending_page(first)
        self.assertEqual([card.request_id for card in page], [second, third])
        self.assertEqual(total, 2)
        self.assertIsNone(next_anchor)

    def test_page_after_restart_reaches_every_card_once(self):
        cards = self.create_many(10)
        self.pending()
        seen = list(self.page_ids())
        self.assertEqual(seen, [card["request_id"] for card in cards[:8]])
        self.restart()
        self.assertEqual(
            [card.request_id for card in self.inbox.pending_cards()],
            [card["request_id"] for card in cards],
        )
        self.tap(self.button("Next"), callback_id="next-after-restart")
        page = self.page_ids()
        self.assertEqual(
            page, [cards[8]["request_id"], cards[9]["request_id"]]
        )
        self.assertEqual(seen + page, [card["request_id"] for card in cards])

    def test_order_tie_breaks_on_request_id(self):
        self.clock.now = 1_700_000_000.0
        cards = [self.create(f"Tie {index}") for index in range(3)]
        expected = sorted(card["request_id"] for card in cards)
        self.assertEqual(
            [card.request_id for card in self.inbox.pending_cards()], expected
        )

    def test_list_pending_tool_order_and_output_unchanged(self):
        first = self.create("First")
        second = self.create("Second")
        pending = self.inbox.call_tool("list_pending", {})
        self.assertEqual(
            [item["request_id"] for item in pending],
            [second["request_id"], first["request_id"]],
        )
        self.assertEqual(
            set(pending[0]),
            {
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
            },
        )
        self.assertEqual(self.inbox.call_tool("list_unprocessed", {}), [])

    def test_prompt_binding_survives_restart(self):
        card = self.create("Bound")
        self.inbox.record_reply_prompt(OWNER, 4242, card["request_id"], 1)
        self.restart()
        self.assertEqual(
            self.inbox.reply_prompt(OWNER, 4242), (card["request_id"], 1)
        )
        self.reply(4242, "after the restart")
        answered = self.inbox.call_tool(
            "get_response", {"request_id": card["request_id"]}
        )
        self.assertEqual(answered["response"]["text"], "after the restart")
        self.assertEqual(answered["response"]["responded_via"], "telegram-reply")

    def test_created_at_is_ignored_by_payload_consumers(self):
        card = self.create("Payload")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        self.tap(self.answer_button(request_id, 1, 0), callback_id="answer")
        payload = self.stored(request_id)
        self.assertIsInstance(payload["created_at"], float)
        self.assertEqual(payload["kind"], "approval")
        self.assertEqual(payload["state"], "tapped")
        self.assertEqual(payload["responded_via"], "telegram")
        self.assertEqual(payload["response_choice"], "Approve")
        self.assertIsNone(payload["response_text"])
        self.assertEqual(payload["telegram_chat_id"], OWNER)
        self.assertIsInstance(payload["telegram_message_id"], int)
        self.assertIsInstance(payload["responded_at"], str)


class TestPendingEntryPoint(NavigatorCase):
    def test_pending_command_sends_pins_and_records_one_navigator(self):
        self.create("First")
        self.pending()
        state = self.navigator()
        self.assertEqual(state["message_id"], self.api.messages[-1]["message_id"])
        self.assertEqual(self.api.pins, [(OWNER, state["message_id"])])
        self.assertIn("Pending: 1", self.screen())
        self.assertEqual(state["view"], {"kind": "list", "anchor": None})

    def test_pending_with_botname_suffix_is_accepted(self):
        self.create("First")
        self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending@paraphe_test_bot",
                }
            }
        )
        self.assertEqual(self.navigator()["message_id"], self.api.messages[-1]["message_id"])

    def test_second_pending_retires_only_previous_keyboard_and_pin(self):
        self.create("First")
        self.pending()
        first = self.nav_id()
        first_text = self.api.messages[first - 1]["text"]
        self.pending()
        second = self.nav_id()
        self.assertNotEqual(first, second)
        self.assertIn(
            {"chat_id": OWNER, "message_id": first, "reply_markup": EMPTY},
            self.api.edits,
        )
        self.assertIn((OWNER, second), self.api.pins)
        self.assertEqual([message for _chat, message in self.api.unpins], [first])
        self.assertEqual(self.api.messages[first - 1]["text"], first_text)
        self.assertEqual(len(self.api.messages), 3)  # card, navigator, navigator

    def test_retirement_after_manual_unpin_is_idempotent(self):
        self.create("First")
        self.pending()
        first = self.nav_id()
        self.api.strip_errors[first] = NotifyRejected("gone", code=400)
        self.api.unpin_errors = [NotifyRejected("gone", code=400)]
        output = io.StringIO()
        with redirect_stdout(output):
            self.pending()
        second = self.nav_id()
        self.assertNotEqual(first, second)
        self.assertIn((OWNER, second), self.api.pins)
        self.assertIn("Telegram navigator strip failed: NotifyRejected", output.getvalue())
        self.assertIn("Telegram navigator unpin failed: NotifyRejected", output.getvalue())

    def test_failed_send_keeps_previous_navigator_active(self):
        self.create("First")
        self.pending()
        first = self.nav_id()
        self.api.send_errors = [NotifyRejected("rejected", code=500)]
        with self.assertRaises(NotifyRejected):
            self.pending()
        self.assertEqual(self.nav_id(), first)
        self.assertTrue(
            self.adapter._nav_edit.acquire(timeout=1), "_nav_edit stayed held"
        )
        self.adapter._nav_edit.release()
        # A following tick proceeds: a new Card changes the list and is edited.
        self.create("Second")
        before = len(self.api.texts)
        self.tick()
        self.assertEqual(len(self.api.texts), before + 1)
        self.assertIn("Pending: 2", self.screen())
        # And a second /pending activates normally; a later update is handled.
        self.pending()
        self.assertNotEqual(self.nav_id(), first)
        self.assertEqual(
            self.adapter.handle_update(
                {
                    "message": {
                        "from": {"id": OWNER},
                        "chat": {"id": OWNER, "type": "private"},
                        "text": "/config",
                    }
                }
            ),
            ["ttl and owner knobs only"],
        )

    def test_navigator_save_failure_keeps_previous_active(self):
        self.create("First")
        self.pending()
        first = self.nav_id()
        self.api.pin_errors = []
        output = io.StringIO()
        with mock.patch.object(self.inbox, "save_navigator", side_effect=OSError("disk full")):
            with redirect_stdout(output):
                self.pending()
        # The send happened; nothing was activated.
        new_message_id = self.api.messages[-1]["message_id"]
        self.assertNotEqual(new_message_id, first)
        self.assertEqual(self.nav_id(), first)
        self.assertIn(
            "Telegram navigator activation failed: OSError", output.getvalue()
        )
        self.assertEqual(self.api.pins, [(OWNER, first)])
        self.assertEqual(self.api.unpins, [])
        self.assertTrue(self.adapter._nav_edit.acquire(timeout=1))
        self.adapter._nav_edit.release()
        # The unrecorded message's buttons are refused and its keyboard stripped.
        self.tap(
            tg.encode_navigator_callback("R"),
            message_id=new_message_id,
            callback_id="stray",
        )
        self.assertIn(
            {"chat_id": OWNER, "message_id": new_message_id, "reply_markup": EMPTY},
            self.api.edits,
        )
        self.assertEqual(self.nav_id(), first)
        # A following tick still refreshes the previous navigator.
        self.create("Second")
        before = len(self.api.texts)
        self.tick()
        self.assertEqual(len(self.api.texts), before + 1)
        self.assertEqual(self.api.texts[-1]["message_id"], first)
        # A second /pending activates normally, in memory and durably.
        self.pending()
        second = self.nav_id()
        self.assertNotEqual(second, first)
        self.assertEqual(self.api.unpins, [(OWNER, first)])
        self.restart()
        self.assertEqual(self.nav_id(), second)
        self.assertEqual(self.navigator()["message_id"], second)

    def test_old_navigator_callback_is_refused_and_stripped(self):
        self.create("First")
        self.pending()
        first = self.nav_id()
        self.pending()
        second = self.nav_id()
        controls = self.markup()["inline_keyboard"][-1][0]["callback_data"]
        before = len(self.api.texts)
        self.tap(controls, message_id=first, callback_id="old")
        self.assertEqual(len(self.api.texts), before)
        self.assertIn(
            {"chat_id": OWNER, "message_id": first, "reply_markup": EMPTY},
            self.api.edits,
        )
        self.assertEqual(self.api.answers[-1], "old")
        self.assertEqual(self.nav_id(), second)


class TestPendingList(NavigatorCase):
    def test_list_shows_total_rows_and_empty_state(self):
        self.pending()
        self.assertIn("Pending: 0", self.screen())
        self.assertIn("Nothing is waiting for an answer.", self.screen())
        self.assertEqual(
            [button["text"] for row in self.markup()["inline_keyboard"] for button in row],
            ["Refresh"],
        )
        self.create("Cut over?")
        self.clock.now += 1
        self.create("Ship it?", agent_name="Hermes", repo="paraphe", risk="high")
        self.tap(self.button("Refresh"), callback_id="refresh")
        text = self.screen()
        self.assertIn("Pending: 2", text)
        self.assertIn("1. <b>Cut over?</b>", text)
        self.assertIn("2. <b>Ship it?</b> — Hermes · paraphe · risk high", text)

    def test_paging_reaches_every_card_once(self):
        cards = self.create_many(10)
        self.pending()
        seen = list(self.page_ids())
        self.assertEqual(len(seen), 8)
        self.tap(self.button("Next"), callback_id="next")
        seen += self.page_ids()
        self.assertEqual(sorted(seen), sorted(card["request_id"] for card in cards))
        self.assertEqual(len(seen), len(set(seen)))
        self.tap(self.button("Previous"), callback_id="prev")
        self.assertEqual(self.page_ids(), seen[:8])
        self.assertEqual(self.markup()["inline_keyboard"][-1][0]["text"], "Previous")

    def test_page_anchor_survives_earlier_row_removal(self):
        cards = self.create_many(10)
        self.pending()
        anchor = self.markup()["inline_keyboard"][-1][-1]["callback_data"]
        self.tap(anchor, callback_id="next")
        first_on_page = self.page_ids()[0]
        self.inbox.call_tool("cancel_request", {"request_id": first_on_page})
        self.tap(self.button("Refresh"), callback_id="refresh")
        page = self.page_ids()
        self.assertNotIn(first_on_page, page)
        # The closed anchor still resolves by its retained key: the page is the
        # one Card after it, not the first page.
        self.assertEqual(page, [cards[9]["request_id"]])

    def test_empty_page_after_removals_falls_back(self):
        cards = self.create_many(10)
        self.pending()
        self.tap(self.button("Next"), callback_id="next")
        for card in cards[7:]:
            self.inbox.call_tool("cancel_request", {"request_id": card["request_id"]})
        self.tap(self.button("Refresh"), callback_id="refresh")
        self.assertIn("Pending: 7", self.screen())
        self.assertEqual(self.page_ids(), [card["request_id"] for card in cards[:7]])

    def test_every_list_page_has_refresh(self):
        for count in (0, 1, 8, 10):
            with self.subTest(count=count):
                self.setUp()
                for index in range(count):
                    self.create(f"Card {index}")
                self.pending()
                self.assertTrue(self.has_button("Refresh"))
                if count > 8:
                    self.tap(self.button("Next"), callback_id=f"next-{count}")
                    self.assertTrue(self.has_button("Refresh"))


class TestPendingCardView(NavigatorCase):
    def test_open_back_refresh_edit_one_message_and_never_send(self):
        card = self.create("Open me")
        self.pending()
        sends = len(self.api.messages)
        self.open_card(card["request_id"])
        self.assertEqual(len(self.api.messages), sends)
        self.assertIn("<b>Open me</b>", self.screen())
        self.assertEqual(self.api.texts[-1]["message_id"], self.nav_id())
        self.tap(self.button("Back to Pending"), callback_id="back")
        self.assertIn("Pending: 1", self.screen())
        self.tap(self.button("Refresh"), callback_id="refresh")
        self.assertEqual(len(self.api.messages), sends)

    def test_navigation_never_taps_renews_or_revises(self):
        card = self.create("Untouched")
        request_id = card["request_id"]
        before = self.stored(request_id)
        self.pending()
        self.open_card(request_id)
        self.tap(self.button("Back to Pending"), callback_id="back")
        self.tap(self.button("Refresh"), callback_id="refresh")
        self.open_card(request_id)
        self.tap(self.button("Back to Pending"), callback_id="back-again")
        after = self.stored(request_id)
        for key in ("version", "expires_at", "notified", "state", "response_choice", "created_at"):
            self.assertEqual(after[key], before[key], key)

    def test_open_oversized_emoji_card_reuses_budget_and_trim(self):
        card = self.create("Emoji", details="🧵" * 2000)
        self.pending()
        self.open_card(card["request_id"])
        text = self.screen()
        self.assertIn(tg.TRIM_MARKER, text)
        # The card body stays inside the renderer's budget; the navigator's
        # bounded status line rides in the platform headroom above it.
        self.assertLessEqual(
            tg.message_units(self.navigator()["published_text"]), tg.MESSAGE_BUDGET
        )
        self.assertLessEqual(tg.message_units(text), tg.PLATFORM_LIMIT)

    def test_card_view_hint_points_to_write_answer(self):
        card = self.create("Write hint")
        self.pending()
        self.open_card(card["request_id"])
        self.assertIn(tg.WRITE_HINT, self.screen())
        self.assertNotIn(tg.REPLY_HINT, self.screen())

    def test_open_closed_card_shows_reason_and_list(self):
        card = self.create("Closed")
        self.pending()
        self.inbox.call_tool("cancel_request", {"request_id": card["request_id"]})
        self.open_card(card["request_id"])
        self.assertIn("This card is closed: cancelled", self.screen())
        self.assertTrue(self.has_button("Review again"))
        self.tap(self.button("Back to Pending"), callback_id="back")
        self.assertIn("Pending: 0", self.screen())


class TestPendingAnswers(NavigatorCase):
    def test_answer_from_navigator_records_once_and_strips_original(self):
        card = self.create("Answer me")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        self.tap(self.answer_button(request_id, 1, 0), callback_id="answer")
        stored = self.stored(request_id)
        self.assertEqual(stored["state"], "tapped")
        self.assertEqual(stored["response_choice"], "Approve")
        self.assertEqual(stored["responded_via"], "telegram")
        self.assertIn(
            {
                "chat_id": OWNER,
                "message_id": stored["telegram_message_id"],
                "reply_markup": EMPTY,
            },
            self.api.edits,
        )
        self.assertIn("Answer recorded: Approve.", self.screen())

    def test_original_and_navigator_concurrent_answers_store_one(self):
        card = self.create("Race")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        original = tg.encode_callback_data(request_id, 1, 1)
        navigator = self.answer_button(request_id, 1, 0)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def fire(data: str, callback_id: str) -> None:
            try:
                barrier.wait(2)
                self.adapter.handle_update(
                    {
                        "callback_query": {
                            "id": callback_id,
                            "from": {"id": OWNER},
                            "data": data,
                            "message": {
                                "message_id": 1,
                                "chat": {"id": OWNER, "type": "private"},
                            },
                        }
                    }
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=fire, args=(original, "one")),
            threading.Thread(target=fire, args=(navigator, "two")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        stored = self.stored(request_id)
        self.assertEqual(stored["state"], "tapped")
        self.assertIn(stored["response_choice"], {"Approve", "Deny"})
        self.assertEqual(len(self.inbox.call_tool("list_unprocessed", {})), 1)

    def test_answer_shows_confirmation_and_list_never_next_card(self):
        first = self.create("First")
        second = self.create("Second")
        self.pending()
        self.open_card(first["request_id"])
        self.tap(self.answer_button(first["request_id"], 1, 0), callback_id="answer")
        self.assertIn("Answer recorded: Approve.", self.screen())
        self.assertIn("Pending: 1", self.screen())
        self.assertNotIn("<b>First</b>", self.screen())
        self.tick()
        self.assertIn("Answer recorded: Approve.", self.screen())
        self.tap(self.button("Refresh"), callback_id="refresh")
        self.assertIn("Answer recorded: Approve.", self.screen())
        self.restart()
        self.adapter.refresh_navigator(time.time())
        self.assertEqual(
            self.navigator()["notice"], "Answer recorded: Approve."
        )
        self.open_card(second["request_id"])
        self.assertIsNone(self.navigator()["notice"])

    def test_stale_answer_records_nothing_and_explains(self):
        cases = ("changed", "expired", "cancelled", "answered")
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                card = self.create("Stale")
                request_id = card["request_id"]
                self.pending()
                self.open_card(request_id)
                data = self.answer_button(request_id, 1, 0)
                if case == "changed":
                    self.inbox.call_tool(
                        "update_request",
                        {"request_id": request_id, "expected_version": 1, "title": "New"},
                    )
                elif case == "expired":
                    self.clock.now += 100_000
                elif case == "cancelled":
                    self.inbox.call_tool("cancel_request", {"request_id": request_id})
                else:
                    self.tap(data, callback_id="first")
                self.tap(data, callback_id=f"stale-{case}")
                stored = self.stored(request_id)
                if case != "answered":
                    self.assertNotEqual(stored["state"], "tapped")
                self.assertIn("Not recorded:", self.screen())

    def test_replayed_answer_callback_records_once(self):
        card = self.create("Replay")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        data = self.answer_button(request_id, 1, 0)
        self.tap(data, callback_id="one")
        first = self.stored(request_id)["response_choice"]
        self.tap(data, callback_id="two")
        self.assertEqual(self.stored(request_id)["response_choice"], first)
        self.assertIn("Not recorded: this card was already answered.", self.screen())

    def test_original_card_reply_and_callback_unchanged(self):
        card = self.create("Original")
        request_id = card["request_id"]
        self.reply(self.card_message_id(request_id), "by reply")
        answered = self.inbox.call_tool("get_response", {"request_id": request_id})
        self.assertEqual(answered["response"]["text"], "by reply")
        self.assertEqual(answered["response"]["responded_via"], "telegram-reply")

        other = self.create("Original tap")
        other_id = other["request_id"]
        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": "p1",
                    "from": {"id": OWNER},
                    "data": tg.encode_callback_data(other_id, 1, 0),
                    "message": {
                        "message_id": self.card_message_id(other_id),
                        "chat": {"id": OWNER, "type": "private"},
                    },
                }
            }
        )
        self.assertEqual(
            self.inbox.call_tool("get_response", {"request_id": other_id})["response"][
                "choice"
            ],
            "Approve",
        )


class TestPendingPrompts(NavigatorCase):
    def write_prompt(self, request_id: str, version: int = 1) -> int:
        self.tap(tg.encode_navigator_callback("W", request_id, version), callback_id="write")
        return self.api.messages[-1]["message_id"]

    def test_write_answer_prompt_binds_card_and_revision(self):
        card = self.create("Bound write")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        prompt_id = self.write_prompt(request_id)
        prompt = self.api.messages[-1]
        self.assertEqual(
            prompt["reply_markup"],
            {"force_reply": True, "input_field_placeholder": "Your answer"},
        )
        self.assertIn("Write your answer to: Bound write", prompt["text"])
        self.assertEqual(self.inbox.reply_prompt(OWNER, prompt_id), (request_id, 1))
        self.reply(prompt_id, "in my own words")
        answered = self.inbox.call_tool("get_response", {"request_id": request_id})
        self.assertEqual(answered["response"]["text"], "in my own words")
        self.assertEqual(answered["response"]["responded_via"], "telegram-reply")
        self.assertIsNone(answered["response"]["choice"])

    def test_write_answer_on_revised_or_closed_card_sends_no_prompt(self):
        card = self.create("Gone")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        self.inbox.call_tool("update_request", {"request_id": request_id, "expected_version": 1, "title": "New"})
        sends = len(self.api.messages)
        self.write_prompt(request_id, version=1)
        self.assertEqual(len(self.api.messages), sends)
        self.assertIn("This card changed — review it again", self.screen())

    def test_prompt_reply_after_selection_change_answers_bound_card(self):
        first = self.create("First")
        second = self.create("Second")
        self.pending()
        self.open_card(first["request_id"])
        prompt_id = self.write_prompt(first["request_id"])
        self.open_card(second["request_id"])
        self.reply(prompt_id, "still the first")
        self.assertEqual(
            self.inbox.call_tool("get_response", {"request_id": first["request_id"]})[
                "response"
            ]["text"],
            "still the first",
        )
        self.assertEqual(
            self.inbox.call_tool("get_response", {"request_id": second["request_id"]})[
                "response"
            ],
            None,
        )

    def test_prompt_reply_after_restart_answers_bound_card(self):
        card = self.create("Restart write")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        prompt_id = self.write_prompt(request_id)
        self.restart()
        self.reply(prompt_id, "after restart")
        self.assertEqual(
            self.inbox.call_tool("get_response", {"request_id": request_id})["response"][
                "text"
            ],
            "after restart",
        )

    def test_prompt_reply_after_revision_is_refused_with_notice(self):
        card = self.create("Revised write")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        prompt_id = self.write_prompt(request_id)
        self.inbox.call_tool(
            "update_request", {"request_id": request_id, "expected_version": 1, "title": "New"}
        )
        before = len(self.api.messages)
        self.reply(prompt_id, "too late")
        self.assertEqual(len(self.api.messages), before + 1)
        self.assertEqual(self.api.messages[-1]["text"], tg.NAVIGATOR_DENIED)
        self.assertIsNone(
            self.inbox.call_tool("get_response", {"request_id": request_id})["response"]
        )

    def test_send_ok_binding_failed_prompt_reply_is_refused(self):
        card = self.create("Unbound")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        output = io.StringIO()
        with mock.patch.object(
            self.inbox, "record_reply_prompt", side_effect=OSError("disk full")
        ), redirect_stdout(output):
            prompt_id = self.write_prompt(request_id)
        self.assertIsNone(self.inbox.reply_prompt(OWNER, prompt_id))
        self.reply(prompt_id, "records nothing")
        self.assertIsNone(
            self.inbox.call_tool("get_response", {"request_id": request_id})["response"]
        )

    def test_reply_to_navigator_is_refused_with_hint(self):
        card = self.create("Not an answer")
        self.pending()
        before = len(self.api.messages)
        self.reply(self.nav_id(), "this is not an answer")
        self.assertEqual(len(self.api.messages), before + 1)
        self.assertIn("Write answer", self.api.messages[-1]["text"])
        self.assertIsNone(
            self.inbox.call_tool("get_response", {"request_id": card["request_id"]})[
                "response"
            ]
        )

    def test_prompt_reply_payload_keeps_guard_fields(self):
        card = self.create("Guard fields")
        request_id = card["request_id"]
        original = self.stored(request_id)
        self.pending()
        self.open_card(request_id)
        prompt_id = self.write_prompt(request_id)
        self.reply(prompt_id, "guarded")
        payload = self.stored(request_id)
        self.assertEqual(payload["responded_via"], "telegram-reply")
        self.assertIsNone(payload["response_choice"])
        self.assertEqual(payload["response_text"], "guarded")
        self.assertEqual(payload["telegram_chat_id"], original["telegram_chat_id"])
        self.assertEqual(payload["telegram_message_id"], original["telegram_message_id"])


class TestPendingAuthorization(NavigatorCase):
    def test_non_owner_group_and_forwarded_inputs_are_ignored(self):
        card = self.create("Guarded")
        request_id = card["request_id"]
        self.pending()
        navigator = self.nav_id()
        sends = len(self.api.messages)
        for message in (
            {"from": {"id": STRANGER}, "chat": {"id": STRANGER, "type": "private"}},
            {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "group"}},
            {
                "from": {"id": OWNER},
                "chat": {"id": OWNER, "type": "private"},
                "forward_origin": {"type": "user"},
            },
        ):
            self.adapter.handle_update({"message": {**message, "text": "/pending"}})
        self.assertEqual(len(self.api.messages), sends)
        self.assertEqual(self.nav_id(), navigator)

        self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "reply_to_message": {"message_id": navigator},
                    "forward_origin": {"type": "user"},
                    "text": "forwarded reply",
                }
            }
        )
        self.assertEqual(len(self.api.messages), sends)

        texts = len(self.api.texts)
        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": "forwarded",
                    "from": {"id": OWNER},
                    "data": tg.encode_navigator_callback("R"),
                    "message": {
                        "message_id": navigator,
                        "chat": {"id": OWNER, "type": "private"},
                        "forward_origin": {"type": "user"},
                    },
                }
            }
        )
        self.assertEqual(len(self.api.texts), texts)
        self.assertEqual(self.api.answers[-1], "forwarded")

        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": "stranger",
                    "from": {"id": STRANGER},
                    "data": tg.encode_navigator_callback("R"),
                    "message": {
                        "message_id": navigator,
                        "chat": {"id": OWNER, "type": "private"},
                    },
                }
            }
        )
        self.assertEqual(len(self.api.texts), texts)
        self.assertIsNone(
            self.inbox.call_tool("get_response", {"request_id": request_id})["response"]
        )

    def test_create_credential_tool_surface_unchanged(self):
        self.assertEqual(
            TOOL_NAMES,
            [
                "how_to_use",
                "ask_question",
                "request_approval",
                "get_response",
                "list_unprocessed",
                "list_pending",
                "mark_processed",
                "report_execution",
                "update_request",
                "cancel_request",
                "notify_user",
                "request_feedback",
            ],
        )
        self.assertEqual(sorted(TOOL_SCHEMAS), sorted(TOOL_NAMES))
        self.assertNotIn("pending_navigator", TOOL_NAMES)
        card = self.create("Credential")
        with self.assertRaises(inbox_mod.InboxError):
            self.inbox.call_tool("claim", {"request_id": card["request_id"]}, bearer=BEARER)
        with self.assertRaises(inbox_mod.ClaimRefused):
            self.inbox.claim(card["request_id"], version=1, choice="Approve")
        self.assertEqual(
            self.inbox.call_tool("get_response", {"request_id": card["request_id"]})[
                "status"
            ],
            "pending",
        )


class TestPendingRefresh(NavigatorCase):
    def test_refresh_reflects_create_update_answer_cancel_expire(self):
        card = self.create("Watched")
        request_id = card["request_id"]
        self.pending()
        self.tick()
        self.assertIn("Pending: 1", self.screen())

        self.create("Added")
        self.tick()
        self.assertIn("Pending: 2", self.screen())

        self.inbox.call_tool(
            "update_request", {"request_id": request_id, "expected_version": 1, "title": "Renamed"}
        )
        self.tick()
        self.assertIn("<b>Renamed</b>", self.screen())

        self.open_card(request_id)
        self.tap(self.answer_button(request_id, 2, 0), callback_id="answer")
        self.assertIn("Pending: 1", self.screen())

        other = self.create("Cancelled")
        self.tick()
        self.inbox.call_tool("cancel_request", {"request_id": other["request_id"]})
        self.tick()
        self.assertIn("Pending: 1", self.screen())

        expiring = self.create("Expiring")
        self.tick()
        observed: dict[str, bool] = {}

        def probe() -> None:
            observed["nav"] = self.adapter._nav_edit.acquire(timeout=1)
            if observed["nav"]:
                self.adapter._nav_edit.release()
            observed["inbox"] = self.inbox._lock.acquire(timeout=1)
            if observed["inbox"]:
                self.inbox._lock.release()

        def on_strip(_chat_id: int, _message_id: int) -> None:
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(2)
            self.assertTrue(observed.get("nav"), "navigator lock held during strip")
            self.assertTrue(observed.get("inbox"), "inbox lock held during strip")

        self.api.on_strip = on_strip
        self.clock.now += 100_000
        self.tick()
        self.assertIn("Pending: 0", self.screen())
        self.assertIn(
            {
                "chat_id": OWNER,
                "message_id": self.stored(expiring["request_id"])["telegram_message_id"],
                "reply_markup": EMPTY,
            },
            self.api.edits,
        )
        self.api.on_strip = None

    def test_refresh_coalesces_and_skips_unchanged(self):
        self.create("Watched")
        self.pending()
        before = len(self.api.texts)
        self.tick(0.1)
        self.tick(0.2)
        self.assertEqual(len(self.api.texts), before)
        self.create("Added")
        self.tick(1.0)
        self.assertEqual(len(self.api.texts), before + 1)
        self.tick(1.1)
        self.assertEqual(len(self.api.texts), before + 1)

    def test_tick_and_refresh_without_change_make_no_api_call(self):
        self.create("Watched")
        self.pending()
        published = self.navigator()["published_at"]
        sends, pins, unpins = len(self.api.messages), list(self.api.pins), list(self.api.unpins)
        before = len(self.api.texts)
        self.tick()
        self.tap(self.button("Refresh"), callback_id="refresh")
        self.assertEqual(len(self.api.texts), before)
        self.assertEqual(len(self.api.messages), sends)
        self.assertEqual(self.api.pins, pins)
        self.assertEqual(self.api.unpins, unpins)
        self.assertEqual(self.navigator()["published_at"], published)

    def test_failed_edit_does_not_advance_updated_time(self):
        card = self.create("Watched")
        self.pending()
        published = self.navigator()["published_at"]
        self.api.edit_errors = [NotifyRejected("boom", code=502)]
        self.create("Added")
        self.tick()
        self.assertEqual(self.navigator()["published_at"], published)
        self.assertIn("Pending: 1", self.navigator()["published_text"])
        self.tick(30)
        self.assertIn("Pending: 2", self.screen())
        self.assertGreater(self.navigator()["published_at"], published)

    def test_refresh_never_pins_or_unpins(self):
        self.create("Watched")
        self.pending()
        pins, unpins = list(self.api.pins), list(self.api.unpins)
        for _ in range(3):
            self.create("Added")
            self.tick()
        self.assertEqual(self.api.pins, pins)
        self.assertEqual(self.api.unpins, unpins)

    def test_selected_card_not_replaced_by_unrelated_change(self):
        first = self.create("First")
        self.clock.now += 1
        second = self.create("Second")
        self.pending()
        pending_event, release = threading.Event(), threading.Event()

        def on_edit() -> None:
            pending_event.set()
            release.wait(3)

        self.api.on_edit = on_edit
        self.create("Unrelated")
        worker = threading.Thread(target=lambda: self.adapter.refresh_navigator(time.time()))
        worker.start()
        try:
            self.assertTrue(pending_event.wait(2))
            # The open-card tap lands its intent while the list edit is in flight.
            self.tap(
                tg.encode_navigator_callback("O", second["request_id"]),
                callback_id="open-second",
            )
            self.assertEqual(self.navigator()["view"]["request_id"], second["request_id"])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive(), "the refresh edit never finished")
        self.api.on_edit = None
        self.assertEqual(self.navigator()["view"]["request_id"], second["request_id"])
        self.assertIn("<b>Second</b>", self.screen())
        self.assertNotIn("Unrelated", self.screen())

    def test_selected_card_revised_or_closed_invalidates_view(self):
        card = self.create("Selected")
        request_id = card["request_id"]
        self.pending()
        self.open_card(request_id)
        self.inbox.call_tool(
            "update_request", {"request_id": request_id, "expected_version": 1, "title": "New"}
        )
        self.tick()
        self.assertIn("This card changed — review it again", self.screen())
        self.assertTrue(self.has_button("Review again"))
        self.assertFalse(self.has_button("Write answer"))

    def test_429_and_5xx_retry_with_backoff_and_never_suspend(self):
        card = self.create("Watched")
        self.pending()
        base = time.time()
        self.api.edit_errors = [NotifyRejected("slow down", code=429, retry_after=5)]
        self.create("Added")
        self.adapter.refresh_navigator(base)
        self.assertGreaterEqual(self.adapter._nav_next_attempt, base + 5)
        self.assertFalse(self.navigator().get("suspended"))
        before = len(self.api.texts)
        self.adapter.refresh_navigator(base + 1)
        self.assertEqual(len(self.api.texts), before)
        self.adapter.refresh_navigator(base + 6)
        self.assertEqual(len(self.api.texts), before + 1)

        self.api.edit_errors = [NotifyRejected("boom", code=503)]
        second = self.create("Second added")
        failed_at = time.time()
        self.adapter.refresh_navigator(failed_at)
        self.assertGreaterEqual(self.adapter._nav_next_attempt, failed_at + 1)
        self.assertFalse(self.navigator().get("suspended"))
        self.api.edit_errors = [NotifyRejected("boom", code=503)]
        self.adapter.refresh_navigator(failed_at + 1)
        self.assertGreaterEqual(self.adapter._nav_next_attempt, failed_at + 1 + 2)
        self.adapter.refresh_navigator(failed_at + 30)
        self.assertIn("Pending: 3", self.screen())
        self.assertFalse(self.navigator().get("suspended"))
        self.assertEqual(self.navigator()["view"]["kind"], "list")

    def test_not_modified_edit_is_success_noop(self):
        card = self.create("Watched")
        self.pending()
        published = self.navigator()["published_at"]
        self.api.edit_errors = [
            NotifyRejected("not modified", code=400, reason="message_not_modified")
        ]
        # Force an edit attempt by clearing the stored view, as a restart could.
        state = self.navigator()
        state["published_text"] = "stale"
        self.inbox.save_navigator(state)
        self.adapter._nav_loaded = False
        self.tick()
        self.assertIn("1. <b>Watched</b>", self.navigator()["published_text"])
        self.assertEqual(self.navigator()["published_at"], published)
        self.assertFalse(self.navigator().get("suspended"))

    def test_message_not_found_edit_suspends_until_pending(self):
        card = self.create("Watched")
        self.pending()
        output = io.StringIO()
        self.api.edit_errors = [NotifyRejected("not found", code=400)]
        self.create("Added")
        with redirect_stdout(output):
            self.tick()
        self.assertIn("Telegram navigator suspended: code=400", output.getvalue())
        self.assertTrue(self.navigator()["suspended"])
        before = len(self.api.texts)
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.api.texts), before)
        self.pending()
        self.assertFalse(self.navigator()["suspended"])
        self.assertIn("Pending: 2", self.screen())

    def test_cannot_edit_or_other_400_suspends_until_pending(self):
        card = self.create("Watched")
        self.pending()
        output = io.StringIO()
        self.api.edit_errors = [NotifyRejected("cannot edit", code=403)]
        self.create("Added")
        with redirect_stdout(output):
            self.tick()
        self.assertIn("Telegram navigator suspended: code=403", output.getvalue())
        before = len(self.api.texts)
        self.tick()
        self.assertEqual(len(self.api.texts), before)
        self.pending()
        self.assertFalse(self.navigator()["suspended"])
        self.assertIn("Pending: 2", self.screen())

    def test_updated_time_is_last_successful_semantic_edit(self):
        card = self.create("Watched")
        self.pending()
        state = self.navigator()
        sent_at = state["published_at"]
        self.assertEqual(
            self.api.messages[-1]["text"].split("\n\n")[-1],
            "Updated " + time.strftime("%H:%M:%S UTC", time.gmtime(sent_at)),
        )
        self.create("Added")
        later = time.time() + 3
        self.adapter.refresh_navigator(later)
        self.assertEqual(self.navigator()["published_at"], later)
        self.assertEqual(
            self.api.texts[-1]["text"].split("\n\n")[-1],
            "Updated " + time.strftime("%H:%M:%S UTC", time.gmtime(later)),
        )
        self.api.edit_errors = [NotifyRejected("boom", code=502)]
        self.create("Third")
        failed_at = later + 10
        self.adapter.refresh_navigator(failed_at)
        self.assertEqual(self.navigator()["published_at"], later)
        before = len(self.api.texts)
        self.adapter.refresh_navigator(later + 1)
        self.assertEqual(len(self.api.texts), before)

    def test_navigator_survives_restart_and_keeps_editing_same_message(self):
        self.create("Watched")
        self.pending()
        message_id = self.nav_id()
        published = self.navigator()["published_text"]
        self.restart()
        self.adapter.refresh_navigator(time.time())
        self.assertEqual(self.api.texts, [])
        self.create("Added")
        self.tick()
        self.assertEqual(self.api.texts[-1]["message_id"], message_id)
        self.assertIn("Pending: 2", self.screen())
        self.assertNotEqual(published, self.navigator()["published_text"])


if __name__ == "__main__":
    unittest.main()
