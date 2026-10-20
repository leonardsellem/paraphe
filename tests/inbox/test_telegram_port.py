"""Inbox-seam tests for the private Telegram adapter."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from collections.abc import Callable
from contextlib import closing, redirect_stdout
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from unittest import mock

BEARER = "test-mcp-bearer"
OWNER = 999001
STRANGER = 888002
TOKEN = "bot-token-must-not-leak"

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe import inbox as inbox_mod
from paraphe.adapters import telegram as tg_mod
from paraphe.inbox.runtime import TelegramAPIError, TelegramBotAPI

ClaimRefused = inbox_mod.ClaimRefused
Inbox = inbox_mod.Inbox
NotifyRejected = inbox_mod.NotifyRejected
TelegramAdapter = tg_mod.TelegramAdapter
decode_callback_data = tg_mod.decode_callback_data


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
        self.fail_edit = False

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
        if self.fail_edit:
            raise RuntimeError("edit failed")
        self.edits.append(
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
        )
        for msg in self.messages:
            if msg["message_id"] == message_id:
                msg["reply_markup"] = reply_markup

    def answer_callback_query(self, callback_id: str) -> None:
        self.answers.append(callback_id)


class RejectOnceAfterFirstSend(FakeBotAPI):
    def __init__(self) -> None:
        super().__init__()
        self.rejected = False

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None, parse_mode: str | None = None
    ) -> dict:
        if self.messages and not self.rejected:
            self.rejected = True
            raise NotifyRejected("rejected")
        return super().send_message(chat_id, text, reply_markup, parse_mode)


class AcceptSecondThenUnknown(FakeBotAPI):
    def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None, parse_mode: str | None = None
    ) -> dict:
        sent = super().send_message(chat_id, text, reply_markup, parse_mode)
        if len(self.messages) == 2:
            raise TimeoutError("lost after accept")
        return sent


class AcceptKeyboardThenUnknown(FakeBotAPI):
    def __init__(self) -> None:
        super().__init__()
        self.unknown = True

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> None:
        super().edit_message_reply_markup(chat_id, message_id, reply_markup)
        if self.unknown and reply_markup.get("inline_keyboard"):
            self.unknown = False
            raise TimeoutError("lost after keyboard accept")


class RaceDuringKeyboardAttach(FakeBotAPI):
    def __init__(self, during_attach: Callable[[], None]) -> None:
        super().__init__()
        self.during_attach = during_attach

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> None:
        if self.during_attach is not None and reply_markup.get("inline_keyboard"):
            during_attach = self.during_attach
            self.during_attach = None
            during_attach()
        super().edit_message_reply_markup(chat_id, message_id, reply_markup)


class TestTelegramPort(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.api: Any = FakeBotAPI()
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

    def _create(self) -> dict:
        return self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "tg-1", "expires_in_seconds": 900},
            bearer=BEARER,
        )

    def _restart(self, api: FakeBotAPI | None = None):
        self.inbox.close()
        restarted_api = api or FakeBotAPI()
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

    def _use_api(self, api: FakeBotAPI | TelegramBotAPI) -> None:
        self.api = api
        self.adapter = TelegramAdapter(self.inbox, api, owner_id=OWNER, bot_token=TOKEN)
        self.inbox._notifier = self.adapter
        self.inbox._telegram = self.adapter

    def _stored(self, request_id):
        with closing(sqlite3.connect(self.inbox.store_path)) as conn:
            return json.loads(conn.execute(
                "SELECT payload FROM cards WHERE request_id = ?", (request_id,)
            ).fetchone()[0])

    def test_not_modified_strip_persists_and_is_not_retried(self):
        descriptions = (
            "Bad Request: message is not modified",
            "bAd ReQuEsT: MeSsAgE Is NoT MoDiFiEd: specified reply markup is unchanged",
        )
        for index, mode in enumerate(("http", "json", "success", "legacy")):
            with self.subTest(mode=mode):
                self._use_api(FakeBotAPI())
                created = self.inbox.call_tool("request_approval", {
                    "title": "Cleanup", "external_id": f"cleanup-{mode}",
                }, bearer=BEARER)
                request_id = created["request_id"]
                self.inbox.record_tap(request_id, choice="Approve")
                if mode == "legacy":
                    payload = self._stored(request_id)
                    del payload["telegram_keyboard_attached"]
                    with closing(sqlite3.connect(self.inbox.store_path)) as conn:
                        conn.execute("UPDATE cards SET payload=? WHERE request_id=?",
                                     (json.dumps(payload), request_id))
                        conn.commit()
                    self.inbox.close()
                self._use_api(TelegramBotAPI(TOKEN))
                # The adapter cache is deliberately empty: the durable locator wins.
                body = json.dumps({"ok": False, "error_code": 400,
                                   "description": descriptions[index % 2]}).encode()
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = (
                    b'{"ok":true,"result":true}' if mode == "success" else body
                )
                error = urllib.error.HTTPError(
                    "https://example.invalid/edit", 400, "Bad Request", EmailMessage(), io.BytesIO(body)
                )
                output = io.StringIO()
                with mock.patch("urllib.request.urlopen", return_value=response,
                                side_effect=error if mode == "http" else None) as opened:
                    with redirect_stdout(output):
                        self.assertTrue(self.inbox.reconcile_notifications())
                error.close()
                self.assertEqual(opened.call_count, 1)
                self.assertNotIn("Telegram strip failed", output.getvalue())
                self.assertFalse(self._stored(request_id)["telegram_keyboard_attached"])
                self.assertEqual(self._stored(request_id)["response_choice"], "Approve")
                for _ in range(2):
                    restarted, _adapter, api = self._restart()
                    self.assertFalse(restarted._cards[request_id].telegram_keyboard_attached)
                    self.assertEqual(api.edits, [])
                    restarted.close()

    def test_only_classified_empty_keyboard_rejection_is_success(self):
        self._use_api(TelegramBotAPI(TOKEN))
        canonical = "Bad Request: message is not modified"
        cases = (
            ({"ok": False, "error_code": 400, "description": "Bad Request: message not found"}, NotifyRejected),
            ({"ok": False, "description": canonical}, NotifyRejected),
            ({"ok": False, "error_code": 401, "description": canonical}, NotifyRejected),
            ({"ok": False, "error_code": "400", "description": canonical}, NotifyRejected),
            ({"ok": False, "error_code": 400, "description": canonical + "ish"}, NotifyRejected),
            ({"ok": False, "error_code": 400, "description": "prefix " + canonical}, NotifyRejected),
            ({"ok": False, "error_code": 400, "description": canonical + "\n"}, NotifyRejected),
            ({"ok": False, "error_code": 400, "description": None}, NotifyRejected),
            (b"not-json", TelegramAPIError),
        )
        for body, error_type in cases:
            with self.subTest(body=body):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = (
                    body if isinstance(body, bytes) else json.dumps(body).encode()
                )
                with mock.patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(error_type):
                        self.adapter.strip_message(OWNER, 1)
        for error in (TimeoutError(canonical), RuntimeError(canonical), NotifyRejected(canonical)):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.api, "edit_message_reply_markup", side_effect=error):
                    with self.assertRaises(type(error)):
                        self.adapter.strip_message(OWNER, 1)
        body = json.dumps({"ok": False, "error_code": 400,
                           "description": canonical + ": private-provider-text"}).encode()
        card_id = "00000000-0000-0000-0000-000000000001"
        self.adapter.remember_message(card_id, 1, OWNER, 1)
        for call in (
            lambda: self.api.send_message(OWNER, "hi", None),
            lambda: self.adapter.finalize_message({"request_id": card_id, "version": 1}),
            lambda: self.api.answer_callback_query("callback"),
            lambda: self.api.edit_message_reply_markup(OWNER, 1, tg_mod.EMPTY_KEYBOARD),
        ):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body
            with mock.patch("urllib.request.urlopen", return_value=response):
                with self.assertRaises(NotifyRejected) as caught:
                    call()
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(caught.exception.reason, "message_not_modified")
            self.assertNotIn("private-provider-text", str(caught.exception))

    def test_cleanup_failure_keeps_flag_retryable(self):
        self._use_api(FakeBotAPI())
        created = self._create()
        request_id = created["request_id"]
        self.inbox.record_tap(request_id, choice="Approve")
        output = io.StringIO()
        for target, method in ((self.api, "edit_message_reply_markup"),
                               (self.inbox._store, "save_card")):
            with self.subTest(method=method):
                with mock.patch.object(target, method, side_effect=OSError("private-provider-text")):
                    with redirect_stdout(output):
                        self.assertFalse(self.inbox.reconcile_notifications())
                self.assertTrue(self._stored(request_id)["telegram_keyboard_attached"])
                self.assertTrue(self.inbox._cards[request_id].telegram_keyboard_attached)
        self.assertNotIn("private-provider-text", output.getvalue())
        restarted, _adapter, api = self._restart()
        self.assertEqual(len(api.edits), 1)
        self.assertFalse(restarted._cards[request_id].telegram_keyboard_attached)

    def test_strip_completion_preserves_current_answer_and_identity(self):
        from copy import deepcopy

        for change in ("answer", "version", "message", "chat", "open"):
            with self.subTest(change=change):
                self._use_api(FakeBotAPI())
                created = self.inbox.call_tool("request_approval", {
                    "title": "Cleanup race", "external_id": f"cleanup-race-{change}",
                }, bearer=BEARER)
                request_id = created["request_id"]
                self.inbox.record_tap(request_id, choice="Approve")
                pending, release = threading.Event(), threading.Event()
                result = []

                def strip(*args):
                    pending.set()
                    self.assertTrue(release.wait(2))

                with mock.patch.object(self.api, "edit_message_reply_markup", side_effect=strip):
                    worker = threading.Thread(target=lambda: result.append(self.inbox.reconcile_notifications()))
                    worker.start()
                    try:
                        self.assertTrue(pending.wait(1))
                        with self.inbox._lock:
                            current = deepcopy(self.inbox._cards[request_id])
                            current.processed_at = "processed-now"
                            current.execution_status = "completed"
                            current.response_text = "keep these exact words"
                            if change == "version":
                                current.version += 1
                            elif change == "message":
                                assert current.telegram_message_id is not None
                                current.telegram_message_id += 1
                            elif change == "chat":
                                assert current.telegram_chat_id is not None
                                current.telegram_chat_id += 1
                            elif change == "open":
                                current.state = "open"
                            self.inbox._save(current)
                    finally:
                        release.set()
                        worker.join(2)
                self.assertFalse(worker.is_alive())
                stored = self._stored(request_id)
                self.assertEqual(stored["telegram_keyboard_attached"], change != "answer")
                self.assertEqual(stored["response_choice"], "Approve")
                self.assertEqual(stored["response_text"], "keep these exact words")
                self.assertEqual(stored["processed_at"], "processed-now")
                self.assertEqual(stored["execution_status"], "completed")
                self.assertEqual(result, [True])
                # Keep the next subcase's sweep focused on its own identity.
                with self.inbox._lock:
                    current.telegram_keyboard_attached = False
                    current.state = "tapped"
                    self.inbox._save(current)

    def test_late_attach_failed_compensation_retries_only_matching_terminal_identity(self):
        for newer in (False, True):
            with self.subTest(newer=newer):
                api = AcceptKeyboardThenUnknown()
                self._use_api(api)
                with self.assertRaises(TimeoutError):
                    self.inbox.call_tool("request_approval", {
                        "title": "Attach race", "external_id": f"attach-race-{newer}",
                    }, bearer=BEARER)
                created = self.inbox.call_tool("get_response", {"external_id": f"attach-race-{newer}"})
                request_id = created["request_id"]
                self.assertFalse(self._stored(request_id)["telegram_keyboard_attached"])
                pending, release = threading.Event(), threading.Event()
                original = api.edit_message_reply_markup
                errors = []

                def edit(chat_id, message_id, reply_markup):
                    if message_id == 1 and reply_markup.get("inline_keyboard"):
                        pending.set()
                        self.assertTrue(release.wait(3))
                    elif message_id == 1 and release.is_set():
                        raise OSError("compensation failed")
                    original(chat_id, message_id, reply_markup)

                def reconcile():
                    try:
                        self.inbox.reconcile_notifications()
                    except inbox_mod.InboxError as exc:
                        errors.append(exc)

                with mock.patch.object(api, "edit_message_reply_markup", side_effect=edit):
                    worker = threading.Thread(target=reconcile)
                    worker.start()
                    try:
                        self.assertTrue(pending.wait(1))
                        if newer:
                            created = self.inbox.call_tool("update_request", {
                                "request_id": request_id, "expected_version": 1, "renotify": True,
                            })
                        message = api.messages[-1]
                        self._callback(message["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                                       "attach-answer", chat={"id": OWNER, "type": "private"},
                                       message_id=message["message_id"])
                        self.assertFalse(self._stored(request_id)["telegram_keyboard_attached"])
                    finally:
                        release.set()
                        worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                stored = self._stored(request_id)
                self.assertEqual(stored["state"], "tapped")
                self.assertEqual(stored["response_choice"], "Approve")
                self.assertEqual(stored["responded_via"], "telegram")
                self.assertEqual(stored["version"], 2 if newer else 1)
                self.assertEqual(stored["telegram_message_id"], 2 if newer else 1)
                self.assertEqual(stored["telegram_keyboard_attached"], not newer)
                restarted, _adapter, retry_api = self._restart()
                self.assertEqual(len(retry_api.edits), 0 if newer else 1)
                self.assertFalse(restarted._cards[request_id].telegram_keyboard_attached)
                self.assertEqual(restarted._cards[request_id].response_choice, "Approve")
                restarted.close()

    def _overlap_reconcile(self, action, *, reject=False):
        with mock.patch.object(self.inbox, "_notify_owner"):
            created = self._create()
        self._use_api(FakeBotAPI())
        pending, release, changed = threading.Event(), threading.Event(), threading.Event()
        original = self.api.send_message
        errors = []

        def send(*args, **kwargs):
            if not pending.is_set():
                pending.set()
                release.wait(2)
                if reject:
                    raise NotifyRejected("known rejection")
            return original(*args, **kwargs)

        def reconcile():
            try:
                self.inbox.reconcile_notifications()
            except Exception as exc:
                errors.append(exc)

        def change():
            try:
                action(created)
            finally:
                changed.set()

        self.api.send_message = send
        worker = threading.Thread(target=reconcile)
        changer = threading.Thread(target=change)
        worker.start()
        try:
            self.assertTrue(pending.wait(1))
            changer.start()
            self.assertTrue(changed.wait(1), "reconcile held the inbox lock during network I/O")
        finally:
            release.set()
            worker.join(2)
            if changer.ident is not None:
                changer.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(changer.is_alive())
        return created, errors

    def test_reconcile_send_overlap_update_does_not_restore_old_identity(self):
        def update(card):
            self.inbox.call_tool("update_request", {
                "request_id": card["request_id"], "expected_version": 1,
                "title": "Updated decision", "renotify": True,
            })
            self.inbox.call_tool("request_approval", {
                "title": "Created during reconcile", "external_id": "reconcile-new",
            }, bearer=BEARER)

        created, errors = self._overlap_reconcile(update)
        self.assertEqual(len(self.api.messages), 3)
        current = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(current["version"], 2)
        self.assertEqual(self.inbox._cards[created["request_id"]].telegram_message_id, 1)
        self.assertIsNone(self.api.messages[-1]["reply_markup"])
        self.assertEqual(len(errors), 1)  # old version cannot bind its accepted message
        self.inbox.reconcile_notifications()
        self.assertEqual(len(self.api.messages), 3)

    def test_reconcile_send_overlap_cancel_leaves_no_keyboard(self):
        created, _errors = self._overlap_reconcile(lambda card: self.inbox.call_tool(
            "cancel_request", {"request_id": card["request_id"]}
        ))
        self.assertEqual(self.inbox.call_tool("get_response", {
            "request_id": created["request_id"],
        })["status"], "cancelled")
        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_reconcile_rejection_cannot_overwrite_concurrent_answer(self):
        created, errors = self._overlap_reconcile(lambda card: self.inbox.answer(
            card["request_id"], version=1, choice="Approve"
        ), reject=True)
        self.assertIsInstance(errors[0], NotifyRejected)
        self.inbox.close()
        restarted, _adapter, api = self._restart()
        response = restarted.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(response["response"]["choice"], "Approve")
        self.assertEqual(api.messages, [])

    def test_reconcile_keyboard_expiry_strip_does_not_hold_lock(self):
        with mock.patch.object(self.inbox, "_notify_owner"):
            created = self._create()
        self._use_api(FakeBotAPI())
        pending, release, read = threading.Event(), threading.Event(), threading.Event()
        original = self.api.edit_message_reply_markup
        errors = []

        def edit(chat_id, message_id, reply_markup):
            if reply_markup.get("inline_keyboard"):
                self.clock.now += 901
            else:
                pending.set()
                release.wait(2)
            original(chat_id, message_id, reply_markup)

        def reconcile():
            try:
                self.inbox.reconcile_notifications()
            except inbox_mod.InboxError as exc:
                errors.append(exc)

        self.api.edit_message_reply_markup = edit
        worker = threading.Thread(target=reconcile)
        reader = threading.Thread(target=lambda: (
            self.inbox.call_tool("get_response", {"request_id": created["request_id"]}), read.set()
        ))
        worker.start()
        try:
            self.assertTrue(pending.wait(1))
            reader.start()
            self.assertTrue(read.wait(1), "post-I/O expiry cleanup held the inbox lock")
        finally:
            release.set()
            worker.join(2)
            if reader.ident is not None:
                reader.join(2)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_reconcile_expiry_strip_does_not_hold_lock_and_logs_failure(self):
        self._use_api(FakeBotAPI())
        created = self._create()
        self.clock.now += 901
        pending, release, read = threading.Event(), threading.Event(), threading.Event()
        output = io.StringIO()

        def strip(chat_id, message_id, reply_markup):
            pending.set()
            release.wait(2)
            raise RuntimeError("https://example.invalid/bot-private-token/card-content")

        self.api.edit_message_reply_markup = strip
        with redirect_stdout(output):
            worker = threading.Thread(target=self.inbox.reconcile_notifications)
            reader = threading.Thread(target=lambda: (
                self.inbox.call_tool("get_response", {"request_id": created["request_id"]}), read.set()
            ))
            worker.start()
            try:
                self.assertTrue(pending.wait(1))
                reader.start()
                self.assertTrue(read.wait(1), "expiry cleanup held the inbox lock")
            finally:
                release.set()
                worker.join(2)
                if reader.ident is not None:
                    reader.join(2)
        self.assertRegex(output.getvalue(), r"strip failed.*RuntimeError")
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("card-content", output.getvalue())

    def test_send_builds_callback_data_with_card_id_version_and_choice_index(self) -> None:
        created = self._create()
        self.adapter.send(created, text="Cut over?")
        markup = self.api.messages[0]["reply_markup"]
        data = markup["inline_keyboard"][0][0]["callback_data"]
        card_id, version, choice_index = decode_callback_data(data)
        self.assertEqual(card_id, created["request_id"])
        self.assertEqual(version, created["version"])
        self.assertEqual(choice_index, 0)
        self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_long_utf8_choice_uses_compact_callback_and_resolves_label(self) -> None:
        choice = "🧵" * 40
        self.inbox._notifier = self.adapter
        created = self.inbox.call_tool(
            "ask_question",
            {
                "question": "Pick one",
                "choices": [choice, "No"],
                "external_id": "tg-long-choice",
                "expires_in_seconds": 900,
            },
            bearer=BEARER,
        )
        data = self.api.messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self._callback(data, "cb-long", chat={"id": OWNER, "type": "private"})
        tapped = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(tapped["response"]["choice"], choice)

    def test_renotify_sends_complete_updated_card_and_persists_message(self) -> None:
        self._use_api(FakeBotAPI())
        created = self._create()
        updated = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "title": "Cut over now?",
                "details": "The live state changed.",
                "recommendation": "Approve",
                "consequence": "The old buttons stay stale.",
                "renotify": True,
            },
        )

        self.assertEqual(len(self.api.messages), 2)
        renotified = self.api.messages[-1]
        self.assertEqual(
            renotified["text"],
            "Approval\n\n"
            "<b>Cut over now?</b>\n\n"
            "The live state changed.\n\n"
            "<b>Recommended:</b> Approve\n\n"
            "<b>If approved:</b> The old buttons stay stale.\n\n"
            "Reply to this message to answer in your own words or ask a question.\n\n"
            "Expires: 2023-11-14 22:28:20 UTC",
        )
        buttons = renotified["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([button["text"] for button in buttons], ["Approve", "Deny"])
        for choice_index, button in enumerate(buttons):
            card_id, version, decoded_index = decode_callback_data(button["callback_data"])
            self.assertEqual(card_id, created["request_id"])
            self.assertEqual(version, updated["version"])
            self.assertEqual(decoded_index, choice_index)
            self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)

        restarted, _adapter, api = self._restart(self.api)
        restarted.claim(
            created["request_id"],
            version=updated["version"],
            from_id=OWNER,
            choice="Approve",
        )
        self.assertEqual(api.edits[-1]["message_id"], 2)

    def test_stale_callback_after_renotify_strips_only_old_message(self) -> None:
        self._use_api(FakeBotAPI())
        created = self._create()
        old_data = self.api.messages[0]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]
        updated = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "renotify": True,
            },
        )

        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": "cb-stale",
                    "from": {"id": OWNER},
                    "data": old_data,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": OWNER, "type": "private"},
                    },
                }
            }
        )

        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})
        self.assertNotEqual(self.api.messages[1]["reply_markup"], {"inline_keyboard": []})
        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": "cb-stale-current-message",
                    "from": {"id": OWNER},
                    "data": old_data,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": OWNER, "type": "private"},
                    },
                }
            }
        )
        self.assertNotEqual(self.api.messages[1]["reply_markup"], {"inline_keyboard": []})
        self.inbox.claim(
            created["request_id"],
            version=updated["version"],
            from_id=OWNER,
            choice="Approve",
        )
        self.assertEqual(self.api.messages[1]["reply_markup"], {"inline_keyboard": []})

    def test_non_renotify_update_strips_retained_keyboard_before_stale_callback(
        self,
    ) -> None:
        self._use_api(FakeBotAPI())
        created = self._create()
        old_data = self.api.messages[0]["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]

        updated = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "title": "Cut over later?",
            },
        )

        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})
        card = self.inbox._cards[created["request_id"]]
        self.assertEqual((card.telegram_chat_id, card.telegram_message_id), (None, None))
        self.assertFalse(card.telegram_keyboard_attached)
        self._callback(
            old_data,
            "cb-stale-retained",
            chat={"id": OWNER, "type": "private"},
            message_id=1,
        )
        pending = self.inbox.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["version"], updated["version"])
        self.assertEqual(self.api.answers[-1], "cb-stale-retained")

    def test_current_callback_from_different_message_strips_only_that_message(self) -> None:
        self._use_api(FakeBotAPI())
        created = self._create()
        data = self.api.messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

        self._callback(
            data,
            "cb-different-message",
            chat={"id": OWNER, "type": "private"},
            message_id=999,
        )

        pending = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(
            self.api.edits[-1],
            {"chat_id": OWNER, "message_id": 999, "reply_markup": {"inline_keyboard": []}},
        )
        self.assertNotEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

        self._callback(
            data,
            "cb-current-message",
            chat={"id": OWNER, "type": "private"},
            message_id=1,
        )
        answered = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_rejected_renotify_retries_from_readback_version(self) -> None:
        self._use_api(RejectOnceAfterFirstSend())
        created = self._create()
        with self.assertRaises(NotifyRejected):
            self.inbox.call_tool(
                "update_request",
                {
                    "request_id": created["request_id"],
                    "expected_version": created["version"],
                    "title": "Persisted revision",
                    "renotify": True,
                },
            )
        readback = self.inbox.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        self.assertEqual(readback["version"], created["version"] + 1)
        self.assertEqual(len(self.api.messages), 1)
        retried = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": readback["version"],
                "renotify": True,
            },
        )

        self.assertEqual(retried["version"], readback["version"] + 1)
        renotified = self.api.messages[-1]
        self.assertIn("<b>Persisted revision</b>", renotified["text"])
        data = renotified["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(decode_callback_data(data)[1], retried["version"])

    def test_rejected_renotify_retries_once_after_restart(self) -> None:
        self._use_api(RejectOnceAfterFirstSend())
        created = self._create()
        with self.assertRaises(NotifyRejected):
            self.inbox.call_tool(
                "update_request",
                {
                    "request_id": created["request_id"],
                    "expected_version": created["version"],
                    "title": "Restart revision",
                    "renotify": True,
                },
            )

        restarted, _adapter, api = self._restart(self.api)
        readback = restarted.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        self.assertEqual(readback["version"], created["version"] + 1)
        self.assertEqual(len(api.messages), 2)
        self.assertIn("<b>Restart revision</b>", api.messages[-1]["text"])
        restarted.close()
        _again, _again_adapter, after_second_restart = self._restart()
        self.assertEqual(after_second_restart.messages, [])

    def test_ambiguous_renotify_does_not_resend_after_restart(self) -> None:
        self._use_api(AcceptSecondThenUnknown())
        created = self._create()
        with self.assertRaises(TimeoutError):
            self.inbox.call_tool(
                "update_request",
                {
                    "request_id": created["request_id"],
                    "expected_version": created["version"],
                    "title": "Ambiguous revision",
                    "renotify": True,
                },
            )
        self.assertEqual(len(self.api.messages), 2)

        restarted, _adapter, api = self._restart()
        self.assertEqual(api.messages, [])
        readback = restarted.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        self.assertEqual(readback["version"], created["version"] + 1)

    def test_non_owner_config_is_ignored_and_token_absent(self) -> None:
        replies = self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": STRANGER},
                    "text": "/config",
                    "chat": {"id": STRANGER},
                }
            }
        )
        self.assertEqual(replies, [])
        blob = str(self.api.messages) + str(replies)
        self.assertNotIn(TOKEN, blob)

    def test_config_requires_private_owner_chat(self) -> None:
        for message in (
            {"from": {"id": OWNER}, "text": "/config", "chat": {"id": OWNER}},
            {
                "from": {"id": OWNER},
                "text": "/config",
                "chat": {"id": OWNER, "type": "group"},
            },
            {
                "from": {"id": OWNER},
                "text": "/config",
                "chat": {"id": STRANGER, "type": "private"},
            },
        ):
            self.assertEqual(self.adapter.handle_update({"message": message}), [])
        replies = self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "text": "/config",
                    "chat": {"id": OWNER, "type": "private"},
                }
            }
        )
        self.assertEqual(replies, ["ttl and owner knobs only"])
        self.assertNotIn(TOKEN, str(replies))

    def test_cancel_and_expiry_strip_their_keyboards(self) -> None:
        self.inbox._notifier = self.adapter
        cancelled = self.inbox.call_tool(
            "request_approval",
            {"title": "Cancel", "external_id": "tg-cancel", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.inbox.call_tool("cancel_request", {"request_id": cancelled["request_id"]})
        expiring = self.inbox.call_tool(
            "request_approval",
            {"title": "Expire", "external_id": "tg-expire", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.clock.now += 900
        self.inbox.call_tool("list_pending", {})
        self.assertEqual(
            [
                edit["message_id"]
                for edit in self.api.edits
                if edit["reply_markup"] == {"inline_keyboard": []}
            ],
            [1, 2],
        )
        expired = self.inbox.call_tool(
            "get_response", {"request_id": expiring["request_id"]}
        )
        self.assertEqual(expired["status"], "expired")

    def test_cancellation_during_keyboard_attachment_strips_buttons(self) -> None:
        raced: dict[str, str] = {}

        def cancel() -> None:
            pending = self.inbox.call_tool("list_pending", {})[0]
            raced["request_id"] = pending["request_id"]
            self.inbox.call_tool("cancel_request", {"request_id": pending["request_id"]})

        api = RaceDuringKeyboardAttach(cancel)
        self._use_api(api)

        with self.assertRaisesRegex(inbox_mod.InboxError, "card is not open"):
            self.inbox.call_tool(
                "request_approval",
                {
                    "title": "Cancel during attach",
                    "external_id": "tg-attach-cancel",
                    "expires_in_seconds": 900,
                },
                bearer=BEARER,
            )

        closed = self.inbox.call_tool(
            "get_response", {"request_id": raced["request_id"]}
        )
        self.assertEqual(closed["status"], "cancelled")
        self.assertEqual(api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_expiry_during_keyboard_attachment_strips_buttons(self) -> None:
        raced: dict[str, str] = {}

        def expire() -> None:
            pending = self.inbox.call_tool("list_pending", {})[0]
            raced["request_id"] = pending["request_id"]
            self.clock.now += 900

        api = RaceDuringKeyboardAttach(expire)
        self._use_api(api)

        with self.assertRaisesRegex(inbox_mod.InboxError, "card is not open"):
            self.inbox.call_tool(
                "request_approval",
                {
                    "title": "Expire during attach",
                    "external_id": "tg-attach-expire",
                    "expires_in_seconds": 900,
                },
                bearer=BEARER,
            )

        closed = self.inbox.call_tool(
            "get_response", {"request_id": raced["request_id"]}
        )
        self.assertEqual(closed["status"], "expired")
        self.assertEqual(api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_cancel_after_update_without_renotify_strips_retained_keyboard(self) -> None:
        self.inbox._notifier = self.adapter
        created = self.inbox.call_tool(
            "request_approval",
            {"title": "Cancel", "external_id": "tg-update-cancel", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "title": "Cancel now",
            },
        )

        self.inbox.call_tool("cancel_request", {"request_id": created["request_id"]})

        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_expiry_after_update_without_renotify_strips_retained_keyboard(self) -> None:
        self.inbox._notifier = self.adapter
        created = self.inbox.call_tool(
            "request_approval",
            {"title": "Expire", "external_id": "tg-update-expire", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "title": "Expire now",
            },
        )

        self.clock.now += 900
        self.inbox.call_tool("list_pending", {})

        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_cancel_after_renotify_strips_old_and_new_keyboards(self) -> None:
        self._use_api(FakeBotAPI())
        created = self.inbox.call_tool(
            "request_approval",
            {"title": "Cancel", "external_id": "tg-renotify-cancel", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "renotify": True,
            },
        )

        self.inbox.call_tool("cancel_request", {"request_id": created["request_id"]})

        self.assertEqual(
            [message["reply_markup"] for message in self.api.messages],
            [{"inline_keyboard": []}, {"inline_keyboard": []}],
        )

    def test_expiry_after_renotify_strips_old_and_new_keyboards(self) -> None:
        self._use_api(FakeBotAPI())
        created = self.inbox.call_tool(
            "request_approval",
            {"title": "Expire", "external_id": "tg-renotify-expire", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "renotify": True,
            },
        )

        self.clock.now += 900
        self.inbox.call_tool("list_pending", {})

        self.assertEqual(
            [message["reply_markup"] for message in self.api.messages],
            [{"inline_keyboard": []}, {"inline_keyboard": []}],
        )

    def test_failed_revision_save_keeps_prior_keyboard_and_identity(self) -> None:
        self._use_api(FakeBotAPI())
        created = self._create()
        request_id = created["request_id"]
        edits_before_update = list(self.api.edits)
        store = self.inbox._store
        assert store is not None
        original_save = store.save_card

        def fail_revision(payload: dict) -> None:
            raise OSError("disk full")

        store.save_card = fail_revision
        try:
            with self.assertRaisesRegex(OSError, "disk full"):
                self.inbox.call_tool(
                    "update_request",
                    {
                        "request_id": request_id,
                        "expected_version": created["version"],
                        "renotify": True,
                    },
                )
        finally:
            store.save_card = original_save

        self.assertEqual(self.api.edits, edits_before_update)
        self.assertNotEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})
        card = self.inbox._cards[request_id]
        self.assertEqual(card.version, created["version"])
        self.assertEqual((card.telegram_chat_id, card.telegram_message_id), (OWNER, 1))
        self.assertTrue(card.telegram_keyboard_attached)

        restarted, _adapter, _api = self._restart(self.api)
        card = restarted._cards[request_id]
        self.assertEqual(card.version, created["version"])
        self.assertEqual((card.telegram_chat_id, card.telegram_message_id), (OWNER, 1))
        self.assertTrue(card.telegram_keyboard_attached)

    def test_failed_revision_strip_keeps_prior_version_and_identity(self) -> None:
        self._use_api(FakeBotAPI())
        created = self._create()
        request_id = created["request_id"]
        self.api.fail_edit = True

        with self.assertRaisesRegex(RuntimeError, "edit failed"):
            self.inbox.call_tool(
                "update_request",
                {
                    "request_id": request_id,
                    "expected_version": created["version"],
                    "renotify": True,
                },
            )

        card = self.inbox._cards[request_id]
        self.assertEqual(card.version, created["version"])
        self.assertEqual((card.telegram_chat_id, card.telegram_message_id), (OWNER, 1))
        self.assertTrue(card.telegram_keyboard_attached)
        self.api.fail_edit = False

        restarted, _adapter, _api = self._restart(self.api)
        card = restarted._cards[request_id]
        self.assertEqual(card.version, created["version"])
        self.assertEqual((card.telegram_chat_id, card.telegram_message_id), (OWNER, 1))
        self.assertTrue(card.telegram_keyboard_attached)
        restarted.call_tool("cancel_request", {"request_id": request_id})
        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})

    def test_close_asks_strip_and_late_claim_refuses_after_edit_failure(self) -> None:
        created = self._create()
        self.adapter.send(created, text="Cut over?")
        data = self.api.messages[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        card_id, version, _choice = decode_callback_data(data)
        envelope = self.inbox.claim(card_id, version=version, from_id=OWNER, choice="Approve")
        self.assertEqual(envelope["response"]["choice"], "Approve")
        self.assertEqual(self.api.messages[0]["reply_markup"], {"inline_keyboard": []})
        with self.assertRaises(ClaimRefused):
            self.inbox.claim(card_id, version=version, from_id=OWNER, choice="Deny")

        other = self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "tg-2", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.adapter.send(other, text="Cut over?")
        self.api.fail_edit = True
        data = self.api.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        card_id, version, _choice = decode_callback_data(data)
        envelope = self.inbox.claim(card_id, version=version, from_id=OWNER, choice="Approve")
        self.assertEqual(envelope["response"]["choice"], "Approve")
        with self.assertRaises(ClaimRefused):
            self.inbox.claim(card_id, version=version, from_id=OWNER, choice="Deny")

    def test_callback_is_answered_on_success_and_refuse(self) -> None:
        created = self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "tg-ack", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.adapter.send(created, text="Cut over?")
        data = self.api.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": "cb-1",
                    "from": {"id": OWNER},
                    "data": data,
                    "message": {"chat": {"id": OWNER, "type": "private"}},
                }
            }
        )
        self.assertEqual(self.api.answers, ["cb-1"])
        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": "cb-2",
                    "from": {"id": OWNER},
                    "data": data,
                    "message": {"chat": {"id": OWNER, "type": "private"}},
                }
            }
        )
        self.assertEqual(self.api.answers, ["cb-1", "cb-2"])

    def test_stranger_text_does_not_create(self) -> None:
        before = self.inbox.call_tool("list_pending", {})
        self.adapter.handle_update(
            {
                "message": {
                    "from": {"id": STRANGER},
                    "text": "please approve",
                    "chat": {"id": STRANGER},
                }
            }
        )
        after = self.inbox.call_tool("list_pending", {})
        self.assertEqual(len(before), len(after))

    def _callback(
        self,
        data: str,
        callback_id: str,
        *,
        chat: dict,
        message_id: int | None = None,
    ) -> None:
        message: dict[str, object] = {"chat": chat}
        if message_id is not None:
            message["message_id"] = message_id
        self.adapter.handle_update(
            {
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": OWNER},
                    "data": data,
                    "message": message,
                }
            }
        )

    def test_callback_requires_private_owner_chat(self) -> None:
        created = self._create()
        self.adapter.send(created, text="Cut over?")
        data = self.api.messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        rid = created["request_id"]
        self._callback(data, "cb-group", chat={"id": OWNER, "type": "group"})
        self._callback(data, "cb-missing-type", chat={"id": OWNER})
        self._callback(data, "cb-wrong-chat", chat={"id": STRANGER, "type": "private"})
        pending = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertIsNone(pending["response"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(self.api.answers, ["cb-group", "cb-missing-type", "cb-wrong-chat"])
        self._callback(data, "cb-ok", chat={"id": OWNER, "type": "private"})
        tapped = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(tapped["response"]["choice"], "Approve")
        self.assertEqual(self.api.answers[-1], "cb-ok")

    def test_restart_restores_message_identity_for_keyboard_strip(self) -> None:
        self.inbox._notifier = self.adapter
        created = self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "tg-restore", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        restarted, _adapter, api = self._restart()
        self.assertEqual(api.messages, [])
        restarted.claim(
            created["request_id"],
            version=created["version"],
            from_id=OWNER,
            choice="Approve",
        )
        self.assertEqual(
            api.edits,
            [{"chat_id": OWNER, "message_id": 1, "reply_markup": {"inline_keyboard": []}}],
        )

    def test_restart_sends_known_unsent_card(self) -> None:
        class RejectSend(FakeBotAPI):
            def send_message(
                self, chat_id: int, text: str, reply_markup: dict | None, parse_mode: str | None = None
            ) -> dict:
                raise NotifyRejected("rejected")

        rejected = RejectSend()
        adapter = TelegramAdapter(self.inbox, rejected, owner_id=OWNER, bot_token=TOKEN)
        self.inbox._notifier = adapter
        with self.assertRaises(NotifyRejected):
            self.inbox.call_tool(
                "request_approval",
                {"title": "Cut over?", "external_id": "tg-unsent", "expires_in_seconds": 900},
                bearer=BEARER,
            )
        restarted, _adapter, api = self._restart()
        pending = restarted.call_tool("list_pending", {})
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(api.messages), 1)

    def test_accepted_then_unknown_does_not_resend_after_restart(self) -> None:
        class AcceptedThenUnknown(FakeBotAPI):
            def send_message(
                self, chat_id: int, text: str, reply_markup: dict | None, parse_mode: str | None = None
            ) -> dict:
                super().send_message(chat_id, text, reply_markup, parse_mode)
                raise TimeoutError("lost after accept")

        ambiguous = AcceptedThenUnknown()
        adapter = TelegramAdapter(self.inbox, ambiguous, owner_id=OWNER, bot_token=TOKEN)
        self.inbox._notifier = adapter
        with self.assertRaises(TimeoutError):
            self.inbox.call_tool(
                "request_approval",
                {"title": "Cut over?", "external_id": "tg-unknown", "expires_in_seconds": 900},
                bearer=BEARER,
            )
        self.assertEqual(len(ambiguous.messages), 1)
        restarted, _adapter, api = self._restart()
        retry = restarted.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "tg-unknown", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.assertTrue(retry["duplicate"])
        self.assertEqual(api.messages, [])

    def test_accepted_keyboard_then_unknown_finalizes_on_restart_without_resend(
        self,
    ) -> None:
        ambiguous = AcceptKeyboardThenUnknown()
        self._use_api(ambiguous)
        with self.assertRaises(TimeoutError):
            self.inbox.call_tool(
                "request_approval",
                {
                    "title": "Cut over?",
                    "external_id": "tg-keyboard-unknown",
                    "expires_in_seconds": 900,
                },
                bearer=BEARER,
            )
        self.assertEqual(len(ambiguous.messages), 1)
        self.assertEqual(len(ambiguous.edits), 1)

        restarted, _adapter, api = self._restart(ambiguous)
        self.assertEqual(len(api.messages), 1)
        self.assertEqual(len(api.edits), 2)
        restarted.close()
        _again, _again_adapter, after_second_restart = self._restart()
        self.assertEqual(after_second_restart.messages, [])
        self.assertEqual(after_second_restart.edits, [])

    def test_message_identity_save_failure_does_not_resend_after_restart(self) -> None:
        self.inbox._notifier = self.adapter
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None
        original = store.save_card

        def fail_identity(payload: dict) -> None:
            if payload.get("telegram_message_id") is not None:
                raise OSError("disk full")
            original(payload)

        store.save_card = fail_identity
        try:
            with self.assertRaises(OSError):
                self.inbox.call_tool(
                    "request_approval",
                    {
                        "title": "Cut over?",
                        "external_id": "tg-identity-save",
                        "expires_in_seconds": 900,
                    },
                    bearer=BEARER,
                )
        finally:
            store.save_card = original
        self.assertEqual(len(self.api.messages), 1)
        self.assertIsNone(self.api.messages[0]["reply_markup"])
        self.assertEqual(self.api.edits, [])
        restarted, _adapter, api = self._restart()
        retry = restarted.call_tool(
            "request_approval",
            {
                "title": "Cut over?",
                "external_id": "tg-identity-save",
                "expires_in_seconds": 900,
            },
            bearer=BEARER,
        )
        self.assertTrue(retry["duplicate"])
        self.assertEqual(api.messages, [])

    def test_keyboard_state_persist_failure_finalizes_on_restart_without_resend(
        self,
    ) -> None:
        self._use_api(FakeBotAPI())
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None
        original = store.save_card

        def fail_attached(payload: dict) -> None:
            if payload.get("telegram_keyboard_attached") is True:
                raise OSError("disk full")
            original(payload)

        store.save_card = fail_attached
        try:
            with self.assertRaises(OSError):
                self.inbox.call_tool(
                    "request_approval",
                    {
                        "title": "Cut over?",
                        "external_id": "tg-keyboard-save",
                        "expires_in_seconds": 900,
                    },
                    bearer=BEARER,
                )
        finally:
            store.save_card = original
        self.assertEqual(len(self.api.messages), 1)
        edits_before_restart = len(self.api.edits)

        restarted, _adapter, api = self._restart(self.api)
        self.assertEqual(len(api.messages), 1)
        self.assertEqual(len(api.edits), edits_before_restart + 1)
        self.assertNotEqual(api.messages[0]["reply_markup"], {"inline_keyboard": []})
        restarted.close()
        _again, _again_adapter, after_second_restart = self._restart()
        self.assertEqual(after_second_restart.messages, [])
        self.assertEqual(after_second_restart.edits, [])

    def test_accepted_send_without_message_id_does_not_duplicate(self) -> None:
        class AcceptWithoutId(FakeBotAPI):
            def send_message(
                self, chat_id: int, text: str, reply_markup: dict | None, parse_mode: str | None = None
            ) -> dict:
                super().send_message(chat_id, text, reply_markup, parse_mode)
                return {"ok": True}

        self.api = AcceptWithoutId()
        self.adapter = TelegramAdapter(self.inbox, self.api, owner_id=OWNER, bot_token=TOKEN)
        self.inbox._notifier = self.adapter
        self.inbox._telegram = self.adapter
        first = self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "tg-accept", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        retry = self.inbox.call_tool(
            "request_approval",
            {"title": "Cut over?", "external_id": "tg-accept", "expires_in_seconds": 900},
            bearer=BEARER,
        )
        self.assertTrue(retry.get("duplicate"))
        self.assertEqual(first["request_id"], retry["request_id"])
        self.assertEqual(len(self.api.messages), 1)


if __name__ == "__main__":
    unittest.main()
