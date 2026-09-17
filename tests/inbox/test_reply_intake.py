"""Reply intake tests: a long-press reply is the owner's answer to the card."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

BEARER = "test-mcp-bearer"
OWNER = 999001
STRANGER = 888002
TOKEN = "bot-token-must-not-leak"

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe import inbox as inbox_mod
from paraphe.adapters import telegram as tg_mod

ClaimRefused = inbox_mod.ClaimRefused
Inbox = inbox_mod.Inbox
TelegramAdapter = tg_mod.TelegramAdapter


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeBotAPI:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[str] = []

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None, parse_mode: str | None = None
    ) -> dict:
        msg = {
            "message_id": len(self.messages) + 1,
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        }
        self.messages.append(msg)
        return msg

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> None:
        self.edits.append(
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
        )
        for msg in self.messages:
            if msg["message_id"] == message_id:
                msg["reply_markup"] = reply_markup

    def answer_callback_query(self, callback_id: str) -> None:
        self.answers.append(callback_id)


class TestReplyIntake(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.api = FakeBotAPI()
        self.clock = FakeClock()
        self.inbox = Inbox(
            owner_telegram_id=OWNER,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=Path(self._tmpdir.name) / "inbox.sqlite",
            mcp_create_bearer=BEARER,
            bot_token=TOKEN,
            clock=self.clock,
        )
        self.addCleanup(self.inbox.close)
        self.adapter = TelegramAdapter(self.inbox, self.api, owner_id=OWNER, bot_token=TOKEN)
        self.inbox._telegram = self.adapter

    def _create(self, external_id: str = "reply-1") -> dict:
        return self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": external_id, "expires_in_seconds": 900},
            bearer=BEARER,
        )

    def _reply(
        self,
        text: str,
        *,
        message_id: int = 1,
        from_id: int = OWNER,
        chat: dict | None = None,
    ) -> list[str]:
        return self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": from_id},
                    "text": text,
                    "chat": chat if chat is not None else {"id": OWNER, "type": "private"},
                    "reply_to_message": {"message_id": message_id},
                }
            }
        )

    def _restart(self):
        self.inbox.close()
        restarted_api = FakeBotAPI()
        restarted = Inbox(
            owner_telegram_id=OWNER,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=Path(self._tmpdir.name) / "inbox.sqlite",
            mcp_create_bearer=BEARER,
            bot_token=TOKEN,
            clock=FakeClock(),
        )
        adapter = TelegramAdapter(
            restarted, restarted_api, owner_id=OWNER, bot_token=TOKEN
        )
        restarted._notifier = adapter
        restarted._telegram = adapter
        restarted.reconcile_notifications()
        self.addCleanup(restarted.close)
        return restarted, adapter, restarted_api

    def test_reply_records_the_owners_words_verbatim(self) -> None:
        created = self._create()
        self.adapter.send(created, text="<b>Cut over?</b>")
        words = "  🧵 push only the docs folder, not wip.txt  "
        self._reply(words)

        got = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(got["status"], "answered")
        self.assertIsNone(got["response"]["choice"])
        self.assertEqual(got["response"]["text"], words)
        self.assertEqual(got["response"]["responded_via"], "telegram-reply")
        self.assertEqual(got["response"]["responded_at"], "2023-11-14T22:13:20Z")
        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_reply_writes_the_same_lifecycle_as_a_same_moment_tap(self) -> None:
        tapped = self._create("reply-tap")
        self.adapter.send(tapped, text="<b>Cut over?</b>")
        self.inbox.claim(
            tapped["request_id"],
            version=tapped["version"],
            from_id=OWNER,
            choice="Approve",
            telegram_chat_id=OWNER,
            telegram_message_id=1,
        )
        replied = self._create("reply-words")
        self.adapter.send(replied, text="<b>Cut over?</b>")
        words = "push only the docs folder"
        self._reply(words, message_id=2)

        tap_card = self.inbox._cards[tapped["request_id"]]
        reply_card = self.inbox._cards[replied["request_id"]]
        self.assertEqual(tap_card.state, reply_card.state)
        self.assertEqual(tap_card.state, "tapped")
        self.assertEqual(tap_card.responded_at, reply_card.responded_at)
        self.assertIsNone(reply_card.response_choice)
        self.assertEqual(tap_card.response_choice, "Approve")
        self.assertIsNone(tap_card.response_text)
        self.assertEqual(reply_card.response_text, words)
        self.assertEqual(tap_card.responded_via, "telegram")
        self.assertEqual(reply_card.responded_via, "telegram-reply")
        self.assertEqual(
            (reply_card.telegram_chat_id, reply_card.telegram_message_id),
            (OWNER, 2),
        )
        self.assertTrue(reply_card.telegram_keyboard_attached)
        self.assertEqual(
            [edit["message_id"] for edit in self.api.edits if edit["reply_markup"] == {"inline_keyboard": []}],
            [1, 2],
        )

    def test_reply_wakes_the_parked_waiter_with_the_text(self) -> None:
        created = self._create()
        self.adapter.send(created, text="<b>Cut over?</b>")
        results: list[dict] = []

        def park() -> None:
            results.append(
                self.inbox.call_tool(
                    "get_response",
                    {"request_id": created["request_id"], "wait_seconds": 30},
                )
            )

        worker = threading.Thread(target=park, daemon=True)
        worker.start()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self.inbox._waiters.get(created["request_id"]):
                break
            time.sleep(0.01)
        self.assertEqual(len(self.inbox._waiters.get(created["request_id"], [])), 1)

        words = "roll back first, then deploy"
        self._reply(words)
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "answered")
        self.assertEqual(results[0]["response"]["text"], words)
        self.assertEqual(results[0]["response"]["responded_via"], "telegram-reply")

    def test_reply_resolves_after_a_restart(self) -> None:
        created = self._create()
        self.adapter.send(created, text="<b>Cut over?</b>")

        restarted, adapter, _api = self._restart()
        words = "after the restart it still answers"
        adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "text": words,
                    "chat": {"id": OWNER, "type": "private"},
                    "reply_to_message": {"message_id": 1},
                }
            }
        )
        got = restarted.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(got["status"], "answered")
        self.assertEqual(got["response"]["text"], words)
        self.assertEqual(got["response"]["responded_via"], "telegram-reply")

    def test_refusals_record_nothing_raise_nothing_and_wake_nothing(self) -> None:
        created = self._create()
        self.adapter.send(created, text="<b>Cut over?</b>")
        words = "the only words that count"

        # Non-owner, non-private, wrong chat, unknown target, no reply target.
        self._reply("stranger words", from_id=STRANGER)
        self._reply("group words", chat={"id": OWNER, "type": "group"})
        self._reply("wrong chat words", chat={"id": STRANGER, "type": "private"})
        self._reply("unknown target", message_id=999)
        self.adapter.handle_update(
            {"message": {"from": {"id": OWNER}, "text": "hello", "chat": {"id": OWNER, "type": "private"}}}
        )
        pending = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["response"])

        # A live reply records; a second reply on the closed card records nothing.
        self._reply(words)
        self._reply("second thoughts")
        got = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(got["response"]["text"], words)

        # The owner-side seam refuses a non-owner directly, without a raise out.
        with self.assertRaises(ClaimRefused) as raised:
            self.inbox.claim_reply(
                from_id=STRANGER, chat_id=OWNER, message_id=1, text="agent words"
            )
        self.assertEqual(raised.exception.reason, "not_owner")

    def test_reply_to_a_status_message_is_not_an_answer(self) -> None:
        self.inbox._notifier = self.adapter
        self.inbox.call_tool(
            "notify_user", {"title": "Sync.", "message": "Done."}, bearer=BEARER
        )
        status_message_id = self.api.messages[-1]["message_id"]
        self._reply("replying to a status message", message_id=status_message_id)
        self.assertEqual(self.inbox.call_tool("list_unprocessed", {}), [])
        self.assertEqual(self.inbox.call_tool("list_pending", {}), [])
        self.assertEqual(self.inbox.call_tool("list_unprocessed", {}), [])

    def test_reply_to_a_superseded_message_resolves_to_nothing(self) -> None:
        created = self._create()
        self.adapter.send(created, text="<b>Cut over?</b>")
        # A renotify moves the card to a new message; the old one is stale.
        self.inbox._notifier = self.adapter
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "title": "Cut over now?",
                "renotify": True,
            },
        )
        self._reply("reply to the stale message", message_id=1)
        pending = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["response"])

        # The live message still answers.
        self._reply("reply to the live message", message_id=2)
        got = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(got["response"]["text"], "reply to the live message")


if __name__ == "__main__":
    unittest.main()
