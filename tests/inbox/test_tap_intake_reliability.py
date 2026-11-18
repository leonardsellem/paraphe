"""Tap intake: an answer must not wait on a slow effect, and one bad ACK must not poison the batch."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe import inbox as inbox_mod
from paraphe.adapters import telegram as tg
from paraphe.inbox import Inbox, NotifyRejected
from paraphe.inbox.runtime import Runtime, TelegramAPIError, TelegramBotAPI

EMPTY = {"inline_keyboard": []}
BEARER = "test-mcp-bearer"
OWNER = 999001
STRANGER = 888002
TOKEN = "bot-token-must-not-leak"


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeBotAPI:
    """The minimum Telegram surface the adapter calls, without the network."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.answers: list[str] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None,
        parse_mode: str | None = None,
        timing: list[float] | None = None,
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


class _NullServer:
    host = "127.0.0.1"
    port = 0

    def close(self) -> None:
        return None


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class SessionAPI(FakeBotAPI):
    """The Telegram fake plus the failure and timing seams these tests inject."""

    def __init__(self) -> None:
        super().__init__()
        self.ack_times: list[tuple[float, str]] = []
        self.ack_errors: dict[str, BaseException] = {}
        self.ack_gate: tuple[threading.Event, threading.Event] | None = None
        self.send_gate: tuple[threading.Event, threading.Event] | None = None
        self.send_calls: list[dict] = []
        self.strip_threads: dict[int, list[str]] = {}
        self.strip_attempts: dict[int, list[float]] = {}
        self.strip_entered: dict[int, threading.Event] = {}
        self.strip_gates: dict[int, tuple[threading.Event, threading.Event]] = {}
        self.strip_errors: dict[int, BaseException] = {}
        self.send_errors: list[BaseException] = []
        self.on_send = None
        self.on_strip = None
        self.on_keyboard = None
        self.on_edit = None
        self.texts: list[dict] = []
        self.ack_fields: list[dict] = []
        self.pins: list[tuple[int, int]] = []
        self.unpins: list[tuple[int, int]] = []
        self.commands: list[dict] = []

    def get_my_commands(self, scope) -> list[dict]:
        return [dict(item) for item in self.commands]

    def set_my_commands(self, commands, scope) -> None:
        return None

    def pin_chat_message(self, chat_id: int, message_id: int) -> None:
        self.pins.append((chat_id, message_id))

    def unpin_chat_message(self, chat_id: int, message_id: int) -> None:
        self.unpins.append((chat_id, message_id))

    def send_message(
        self, chat_id, text, reply_markup, parse_mode=None, timing=None
    ):
        self.send_calls.append({"chat_id": chat_id, "text": text})
        if self.on_send is not None:
            self.on_send()
        start = time.monotonic()
        gate = self.send_gate
        if gate is not None:
            self.send_gate = None
            gate[0].set()
            gate[1].wait(10)
        try:
            if self.send_errors:
                raise self.send_errors.pop(0)
            return super().send_message(chat_id, text, reply_markup, parse_mode)
        finally:
            if timing is not None:
                timing[:] = [start, time.monotonic()]

    def edit_message_text(self, chat_id, message_id, text, reply_markup, parse_mode="HTML"):
        self.texts.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        if self.on_edit is not None:
            self.on_edit()
        for message in self.messages:
            if message["message_id"] == message_id:
                message["text"] = text
                message["reply_markup"] = reply_markup

    def answer_callback_query(self, callback_id: str, **fields) -> None:
        self.ack_fields.append(dict(fields))
        gate = self.ack_gate
        if gate is not None:
            self.ack_gate = None
            gate[0].set()
            gate[1].wait(10)
        error = self.ack_errors.get(callback_id)
        if error is not None:
            self.ack_errors.pop(callback_id, None)
            raise error
        self.ack_times.append((time.monotonic(), callback_id))
        super().answer_callback_query(callback_id)

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup) -> None:
        if not (reply_markup or {}).get("inline_keyboard"):
            self.strip_threads.setdefault(message_id, []).append(threading.current_thread().name)
            self.strip_attempts.setdefault(message_id, []).append(time.monotonic())
            if self.on_strip is not None:
                self.on_strip()
            entered = self.strip_entered.get(message_id)
            if entered is not None:
                entered.set()
            gate = self.strip_gates.pop(message_id, None)
            if gate is not None:
                gate[1].wait(10)
            error = self.strip_errors.pop(message_id, None)
            if error is not None:
                raise error
        elif self.on_keyboard is not None:
            self.on_keyboard()
        super().edit_message_reply_markup(chat_id, message_id, reply_markup)


class Harness:
    def __init__(self, inbox, adapter, runtime, api, clock=None) -> None:
        self.inbox = inbox
        self.adapter = adapter
        self.runtime = runtime
        self.api = api
        self.clock = clock
        self.target = None

    def create(self, *, external_id: str, kind: str = "request_approval", **fields):
        args = {"external_id": external_id}
        if kind == "ask_question":
            args["question"] = fields.pop("question", "Ship it?")
        else:
            args["title"] = fields.pop("title", "Ship it?")
        args.update(fields)
        return self.inbox.call_tool(kind, args, bearer=BEARER)

    def stored(self, request_id: str) -> dict:
        with closing(sqlite3.connect(self.inbox.store_path)) as conn:
            row = conn.execute(
                "SELECT payload FROM cards WHERE request_id = ?", (request_id,)
            ).fetchone()
        return json.loads(row[0])

    def offset(self):
        with closing(sqlite3.connect(self.inbox.store_path)) as conn:
            row = conn.execute(
                "SELECT value FROM durable_state WHERE key = 'telegram_next_offset'"
            ).fetchone()
        return None if row is None else int(row[0])

    def tap_update(self, card: dict, update_id: int, callback_id: str) -> dict:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": callback_id,
                "from": {"id": OWNER},
                "data": tg.encode_callback_data(card["request_id"], card["version"], 0),
                "message": {
                    "message_id": self.stored(card["request_id"])["telegram_message_id"],
                    "chat": {"id": OWNER, "type": "private"},
                },
            },
        }


class IntakeCase(unittest.TestCase):
    # --- shared plumbing -----------------------------------------------------

    def reply(self, harness: Harness, message_id: int, text: str, *, chat_id: int = OWNER):
        return harness.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": chat_id, "type": "private"},
                    "reply_to_message": {"message_id": message_id},
                    "text": text,
                }
            }
        )

    def tap_message(
        self,
        harness: Harness,
        data: str,
        *,
        callback_id: str = "cb",
        message_id: int | None = None,
        chat_type: str = "private",
        from_id: int = OWNER,
        forwarded: bool = False,
    ):
        message = {"message_id": message_id or 0, "chat": {"id": OWNER, "type": chat_type}}
        if forwarded:
            message["forward_origin"] = {"type": "user"}
        return harness.adapter.handle_update(
            {
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": from_id},
                    "data": data,
                    "message": message,
                }
            }
        )

    def tap_card(self, harness: Harness, card: dict, *, callback_id: str = "cb", **fields):
        stored = harness.stored(card["request_id"])
        data = tg.encode_callback_data(card["request_id"], card["version"], 0)
        return self.tap_message(
            harness,
            data,
            callback_id=callback_id,
            message_id=stored["telegram_message_id"],
            **fields,
        )

    def answer_callback(self, harness: Harness, card: dict, update_id: int, callback_id: str) -> dict:
        return harness.tap_update(card, update_id, callback_id)

    def wait_until(self, predicate, timeout: float = 5.0, message: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    def start_runtime(self, harness: Harness, batches=None) -> threading.Thread:
        """Run the real loop; `batches(offset)` supplies each poll's updates."""
        feeder = batches if batches is not None else (lambda offset: [])
        harness.api.get_updates = lambda *, offset, timeout: feeder(offset)
        thread = threading.Thread(target=harness.runtime.run, daemon=True, name="poll-thread")
        thread.start()
        return thread

    def assert_effect_off_lock(self, harness: Harness, trigger, *, hook: str, opener) -> dict:
        entered, release = threading.Event(), threading.Event()
        observed: dict = {}

        def effect() -> None:
            observed["owned"] = harness.inbox._lock._is_owned()
            observed["thread"] = threading.current_thread().name
            entered.set()
            release.wait(5)

        setattr(harness.api, hook, effect)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                trigger()
            except BaseException as exc:  # surfaced by the caller's assertions
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True, name="trigger-thread")
        thread.start()
        try:
            self.assertTrue(entered.wait(3), f"no {hook} effect was reached")
            self.assertFalse(observed["owned"], "a remote effect ran while holding the inbox lock")
            started = time.monotonic()
            opener()
            self.assertLess(
                time.monotonic() - started,
                1.0,
                "a competing claim/read waited for the blocked remote effect",
            )
        finally:
            release.set()
            thread.join(5)
            setattr(harness.api, hook, None)
        observed["errors"] = errors
        return observed

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.store_path = Path(self._tmpdir.name) / "inbox.sqlite"
        self.clock = FakeClock()
        self._live: list[Harness] = []

    def tearDown(self) -> None:
        for harness in self._live:
            harness.runtime.stop()
            wake = getattr(harness.inbox, "wake_cleanup", None)
            if wake is not None:
                wake()
            harness.runtime.close()

    def harness(self, *, api=None, store_path=None) -> Harness:
        api = api if api is not None else SessionAPI()
        if store_path is None:
            store_path = Path(self._tmpdir.name) / f"inbox-{len(self._live)}.sqlite"
        inbox = Inbox(
            owner_telegram_id=OWNER,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=store_path,
            mcp_create_bearer=BEARER,
            bot_token=TOKEN,
            clock=self.clock,
        )
        adapter = tg.TelegramAdapter(inbox, api, owner_id=OWNER, bot_token=TOKEN)
        inbox._notifier = adapter
        inbox._telegram = adapter
        runtime = Runtime(
            inbox=inbox,
            telegram=adapter,
            api=api,
            server=_NullServer(),
            poll_seconds=25,
        )
        saved = self._saved_offset(inbox.store_path)
        runtime.next_update_id = 0 if saved is None else saved
        runtime._offset_ready = saved is not None
        harness = Harness(inbox, adapter, runtime, api, clock=self.clock)
        self._live.append(harness)
        return harness

    @staticmethod
    def _saved_offset(path: Path):
        if not Path(path).exists():
            return None
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute(
                "SELECT value FROM durable_state WHERE key = 'telegram_next_offset'"
            ).fetchone()
        return None if row is None else int(row[0])

    def wait_state(self, harness: Harness, request_id: str, state: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            card = harness.inbox._cards.get(request_id)
            if card is not None and card.state == state:
                return True
            time.sleep(0.01)
        return False

    # --- U1: acknowledgement before work, ACK failures contained -------------

    def test_callback_ack_precedes_blocked_create(self) -> None:
        cases = (
            ("ask_question", {"question": "Ship the intake fix?"}),
            ("request_approval", {"title": "Ship the intake fix?"}),
            ("request_feedback", {"title": "Ship the intake fix?"}),
            ("notify_user", {"title": "Status while a tap arrives"}),
        )
        for kind, fields in cases:
            with self.subTest(kind=kind):
                harness = self.harness()
                card = harness.create(external_id=f"tap-{kind}")
                entered, release = threading.Event(), threading.Event()
                harness.api.send_gate = (entered, release)
                errors: list[BaseException] = []

                def create() -> None:
                    try:
                        harness.create(external_id=f"blocked-{kind}", kind=kind, **fields)
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)

                creator = threading.Thread(target=create, daemon=True)
                creator.start()
                self.assertTrue(entered.wait(2), "the create never reached its remote send")
                harness.api.get_updates = lambda *, offset, timeout: [
                    harness.tap_update(card, 3, "cb-blocked")
                ]
                harness.runtime.next_update_id = 3
                harness.runtime._offset_ready = True
                poller = threading.Thread(target=harness.runtime.poll_once, daemon=True)
                poller.start()
                acked = False
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    if harness.api.ack_times:
                        acked = True
                        break
                    time.sleep(0.01)
                committed = self.wait_state(harness, card["request_id"], "tapped", 1.0) if acked else False
                release.set()
                creator.join(5)
                poller.join(5)
                self.assertEqual(errors, [])
                self.assertTrue(acked, "the acknowledgement must precede the blocked create")
                self.assertTrue(
                    committed, "the answer must commit while the unrelated send is still blocked"
                )
                self.assertEqual(harness.api.answers.count("cb-blocked"), 1)
                # Healthy control: with nothing blocked the same path stays < 1 s.
                fresh = harness.create(external_id=f"control-{kind}")
                harness.api.get_updates = lambda *, offset, timeout: [
                    harness.tap_update(fresh, 4, "cb-control")
                ]
                harness.runtime.next_update_id = 4
                started = time.monotonic()
                harness.runtime.poll_once()
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(harness.stored(fresh["request_id"])["state"], "tapped")

    def test_ack_failure_cannot_poison_batch(self) -> None:
        for mode in ("http400", "telegram_error", "timeout"):
            with self.subTest(mode=mode):
                harness = self.harness()
                first = harness.create(external_id=f"ack-first-{mode}")
                second = harness.create(external_id=f"ack-second-{mode}")
                batch = [
                    harness.tap_update(first, 3, "cb-3"),
                    harness.tap_update(second, 4, "cb-4"),
                ]
                harness.api.get_updates = lambda *, offset, timeout: (
                    batch if offset <= 3 else []
                )
                harness.runtime.next_update_id = 3
                harness.runtime._offset_ready = True
                if mode == "http400":
                    self._install_http_transport(harness, refuse=("cb-3",))
                elif mode == "telegram_error":
                    harness.api.ack_errors["cb-3"] = TelegramAPIError("transport lost")
                else:
                    harness.api.ack_errors["cb-3"] = TimeoutError("ack timed out")
                try:
                    harness.runtime.poll_once()
                except Exception as exc:
                    self.fail(f"one refused acknowledgement escaped poll_once: {type(exc).__name__}")
                self.assertEqual(harness.stored(first["request_id"])["state"], "tapped")
                self.assertEqual(harness.stored(second["request_id"])["state"], "tapped")
                self.assertEqual(harness.offset(), 5)
                first_answer = harness.stored(first["request_id"])["responded_at"]
                # The real loop keeps polling; it never replays the poisoned update.
                polls: list[int] = []

                def poll(*, offset, timeout):
                    polls.append(offset)
                    harness.runtime.stop()
                    return []

                harness.api.get_updates = poll
                harness.runtime._stopping.clear()
                harness.runtime.run()
                harness.runtime._stopping.clear()
                self.assertNotIn(3, polls)
                # A restart reads the committed answers and the advanced cursor.
                restarted = self.harness(store_path=harness.inbox.store_path, api=SessionAPI())
                restarted.api.get_updates = lambda *, offset, timeout: []
                restarted.runtime.poll_once()
                self.assertEqual(restarted.stored(first["request_id"])["responded_at"], first_answer)
                self.assertEqual(restarted.stored(first["request_id"])["response_choice"], "Approve")
                self.assertEqual(restarted.stored(second["request_id"])["response_choice"], "Approve")
                self.assertEqual(restarted.offset(), 5)

    def _install_http_transport(self, harness: Harness, *, refuse: tuple[str, ...]) -> None:
        """Route the adapter's API through _call with a real HTTPError for one callback."""
        api = TelegramBotAPI(TOKEN)
        sent = {"next": 1000}
        batch = harness.api.get_updates

        def urlopen(request, timeout=None):
            method = request.full_url.rsplit("/", 1)[-1]
            payload = json.loads(request.data.decode("utf-8"))
            if method == "answerCallbackQuery":
                callback_id = payload["callback_query_id"]
                if callback_id in refuse:
                    body = json.dumps(
                        {
                            "ok": False,
                            "error_code": 400,
                            "description": "query is too old and response timeout expired",
                        }
                    ).encode("utf-8")
                    raise urllib.error.HTTPError(
                        request.full_url, 400, "Bad Request", None, io.BytesIO(body)
                    )
                return _FakeHTTPResponse(b'{"ok":true,"result":true}')
            if method == "sendMessage":
                sent["next"] += 1
                return _FakeHTTPResponse(
                    json.dumps({"ok": True, "result": {"message_id": sent["next"]}}).encode()
                )
            return _FakeHTTPResponse(b'{"ok":true,"result":true}')

        api.get_updates = batch  # type: ignore[method-assign]
        harness.adapter._api = api
        harness.api = api  # type: ignore[assignment]
        harness.runtime.api = api
        self.addCleanup(setattr, harness.adapter, "_api", harness.api)
        self._transport = mock.patch("urllib.request.urlopen", side_effect=urlopen)
        self._transport.start()
        self.addCleanup(self._transport.stop)


    # --- U3: navigator intake and replay ------------------------------------

    def test_navigator_ack_refusal_still_claims_once(self) -> None:
        harness = self.harness()
        card = harness.create(external_id="nav-ack")
        harness.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        navigator_id = harness.inbox.load_navigator()["message_id"]
        harness.api.ack_errors["cb-nav"] = NotifyRejected("query is too old", code=400)
        data = tg.encode_navigator_callback("A", card["request_id"], card["version"], 0)
        self.tap_message(harness, data, callback_id="cb-nav", message_id=navigator_id)
        stored = harness.stored(card["request_id"])
        self.assertEqual(stored["state"], "tapped")
        self.assertEqual(stored["response_choice"], "Approve")
        # A replay is refused as already answered and records nothing new.
        self.tap_message(harness, data, callback_id="cb-replay", message_id=navigator_id)
        replayed = harness.stored(card["request_id"])
        self.assertEqual(replayed["responded_at"], stored["responded_at"])
        self.assertEqual(replayed["response_choice"], "Approve")
        # A later distinct card is still handled in the same transport health.
        later = harness.create(external_id="nav-later")
        self.tap_card(harness, later, callback_id="cb-later")
        self.assertEqual(harness.stored(later["request_id"])["state"], "tapped")
        self.assertEqual(len(harness.inbox.call_tool("list_unprocessed")), 2)

    def test_ack_timeout_is_short_and_other_timeouts_unchanged(self) -> None:
        api = TelegramBotAPI(TOKEN)
        bodies = {
            "answerCallbackQuery": b'{"ok":true,"result":true}',
            "sendMessage": b'{"ok":true,"result":{"message_id":1}}',
            "editMessageReplyMarkup": b'{"ok":true,"result":true}',
            "getUpdates": b'{"ok":true,"result":[]}',
        }
        calls: list[tuple[str, int | None]] = []

        def urlopen(request, timeout=None):
            method = request.full_url.rsplit("/", 1)[-1]
            calls.append((method, timeout))
            return _FakeHTTPResponse(bodies[method])

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            api.answer_callback_query("cb")
            api.send_message(OWNER, "hi", None)
            api.edit_message_reply_markup(OWNER, 1, EMPTY)
            api.get_updates(offset=-1, timeout=0)
            api.get_updates(offset=0, timeout=25)
        self.assertEqual(
            calls,
            [
                ("answerCallbackQuery", 1),
                ("sendMessage", 15),
                ("editMessageReplyMarkup", 15),
                ("getUpdates", 15),
                ("getUpdates", 35),
            ],
        )

    # --- U2: lock/effect split, cleanup worker ------------------------------

    def test_all_outbound_callers_release_inbox_lock(self) -> None:
        def opener_for(harness: Harness, other: dict, hook: str):
            def opener() -> None:
                harness.inbox.call_tool("get_response", {"request_id": other["request_id"]})
                if hook != "on_strip":
                    harness.inbox.claim(
                        other["request_id"], version=1, from_id=OWNER, choice="Approve"
                    )

            return opener

        def card_prepare(external_id: str, *, claim: bool = False, keep_flag: bool = False):
            def prepare(harness: Harness) -> None:
                card = harness.create(external_id=external_id)
                harness.target = card
                if claim:
                    if keep_flag:
                        message_id = harness.stored(card["request_id"])["telegram_message_id"]
                        harness.api.strip_errors[message_id] = RuntimeError("strip unavailable")
                    harness.inbox.claim(
                        card["request_id"], version=1, from_id=OWNER, choice="Approve"
                    )

            return prepare

        def create_prepare(external_id: str, kind: str = "request_approval", **fields):
            def prepare(harness: Harness) -> None:
                harness.target = harness.create(
                    external_id=external_id, kind=kind, **fields
                )

            return prepare

        def expire(harness: Harness) -> None:
            harness.clock.now += 100_000

        def expired_card(external_id: str):
            def prepare(harness: Harness) -> None:
                card = harness.create(external_id=external_id)
                harness.target = card
                expire(harness)

            return prepare

        def duplicate_prepare(harness: Harness) -> None:
            harness.api.send_errors.append(NotifyRejected("rejected", code=500))
            with self.assertRaises(NotifyRejected):
                harness.create(external_id="row-duplicate")

        def swallow(call):
            def trigger(harness: Harness) -> None:
                try:
                    call(harness)
                except Exception:
                    pass

            return trigger
        def target_id(harness: Harness) -> str:
            return harness.target["request_id"]

        def update_trigger(renotify: bool = False):
            def trigger(harness: Harness) -> None:
                arguments = {
                    "request_id": target_id(harness),
                    "expected_version": harness.target["version"],
                    "title": "Revised",
                }
                if renotify:
                    arguments["renotify"] = True
                harness.inbox.call_tool("update_request", arguments)

            return trigger

        rows = [
            ("create approval", "on_send", lambda h: None,
             lambda h: h.create(external_id="row-approval")),
            ("create question", "on_send", lambda h: None,
             lambda h: h.create(external_id="row-question", kind="ask_question")),
            ("create feedback", "on_send", lambda h: None,
             lambda h: h.create(external_id="row-feedback", kind="request_feedback")),
            ("notify user", "on_send", lambda h: None,
             lambda h: h.create(external_id="row-notify", kind="notify_user")),
            ("duplicate create retry", "on_send", duplicate_prepare,
             lambda h: h.create(external_id="row-duplicate")),
            ("update old-message strip", "on_strip", card_prepare("row-update"),
             update_trigger()),
            ("update renotify send", "on_send", card_prepare("row-renotify"),
             update_trigger(renotify=True)),
            ("cancel strip", "on_strip", card_prepare("row-cancel"),
             lambda h: h.inbox.call_tool("cancel_request", {"request_id": target_id(h)})),
            ("claim success strip", "on_strip", card_prepare("row-claim"),
             lambda h: h.inbox.claim(target_id(h), version=1, from_id=OWNER, choice="Approve")),
            ("refused claim strip", "on_strip",
             card_prepare("row-refused", claim=True, keep_flag=True),
             lambda h: swallow(lambda hh: hh.inbox.claim(
                 target_id(hh), version=1, from_id=OWNER, choice="Deny"))(h)),
            ("direct reply strip", "on_strip", card_prepare("row-reply"),
             lambda h: self.reply(
                 h, h.stored(target_id(h))["telegram_message_id"], "reply answer")),
            ("prompt reply strip", "on_strip",
             lambda h: (card_prepare("row-prompt")(h), h.inbox.record_reply_prompt(
                 OWNER, 999, target_id(h), h.target["version"])),
             lambda h: self.reply(h, 999, "prompt answer")),
            ("record_tap strip", "on_strip", card_prepare("row-record"),
             lambda h: (h.inbox.record_tap(target_id(h), choice="Approve"),
                        h.inbox._service_cleanup())),
            ("expiry via get_response", "on_strip", expired_card("row-exp-read"),
             lambda h: h.inbox.call_tool("get_response", {"request_id": target_id(h)})),
            ("expiry via list_pending", "on_strip", expired_card("row-exp-list"),
             lambda h: h.inbox.call_tool("list_pending")),
            ("expiry via mark_processed", "on_strip", expired_card("row-exp-mark"),
             lambda h: swallow(lambda hh: hh.inbox.call_tool(
                 "mark_processed", {"request_id": target_id(hh)}))(h)),
            ("expiry via report_execution", "on_strip", expired_card("row-exp-report"),
             lambda h: swallow(lambda hh: hh.inbox.call_tool(
                 "report_execution",
                 {"request_id": target_id(hh), "outcome": "accepted"}))(h)),
            ("expiry via _park", "on_strip", expired_card("row-exp-park"),
             lambda h: (h.inbox._park(target_id(h), 0.05), h.inbox._service_cleanup())),
            ("unlocked reconcile control", "on_strip",
             card_prepare("row-reconcile", claim=True, keep_flag=True),
             lambda h: h.inbox.reconcile_notifications()),
            ("unlocked finalize control", "on_keyboard", card_prepare("row-finalize"),
             lambda h: h.inbox._finalize_telegram(h.inbox._cards[target_id(h)])),
            ("unlocked strip_closed control", "on_strip",
             card_prepare("row-strip", claim=True, keep_flag=True),
             lambda h: h.inbox.strip_closed([h.inbox._cards[target_id(h)]])),
        ]

        for name, hook, prepare, trigger in rows:
            with self.subTest(caller=name):
                harness = self.harness()
                other = harness.create(external_id=f"other-{name.replace(' ', '-')}")
                prepare(harness)
                stop = threading.Event()
                worker = None
                if name.startswith("expiry"):
                    # The expiry drain belongs to the worker here; otherwise a
                    # competing read could drain the queued strip itself.
                    worker = threading.Thread(
                        target=harness.inbox.cleanup_loop,
                        kwargs={"cancelled": stop.is_set},
                        daemon=True,
                        name="cleanup-thread",
                    )
                    worker.start()
                    self.wait_until(lambda: harness.inbox._cleanup_active, 3)
                try:
                    observed = self.assert_effect_off_lock(
                        harness,
                        lambda: trigger(harness),
                        hook=hook,
                        opener=opener_for(harness, other, hook),
                    )
                finally:
                    stop.set()
                    harness.inbox.wake_cleanup()
                    if worker is not None:
                        worker.join(3)
                allowed = {"expiry via mark_processed", "expiry via report_execution"}
                if name not in allowed:
                    self.assertEqual(observed["errors"], [], f"{name} raised")

        # list_unprocessed never expires an open card (tapped-only control).
        control = self.harness()
        created = control.create(external_id="control-unprocessed")
        control.clock.now += 100_000
        self.assertEqual(control.inbox.call_tool("list_unprocessed"), [])
        self.assertEqual(control.stored(created["request_id"])["state"], "open")

    def test_slow_cleanup_does_not_delay_later_callback_or_reply(self) -> None:
        harness = self.harness()
        first = harness.create(external_id="slow-a")
        second = harness.create(external_id="slow-b")
        third = harness.create(external_id="slow-c")
        first_message = harness.stored(first["request_id"])["telegram_message_id"]
        entered, release = threading.Event(), threading.Event()
        harness.api.strip_entered[first_message] = entered
        harness.api.strip_gates[first_message] = (entered, release)
        batch = [
            harness.tap_update(first, 10, "cb-a"),
            harness.tap_update(second, 11, "cb-b"),
            {
                "update_id": 12,
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "reply_to_message": {
                        "message_id": harness.stored(third["request_id"])["telegram_message_id"]
                    },
                    "text": "the reply answer",
                },
            },
        ]
        polls: list[int] = []

        def feeder(offset: int):
            polls.append(offset)
            return batch if offset <= 10 else []

        harness.runtime.next_update_id = 10
        harness.runtime._offset_ready = True
        thread = self.start_runtime(harness, feeder)
        try:
            self.assertTrue(entered.wait(3), "the worker never blocked on the first strip")
            self.wait_until(
                lambda: harness.stored(third["request_id"]).get("response_text")
                == "the reply answer",
                3,
                "the reply in the same batch was not recorded",
            )
            self.assertEqual(harness.stored(first["request_id"])["response_choice"], "Approve")
            self.assertEqual(harness.stored(second["request_id"])["response_choice"], "Approve")
            self.assertEqual(harness.stored(third["request_id"])["responded_via"], "telegram-reply")
            self.assertEqual(harness.offset(), 13)
            self.assertEqual(harness.api.answers.count("cb-a"), 1)
            self.assertEqual(harness.api.answers.count("cb-b"), 1)
            self.assertEqual(len(harness.api.answers), 2)  # a reply has no callback ACK
            self.wait_until(lambda: 13 in polls, 3, "the poll thread never advanced past the batch")
            blocked_threads = set(harness.api.strip_threads[first_message])
            self.assertNotIn("poll-thread", blocked_threads)
            self.assertNotIn("MainThread", blocked_threads)
            self.assertEqual(blocked_threads, {"telegram-reconcile"})
        finally:
            release.set()
            harness.runtime.stop()
            thread.join(5)
        self.assertFalse(thread.is_alive())

        restarted = self.harness(store_path=harness.inbox.store_path)
        restarted.inbox.reconcile_notifications()
        self.assertEqual(restarted.stored(first["request_id"])["response_choice"], "Approve")
        self.assertEqual(restarted.stored(second["request_id"])["response_choice"], "Approve")
        self.assertEqual(restarted.stored(third["request_id"])["response_text"], "the reply answer")
        self.assertEqual(restarted.offset(), 13)

        # Startup reconciliation is held: the poll thread still never strips.
        held = self.harness(store_path=Path(self._tmpdir.name) / "held.sqlite")
        held_card = held.create(external_id="held-card")
        held_message = held.stored(held_card["request_id"])["telegram_message_id"]
        strip_entered, strip_release = threading.Event(), threading.Event()
        held.api.strip_entered[held_message] = strip_entered
        held.api.strip_gates[held_message] = (strip_entered, strip_release)
        reconcile_started, allow_reconcile = threading.Event(), threading.Event()
        original_reconcile = held.inbox.reconcile_notifications

        def slow_reconcile(**kwargs):
            reconcile_started.set()
            allow_reconcile.wait(5)
            return original_reconcile(**kwargs)

        held.inbox.reconcile_notifications = slow_reconcile
        held.runtime.next_update_id = 20
        held.runtime._offset_ready = True
        held_batch = [held.tap_update(held_card, 20, "cb-held")]
        held_thread = self.start_runtime(
            held, lambda offset: held_batch if offset <= 20 else []
        )
        try:
            self.assertTrue(reconcile_started.wait(2), "startup reconciliation never started")
            self.assertFalse(held.inbox._cleanup_active)
            self.wait_until(
                lambda: held.stored(held_card["request_id"])["state"] == "tapped", 3,
                "the startup-window tap was not recorded",
            )
            self.assertEqual(held.offset(), 21)
            self.assertFalse(
                strip_entered.wait(0.3),
                "a startup-window tap stripped on the poll thread",
            )
            allow_reconcile.set()
            self.assertTrue(strip_entered.wait(3), "the worker never drained the queued strip")
            self.assertEqual(set(held.api.strip_threads[held_message]), {"telegram-reconcile"})
        finally:
            allow_reconcile.set()
            strip_release.set()
            held.runtime.stop()
            held_thread.join(5)

    def test_cleanup_retry_restart_and_stale_settlement(self) -> None:
        # A failed strip survives on the durable flag, retries no sooner than
        # 1 s and clears once on the matching success.
        harness = self.harness()
        card = harness.create(external_id="cleanup-retry")
        message_id = harness.stored(card["request_id"])["telegram_message_id"]
        stop = threading.Event()
        worker = threading.Thread(
            target=harness.inbox.cleanup_loop,
            kwargs={"cancelled": stop.is_set},
            daemon=True,
            name="cleanup-thread",
        )
        worker.start()
        self.wait_until(lambda: harness.inbox._cleanup_active, 3, "the cleanup worker never started")
        harness.api.strip_errors[message_id] = RuntimeError("telegram down")
        harness.inbox.claim(card["request_id"], version=1, from_id=OWNER, choice="Approve")
        self.assertTrue(harness.stored(card["request_id"])["telegram_keyboard_attached"])
        try:
            self.wait_until(
                lambda: len(harness.api.strip_attempts.get(message_id, [])) >= 2, 5,
                "the failed strip was not retried",
            )
            attempts = harness.api.strip_attempts[message_id]
            self.assertGreaterEqual(attempts[1] - attempts[0], 0.9, "busy retry before 1 s")
            self.wait_until(
                lambda: not harness.stored(card["request_id"])["telegram_keyboard_attached"],
                3,
                "the matching success did not clear the flag",
            )
            cleared = len(harness.api.strip_attempts[message_id])
            time.sleep(1.2)
            self.assertEqual(len(harness.api.strip_attempts[message_id]), cleared)
        finally:
            stop.set()
            harness.inbox.wake_cleanup()
            worker.join(3)
        self.assertFalse(worker.is_alive())

        # A local flag-save failure keeps the flag retryable.
        save_harness = self.harness(store_path=Path(self._tmpdir.name) / "save.sqlite")
        save_card = save_harness.create(external_id="cleanup-save")
        save_message = save_harness.stored(save_card["request_id"])["telegram_message_id"]
        save_harness.api.strip_errors[save_message] = RuntimeError("telegram down")
        save_harness.inbox.claim(
            save_card["request_id"], version=1, from_id=OWNER, choice="Approve"
        )
        store = save_harness.inbox._store
        real_save = store.save_card

        with mock.patch.object(
            store, "save_card", side_effect=sqlite3.OperationalError("disk full")
        ):
            save_harness.inbox._service_cleanup()
        self.assertTrue(
            save_harness.stored(save_card["request_id"])["telegram_keyboard_attached"],
            "a failed flag save lost the retry",
        )
        save_harness.inbox._service_cleanup()
        self.assertFalse(
            save_harness.stored(save_card["request_id"])["telegram_keyboard_attached"]
        )

        # A restart rescans the durable flag and strips it.
        restart = self.harness(store_path=save_harness.inbox.store_path)
        restart.inbox.reconcile_notifications()
        self.assertFalse(restart.stored(save_card["request_id"])["telegram_keyboard_attached"])

        # A newer revision landing during a blocked strip is never restored
        # over, and the captured old locator is the only one stripped.
        stale = self.harness(store_path=Path(self._tmpdir.name) / "stale.sqlite")
        target = stale.create(external_id="cleanup-stale")
        old_message = stale.stored(target["request_id"])["telegram_message_id"]
        gate_in, gate_out = threading.Event(), threading.Event()
        stale.api.strip_entered[old_message] = gate_in
        stale.api.strip_gates[old_message] = (gate_in, gate_out)
        errors: list[BaseException] = []

        def revise() -> None:
            try:
                stale.inbox.call_tool(
                    "update_request",
                    {
                        "request_id": target["request_id"],
                        "expected_version": 1,
                        "title": "Revision two",
                    },
                )
            except BaseException as exc:
                errors.append(exc)

        revision = threading.Thread(target=revise, daemon=True)
        revision.start()
        try:
            self.assertTrue(gate_in.wait(3), "the old-message strip never blocked")
            stale.inbox.call_tool(
                "update_request",
                {
                    "request_id": target["request_id"],
                    "expected_version": 2,
                    "title": "Revision three",
                },
            )
            stale.api.strip_errors[old_message] = RuntimeError("late failure")
        finally:
            gate_out.set()
            revision.join(5)
        stored = stale.stored(target["request_id"])
        self.assertEqual(stored["version"], 3)
        self.assertEqual(stored["state"], "open")
        self.assertEqual(stored["title"], "Revision three")
        self.assertEqual(errors, [], "a stale strip failure was reported as a rollback")
        self.assertEqual(
            sorted(stale.api.strip_attempts),
            [old_message],
            "a newer locator was stripped or the captured one was not",
        )
        self.assertEqual(stored["telegram_message_id"], None)

        # A late keyboard attach after a terminal claim stays retryable.
        late = self.harness(store_path=Path(self._tmpdir.name) / "late.sqlite")
        card_late = late.create(external_id="cleanup-late")
        late_message = late.stored(card_late["request_id"])["telegram_message_id"]
        late.inbox.claim(card_late["request_id"], version=1, from_id=OWNER, choice="Approve")
        late.adapter._messages[card_late["request_id"]] = (
            card_late["version"],
            OWNER,
            late_message,
        )
        late.api.strip_errors[late_message] = RuntimeError("strip down")
        with self.assertRaises(Exception):
            late.adapter.finalize_message(
                {"request_id": card_late["request_id"], "version": card_late["version"]}
            )
        self.assertTrue(late.stored(card_late["request_id"])["telegram_keyboard_attached"])

        # Processing and execution fields survive a failed cleanup.
        marked = late.inbox.call_tool("mark_processed", {"request_id": card_late["request_id"]})
        late.inbox.call_tool(
            "report_execution",
            {"request_id": card_late["request_id"], "outcome": "completed"},
        )
        late.inbox._service_cleanup()
        after = late.stored(card_late["request_id"])
        self.assertEqual(after["processed_at"], marked["processed_at"])
        self.assertEqual(after["execution_status"], "completed")
        self.assertEqual(after["response_choice"], "Approve")

    def test_update_failure_never_restores_over_new_answer(self) -> None:
        harness = self.harness()
        card = harness.create(external_id="update-guard")
        message_id = harness.stored(card["request_id"])["telegram_message_id"]
        entered, release = threading.Event(), threading.Event()
        harness.api.strip_entered[message_id] = entered
        harness.api.strip_gates[message_id] = (entered, release)
        errors: list[BaseException] = []

        def revise() -> None:
            try:
                harness.inbox.call_tool(
                    "update_request",
                    {"request_id": card["request_id"], "expected_version": 1, "title": "Two"},
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=revise, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            answer = harness.inbox.claim(
                card["request_id"], version=2, from_id=OWNER, choice="Approve"
            )
            self.assertEqual(answer["response"]["choice"], "Approve")
            harness.api.strip_errors[message_id] = NotifyRejected("gone", code=400)
        finally:
            release.set()
            thread.join(5)
        stored = harness.stored(card["request_id"])
        self.assertEqual(stored["state"], "tapped")
        self.assertEqual(stored["response_choice"], "Approve")
        self.assertEqual(stored["version"], 2)
        self.assertEqual(stored["title"], "Two")
        self.assertEqual(errors, [], "a stale failure raised over a committed newer state")

        # Same-version/no-concurrency rollback is retained.
        rollback = self.harness(store_path=Path(self._tmpdir.name) / "rollback.sqlite")
        card2 = rollback.create(external_id="update-rollback")
        message2 = rollback.stored(card2["request_id"])["telegram_message_id"]
        rollback.api.strip_errors[message2] = NotifyRejected("gone", code=400)
        with self.assertRaises(NotifyRejected):
            rollback.inbox.call_tool(
                "update_request",
                {"request_id": card2["request_id"], "expected_version": 1, "title": "Lost"},
            )
        restored = rollback.stored(card2["request_id"])
        self.assertEqual(restored["version"], 1)
        self.assertEqual(restored["state"], "open")
        self.assertNotEqual(restored["title"], "Lost")

    def test_notification_reservations_survive_concurrent_io(self) -> None:
        harness = self.harness()
        entered, release = threading.Event(), threading.Event()
        harness.api.send_gate = (entered, release)
        results: list[dict] = []
        errors: list[BaseException] = []

        def create() -> None:
            try:
                results.append(harness.create(external_id="notify-race"))
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=create, daemon=True)
        first.start()
        self.assertTrue(entered.wait(3))
        second_view = harness.create(external_id="notify-race")
        self.assertTrue(second_view["duplicate"])
        self.assertEqual(len(harness.api.send_calls), 1)
        release.set()
        first.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(harness.inbox._cards), 1)

        # A known rejection rolls the reservation back; a retry sends again.
        rejected = self.harness(store_path=Path(self._tmpdir.name) / "rejected.sqlite")
        rejected.api.send_errors.append(NotifyRejected("bad request", code=400))
        with self.assertRaises(NotifyRejected):
            rejected.create(external_id="notify-retry")
        retried = rejected.create(external_id="notify-retry")
        self.assertTrue(retried["duplicate"])
        self.assertEqual(len(rejected.api.send_calls), 2)
        self.assertTrue(rejected.stored(retried["request_id"])["notified"])

        # An ambiguous accepted-then-timeout is never resent across a restart.
        ambiguous = self.harness(store_path=Path(self._tmpdir.name) / "ambiguous.sqlite")
        original_send = ambiguous.api.send_message

        def accepted_then_lost(chat_id, text, reply_markup, parse_mode=None, timing=None):
            original_send(chat_id, text, reply_markup, parse_mode, timing)
            raise TimeoutError("lost after accept")

        ambiguous.api.send_message = accepted_then_lost
        with self.assertRaises(TimeoutError):
            ambiguous.create(external_id="notify-ambiguous")
        self.assertEqual(len(ambiguous.api.send_calls), 1)
        restarted = self.harness(store_path=ambiguous.inbox.store_path)
        restarted.inbox.reconcile_notifications()
        self.assertEqual(restarted.api.send_calls, [])

        # A concurrent terminal state is not reverted by notifier completion.
        terminal = self.harness(store_path=Path(self._tmpdir.name) / "terminal.sqlite")
        blocked, unblock = threading.Event(), threading.Event()
        terminal.api.send_gate = (blocked, unblock)

        def create_terminal() -> None:
            terminal.create(external_id="notify-terminal")

        creator = threading.Thread(target=create_terminal, daemon=True)
        creator.start()
        self.assertTrue(blocked.wait(3))
        card_id = next(iter(terminal.inbox._cards))
        terminal.inbox.claim(card_id, version=1, from_id=OWNER, choice="Approve")
        unblock.set()
        creator.join(5)
        self.assertEqual(terminal.stored(card_id)["state"], "tapped")

    def test_cleanup_worker_lifetime_and_single_poller(self) -> None:
        harness = self.harness()
        card = harness.create(external_id="lifetime")
        message_id = harness.stored(card["request_id"])["telegram_message_id"]
        entered, release = threading.Event(), threading.Event()
        harness.api.strip_entered[message_id] = entered
        harness.api.strip_gates[message_id] = (entered, release)

        def feeder(offset: int):
            harness.runtime._stopping.wait(0.01)
            return []

        poller = self.start_runtime(harness, feeder)
        try:
            self.assertTrue(harness.runtime._reconciled.wait(2))
            harness.inbox.claim(card["request_id"], version=1, from_id=OWNER, choice="Approve")
            self.assertTrue(entered.wait(3), "the cleanup worker never reached the strip")
            worker = harness.runtime._reconcile_worker
            self.assertIsNotNone(worker)
            started = time.monotonic()
            harness.runtime.stop()
            self.assertLess(time.monotonic() - started, 0.5, "stop joined the blocked cleanup")
            self.assertTrue(worker.is_alive(), "stop did not leave the in-flight strip alone")
            self.assertIn("telegram-reconcile", [thread.name for thread in threading.enumerate()])
        finally:
            release.set()
        worker.join(5) if worker is not None else None
        self.assertFalse(worker.is_alive() if worker is not None else False)
        harness.runtime.close()
        self.assertIsNone(harness.inbox._store)
        self.assertFalse(poller.is_alive())
        names = [thread.name for thread in threading.enumerate()]
        self.assertLessEqual(names.count("telegram-reconcile"), 1)
        self.assertLessEqual(names.count("poll-thread"), 1)

        # Console mode: no Telegram poller, reconciliation still works.
        console_env = {
            key: value
            for key, value in {
                "PARAPHE_OWNER_TELEGRAM_ID": str(OWNER),
                "PARAPHE_MCP_CREATE_BEARER": BEARER,
                "PARAPHE_OWNER_ANSWER_TOKEN": "test-owner-token",
                "PARAPHE_MCP_PORT": "0",
                "PARAPHE_STORE_PATH": str(Path(self._tmpdir.name) / "console.sqlite"),
            }.items()
        }
        console = Runtime.start(console_env)
        try:
            self.assertIsNone(console.telegram)
            self.assertTrue(console.inbox.reconcile_notifications())
            created = console.inbox.call_tool(
                "request_approval",
                {"title": "Console card", "external_id": "console-card"},
                bearer=BEARER,
            )
            answer = console.inbox.claim(
                created["request_id"],
                version=1,
                from_id=console.inbox.owner_telegram_id,
                choice="Approve",
            )
            self.assertEqual(answer["response"]["choice"], "Approve")
        finally:
            console.close()
        self.assertIsNone(console.inbox._store)

    # --- U3: write failure replay, refusal matrix ---------------------------

    def test_answer_and_cursor_write_failures_are_replayable(self) -> None:
        # Failure before the answer commit: cursor retained, recovery commits once.
        before = self.harness(store_path=Path(self._tmpdir.name) / "before.sqlite")
        card = before.create(external_id="write-before")
        before.inbox._ensure_store().save_telegram_next_offset(10)
        before.runtime.next_update_id = 10
        before.runtime._offset_ready = True
        before.api.get_updates = lambda *, offset, timeout: (
            [before.tap_update(card, 10, "cb-10")] if offset <= 10 else []
        )
        store = before.inbox._store
        real_save = store.save_card
        with mock.patch.object(store, "save_card", side_effect=sqlite3.OperationalError("disk")):
            with self.assertRaises(sqlite3.OperationalError):
                before.runtime.poll_once()
        self.assertEqual(before.stored(card["request_id"])["state"], "open")
        self.assertEqual(before.offset(), 10)
        before.runtime.poll_once()
        self.assertEqual(before.stored(card["request_id"])["state"], "tapped")
        self.assertEqual(before.offset(), 11)

        # Failure after the commit but before the cursor save: replay is a
        # refusal, never a second answer, and the later cursor catches up.
        after = self.harness(store_path=Path(self._tmpdir.name) / "after.sqlite")
        card2 = after.create(external_id="write-after")
        after.inbox._ensure_store().save_telegram_next_offset(20)
        after.runtime.next_update_id = 20
        after.runtime._offset_ready = True
        after.api.get_updates = lambda *, offset, timeout: (
            [after.tap_update(card2, 20, "cb-20")] if offset <= 20 else []
        )
        store2 = after.inbox._store
        real_offset_save = store2.save_telegram_next_offset
        calls = {"count": 0}

        def flaky_offset(offset: int) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("disk")
            return real_offset_save(offset)

        with mock.patch.object(store2, "save_telegram_next_offset", side_effect=flaky_offset):
            with self.assertRaises(sqlite3.OperationalError):
                after.runtime.poll_once()
        first = after.stored(card2["request_id"])
        self.assertEqual(first["state"], "tapped")
        self.assertEqual(after.offset(), 20)
        after.runtime.poll_once()
        replayed = after.stored(card2["request_id"])
        self.assertEqual(replayed["responded_at"], first["responded_at"])
        self.assertEqual(replayed["response_choice"], first["response_choice"])
        self.assertEqual(after.offset(), 21)

        # An unexpected handler failure retains the cursor instead of skipping.
        crash = self.harness(store_path=Path(self._tmpdir.name) / "crash.sqlite")
        card3 = crash.create(external_id="write-crash")
        crash.inbox._ensure_store().save_telegram_next_offset(30)
        crash.runtime.next_update_id = 30
        crash.runtime._offset_ready = True
        crash.api.get_updates = lambda *, offset, timeout: (
            [crash.tap_update(card3, 30, "cb-30")] if offset <= 30 else []
        )
        with mock.patch.object(
            crash.adapter, "handle_update", side_effect=RuntimeError("handler bug")
        ):
            with self.assertRaises(RuntimeError):
                crash.runtime.poll_once()
        self.assertEqual(crash.offset(), 30)
        self.assertEqual(crash.stored(card3["request_id"])["state"], "open")

    def test_refused_and_malformed_actions_do_not_poison_later_valid_update(self) -> None:
        harness = self.harness()
        stale = harness.create(external_id="matrix-stale")
        expired = harness.create(
            external_id="matrix-expired", expires_in_seconds=900
        )
        cancelled = harness.create(external_id="matrix-cancelled")
        answered = harness.create(external_id="matrix-answered")
        valid = harness.create(external_id="matrix-valid")
        harness.inbox.call_tool("cancel_request", {"request_id": cancelled["request_id"]})
        harness.inbox.claim(answered["request_id"], version=1, from_id=OWNER, choice="Approve")
        stale_data = tg.encode_callback_data(stale["request_id"], 99, 0)
        batch = [
            harness.tap_update(stale, 40, "cb-stale"),
            harness.tap_update(expired, 41, "cb-expired"),
            harness.tap_update(cancelled, 42, "cb-cancelled"),
            harness.tap_update(answered, 43, "cb-answered"),
            {
                "update_id": 44,
                "callback_query": {
                    "id": "cb-wrong-message",
                    "from": {"id": OWNER},
                    "data": tg.encode_callback_data(stale["request_id"], stale["version"], 0),
                    "message": {"message_id": 9999, "chat": {"id": OWNER, "type": "private"}},
                },
            },
            {
                "update_id": 45,
                "callback_query": {
                    "id": "cb-invalid-choice",
                    "from": {"id": OWNER},
                    "data": tg.encode_callback_data(stale["request_id"], stale["version"], 3),
                    "message": {
                        "message_id": harness.stored(stale["request_id"])["telegram_message_id"],
                        "chat": {"id": OWNER, "type": "private"},
                    },
                },
            },
            harness.tap_update(valid, 46, "cb-valid"),
        ]
        # Rewrite the first entry as a stale version and expire the second.
        batch[0] = {
            "update_id": 40,
            "callback_query": {
                "id": "cb-stale",
                "from": {"id": OWNER},
                "data": stale_data,
                "message": {
                    "message_id": harness.stored(stale["request_id"])["telegram_message_id"],
                    "chat": {"id": OWNER, "type": "private"},
                },
            },
        }
        harness.clock.now += 901
        harness.runtime.next_update_id = 40
        harness.runtime._offset_ready = True
        harness.api.get_updates = lambda *, offset, timeout: (
            [item for item in batch if item["update_id"] >= offset]
        )
        harness.runtime.poll_once()
        self.assertEqual(harness.offset(), 47)
        self.assertEqual(harness.stored(stale["request_id"])["state"], "open")
        self.assertEqual(harness.stored(expired["request_id"])["state"], "expired")
        self.assertEqual(harness.stored(cancelled["request_id"])["state"], "cancelled")
        self.assertEqual(harness.stored(answered["request_id"])["response_choice"], "Approve")
        self.assertEqual(harness.stored(valid["request_id"])["response_choice"], "Approve")
        refusal_lines = [call["text"] for call in harness.api.send_calls if call["text"].startswith("Not recorded:")]
        self.assertTrue(refusal_lines, "no healthy owner-facing refusal was shown")
        self.assertTrue(all("Not recorded:" in line for line in refusal_lines))

        # Invalid update ids still refuse without coercion or a cursor skip.
        bad = self.harness(store_path=Path(self._tmpdir.name) / "bad-id.sqlite")
        bad.inbox._ensure_store().save_telegram_next_offset(50)
        bad.runtime.next_update_id = 50
        bad.runtime._offset_ready = True
        bad.api.get_updates = lambda *, offset, timeout: [{"update_id": "50", "message": {}}]
        with self.assertRaises(TelegramAPIError):
            bad.runtime.poll_once()
        self.assertEqual(bad.offset(), 50)

        # A failed reply-refusal notice and a failed navigator reply hint are
        # presentation faults: they never stop the update or its cursor.
        notice = self.harness(store_path=Path(self._tmpdir.name) / "notice.sqlite")
        card_prompt = notice.create(external_id="notice-prompt")
        notice.inbox.record_reply_prompt(OWNER, 777, card_prompt["request_id"], 1)
        notice.inbox.call_tool("cancel_request", {"request_id": card_prompt["request_id"]})
        notice.api.send_errors.append(NotifyRejected("notice refused", code=400))
        notice.runtime.next_update_id = 60
        notice.runtime._offset_ready = True
        notice.api.get_updates = lambda *, offset, timeout: [
            {
                "update_id": 60,
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "reply_to_message": {"message_id": 777},
                    "text": "too late",
                },
            }
        ]
        notice.runtime.poll_once()
        self.assertEqual(notice.offset(), 61)

        hint = self.harness(store_path=Path(self._tmpdir.name) / "hint.sqlite")
        hint.create(external_id="hint-card")
        hint.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        navigator_id = hint.inbox.load_navigator()["message_id"]
        hint.api.send_errors.append(TelegramAPIError("hint refused"))
        hint.runtime.next_update_id = 70
        hint.runtime._offset_ready = True
        hint.api.get_updates = lambda *, offset, timeout: [
            {
                "update_id": 70,
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "reply_to_message": {"message_id": navigator_id},
                    "text": "a reply to the navigator",
                },
            }
        ]
        hint.runtime.poll_once()
        self.assertEqual(hint.offset(), 71)

    def test_navigator_effect_failure_does_not_poison_next_answer(self) -> None:
        # /pending replacement failure: logged, previous navigator kept, later
        # update still handled and its cursor advanced.
        harness = self.harness()
        card = harness.create(external_id="nav-effect")
        harness.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        first_nav = harness.inbox.load_navigator()["message_id"]
        harness.api.send_errors.append(NotifyRejected("send refused", code=500))
        output = io.StringIO()
        harness.runtime.next_update_id = 80
        harness.runtime._offset_ready = True
        harness.api.get_updates = lambda *, offset, timeout: [
            {
                "update_id": 80,
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                },
            }
        ]
        with mock.patch("sys.stdout", output):
            harness.runtime.poll_once()
        self.assertIn("Telegram navigator send failed: NotifyRejected", output.getvalue())
        self.assertEqual(harness.inbox.load_navigator()["message_id"], first_nav)
        self.assertEqual(harness.offset(), 81)

        # Write-answer prompt send failure: nothing is bound to a fake reply.
        prompt = self.harness(store_path=Path(self._tmpdir.name) / "prompt-effect.sqlite")
        prompt_card = prompt.create(external_id="nav-prompt")
        prompt.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        nav_id = prompt.inbox.load_navigator()["message_id"]
        prompt.api.send_errors.append(TelegramAPIError("prompt refused"))
        self.tap_message(
            prompt,
            tg.encode_navigator_callback("W", prompt_card["request_id"], 1),
            callback_id="cb-write",
            message_id=nav_id,
        )
        self.reply(
            prompt,
            prompt.api.messages[-1]["message_id"] if prompt.api.messages else 1,
            "unbound",
        )
        self.assertIsNone(prompt.stored(prompt_card["request_id"])["response_text"])
        self.tap_card(prompt, prompt_card, callback_id="cb-after-prompt")
        self.assertEqual(prompt.stored(prompt_card["request_id"])["response_choice"], "Approve")

        # A slow existing navigator edit never delays recording an answer, and
        # a newer selection survives the older publication.
        slow = self.harness(store_path=Path(self._tmpdir.name) / "slow-edit.sqlite")
        slow_card = slow.create(external_id="nav-slow")
        slow.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        slow_nav = slow.inbox.load_navigator()["message_id"]
        entered, release = threading.Event(), threading.Event()

        def on_edit() -> None:
            entered.set()
            release.wait(5)

        slow.api.on_edit = on_edit
        tick_errors: list[BaseException] = []

        def tick() -> None:
            try:
                slow.adapter.refresh_navigator(time.time())
            except BaseException as exc:
                tick_errors.append(exc)

        ticker = threading.Thread(target=tick, daemon=True)
        slow.adapter._navigator_intent({"kind": "list", "anchor": None}, "refresh")
        ticker.start()
        try:
            self.assertTrue(entered.wait(3), "the navigator edit never started")
            started = time.monotonic()
            self.tap_message(
                slow,
                tg.encode_navigator_callback("A", slow_card["request_id"], 1, 0),
                callback_id="cb-slow-answer",
                message_id=slow_nav,
            )
            self.assertLess(
                time.monotonic() - started, 1.0, "an answer waited for the slow navigator edit"
            )
            self.assertEqual(slow.stored(slow_card["request_id"])["response_choice"], "Approve")
            self.tap_message(
                slow,
                tg.encode_navigator_callback("O", slow_card["request_id"]),
                callback_id="cb-slow-open",
                message_id=slow_nav,
            )
            saved = slow.inbox.load_navigator()
            self.assertEqual(saved["view"]["request_id"], slow_card["request_id"])
            self.assertGreaterEqual(
                int(saved.get("generation") or 0), 2, "the newer intent generation was lost"
            )
        finally:
            release.set()
            ticker.join(5)
        slow.api.on_edit = None
        self.assertEqual(tick_errors, [], "the slow publish raised")
        slow.adapter.refresh_navigator(time.time() + 1)
        self.assertTrue(
            slow.api.texts
            and any(
                marker in slow.api.texts[-1]["text"]
                for marker in ("Answer recorded: Approve.", "This card is closed", "Ship it?")
            ),
            f"the navigator never published the newer intent after the slow edit: {slow.api.texts!r}",
        )

    def test_credentials_and_owner_guards_remain_closed(self) -> None:
        harness = self.harness()
        card = harness.create(external_id="guard-card")
        server = harness.inbox.serve("127.0.0.1", 0)
        self.addCleanup(server.close)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/answer",
            data=json.dumps(
                {
                    "request_id": card["request_id"],
                    "version": card["version"],
                    "choice": "Approve",
                }
            ).encode("utf-8"),
            headers={"Authorization": f"Bearer {BEARER}", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        self.assertEqual(harness.stored(card["request_id"])["state"], "open")
        self.assertIsNone(harness.stored(card["request_id"])["response_choice"])

        # No MCP/HTTP tool answers a decision: the create credential is not owner.
        self.assertFalse({"answer", "claim", "answer_request"} & set(inbox_mod.TOOL_NAMES))
        self.assertFalse(harness.inbox.check_answer_token(BEARER))

        sends = len(harness.api.send_calls)
        # A non-owner tap records nothing and receives no content.
        message_id = harness.stored(card["request_id"])["telegram_message_id"]
        self.tap_card(harness, card, callback_id="cb-stranger", from_id=STRANGER)
        self.assertEqual(harness.stored(card["request_id"])["state"], "open")
        self.assertEqual(len(harness.api.send_calls), sends)
        # A group chat is not the owner's private chat.
        self.tap_card(harness, card, callback_id="cb-group", chat_type="group")
        self.assertEqual(harness.stored(card["request_id"])["state"], "open")
        # A forwarded navigator tap is ignored.
        harness.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        navigator_id = harness.inbox.load_navigator()["message_id"]
        self.tap_message(
            harness,
            tg.encode_navigator_callback("A", card["request_id"], card["version"], 0),
            callback_id="cb-forwarded",
            message_id=navigator_id,
            forwarded=True,
        )
        self.assertEqual(harness.stored(card["request_id"])["state"], "open")
        # A stale version, a wrong locator and an expired card all refuse.
        self.tap_message(
            harness,
            tg.encode_callback_data(card["request_id"], card["version"] + 1, 0),
            callback_id="cb-stale",
            message_id=message_id,
        )
        self.tap_message(
            harness,
            tg.encode_callback_data(card["request_id"], card["version"], 0),
            callback_id="cb-locator",
            message_id=9999,
        )
        harness.clock.now += 100_000
        self.tap_message(
            harness,
            tg.encode_callback_data(card["request_id"], card["version"], 0),
            callback_id="cb-expired",
            message_id=message_id,
        )
        self.assertEqual(harness.stored(card["request_id"])["state"], "expired")

        # First valid answer wins across the navigator and the original button.
        winner = self.harness(store_path=Path(self._tmpdir.name) / "winner.sqlite")
        target = winner.create(external_id="guard-winner")
        winner.adapter.handle_update(
            {
                "message": {
                    "from": {"id": OWNER},
                    "chat": {"id": OWNER, "type": "private"},
                    "text": "/pending",
                }
            }
        )
        winner_nav = winner.inbox.load_navigator()["message_id"]
        self.tap_message(
            winner,
            tg.encode_navigator_callback("A", target["request_id"], 1, 0),
            callback_id="cb-first",
            message_id=winner_nav,
        )
        self.tap_card(winner, target, callback_id="cb-second")
        stored = winner.stored(target["request_id"])
        self.assertEqual(stored["response_choice"], "Approve")
        self.assertEqual(stored["responded_via"], "telegram")

    def test_waiters_read_committed_answer_and_safe_fault_logs(self) -> None:
        harness = self.harness()
        card = harness.create(external_id="waiter")
        message_id = harness.stored(card["request_id"])["telegram_message_id"]
        entered, release = threading.Event(), threading.Event()
        harness.api.strip_entered[message_id] = entered
        harness.api.strip_gates[message_id] = (entered, release)
        results: list[dict] = []

        def parked() -> None:
            results.append(
                harness.inbox.call_tool(
                    "get_response",
                    {"request_id": card["request_id"], "wait_seconds": 30},
                )
            )

        waiter = threading.Thread(target=parked, daemon=True)
        waiter.start()
        self.wait_until(
            lambda: bool(harness.inbox._waiters.get(card["request_id"])), 3,
            "the waiter never parked",
        )
        harness.runtime.next_update_id = 0
        harness.runtime._offset_ready = True
        poller = self.start_runtime(harness, lambda offset: [])

        tap_errors: list[BaseException] = []

        def feeder_tap() -> None:
            try:
                self.tap_card(harness, card, callback_id="cb-waiter")
            except BaseException as exc:
                tap_errors.append(exc)

        tap_thread = threading.Thread(target=feeder_tap, daemon=True)
        tap_thread.start()
        try:
            self.assertTrue(entered.wait(3), "the strip never blocked")
            self.assertEqual(tap_errors, [])
            waiter.join(5)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(results[0]["response"]["choice"], "Approve")
            read = harness.inbox.call_tool("get_response", {"request_id": card["request_id"]})
            self.assertEqual(read["response"]["choice"], "Approve")
            unprocessed = harness.inbox.call_tool("list_unprocessed")
            self.assertEqual(len(unprocessed), 1)
            self.assertIsNone(unprocessed[0]["processed_at"])
            self.assertIsNone(harness.stored(card["request_id"])["processed_at"])
        finally:
            release.set()
            tap_thread.join(5)
            harness.runtime.stop()
            poller.join(5)

        # Safe diagnostics: no token, provider URL, card or answer text.
        secret = "https://api.telegram.org/bot-secret-token/card-private-answer"
        noisy = self.harness(store_path=Path(self._tmpdir.name) / "logs.sqlite")
        noisy_card = noisy.create(external_id="logs", title="card-private-title")
        noisy_message = noisy.stored(noisy_card["request_id"])["telegram_message_id"]
        noisy.api.ack_errors["cb-noisy"] = TelegramAPIError(secret)
        noisy.api.strip_errors[noisy_message] = RuntimeError(secret)
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.tap_card(noisy, noisy_card, callback_id="cb-noisy")
        text = output.getvalue()
        self.assertIn("Telegram acknowledgement failed: TelegramAPIError", text)
        self.assertIn("Telegram strip failed: RuntimeError", text)
        for leaked in ("bot-secret-token", "card-private-title", "card-private-answer", "https://api.telegram.org"):
            self.assertNotIn(leaked, text)

        # The acknowledgement is empty: it never claims the answer was recorded.
        empty = self.harness(store_path=Path(self._tmpdir.name) / "empty.sqlite")
        empty_card = empty.create(external_id="empty-ack")
        self.tap_card(empty, empty_card, callback_id="cb-empty")
        self.assertEqual(empty.api.ack_fields, [{}])
        self.assertEqual(empty.stored(empty_card["request_id"])["state"], "tapped")

        # A healthy refusal explains non-acceptance without touching the answer.
        healthy = self.harness(store_path=Path(self._tmpdir.name) / "healthy.sqlite")
        healthy_card = healthy.create(external_id="healthy-refusal")
        self.tap_card(healthy, healthy_card, callback_id="cb-ok")
        before_choice = healthy.stored(healthy_card["request_id"])["response_choice"]
        sends_before = len(healthy.api.send_calls)
        self.tap_card(healthy, healthy_card, callback_id="cb-refuse")
        refusal = [
            call["text"] for call in healthy.api.send_calls[sends_before:]
        ]
        self.assertTrue(refusal and "Not recorded:" in refusal[0])
        self.assertIn("answered", refusal[0])
        self.assertEqual(
            healthy.stored(healthy_card["request_id"])["response_choice"], before_choice
        )


    # --- U3: safe send-span diagnostics -------------------------------------

    def test_send_span_diagnostic_is_strict_and_fail_closed(self) -> None:
        """`overlap=yes` only from a genuine inside-call span and a claim
        bracket strictly inside it; every other shape stays unproven."""

        class SpanAPI(SessionAPI):
            """A fake transport that brackets its own I/O like the real one."""

            def __init__(self) -> None:
                super().__init__()
                self.gate: tuple[threading.Event, threading.Event] | None = None

            def send_message(
                self, chat_id, text, reply_markup, parse_mode=None, timing=None
            ):
                start = time.monotonic()
                gate = self.gate
                if gate is not None:
                    self.gate = None
                    gate[0].set()
                    gate[1].wait(10)
                try:
                    return super().send_message(chat_id, text, reply_markup, parse_mode)
                finally:
                    if timing is not None:
                        timing[:] = [start, time.monotonic()]

        def send_line(text: str, request_id: str) -> str:
            lines = [
                line
                for line in text.splitlines()
                if line.startswith("Telegram send ") and f" rid={request_id} " in line
            ]
            self.assertEqual(len(lines), 1, f"expected one send line for {request_id}")
            return lines[0]

        def create_in_thread(harness: Harness, result: dict, external_id: str):
            def run() -> None:
                result["card"] = harness.create(external_id=external_id)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            return thread

        def tap_in_thread(harness: Harness, card: dict, callback_id: str) -> None:
            done = threading.Event()
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    self.tap_card(harness, card, callback_id=callback_id)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()

            threading.Thread(target=run, daemon=True).start()
            self.assertTrue(done.wait(3), "the tap waited for the blocked send")
            self.assertEqual(errors, [])

        # Positive: the claim commits strictly inside the blocked send span.
        api = SpanAPI()
        harness = self.harness(api=api)
        tapped = harness.create(external_id="busy-tap", title="private-title")
        gate = (threading.Event(), threading.Event())
        api.gate = gate
        created: dict = {}
        thread = create_in_thread(harness, created, "busy-send")
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertTrue(gate[0].wait(3), "the send never reached its span")
            tap_in_thread(harness, tapped, "cb-inside")
            gate[1].set()
            thread.join(5)
        sent = created["card"]
        line = send_line(output.getvalue(), sent["request_id"])
        self.assertIn("overlap=yes reason=inside_send", line)
        self.assertIn(f"v={sent['version']}", line)
        self.assertIn(f"claim_rid={tapped['request_id']}", line)
        self.assertIn(f"claim_v={tapped['version']}", line)
        self.assertIn(f"pid={os.getpid()}", line)
        for leaked in (TOKEN, "private-title", BEARER):
            self.assertNotIn(leaked, output.getvalue())

        # RED: a tap after the transport return but before message-id settlement.
        late_api = SpanAPI()
        late = self.harness(api=late_api)
        late_tap = late.create(external_id="late-tap")
        original_record = late.inbox.record_telegram_message
        settlement, release = threading.Event(), threading.Event()

        def delayed_record(request_id, **kwargs):
            if not settlement.is_set():
                settlement.set()
                release.wait(5)
            return original_record(request_id, **kwargs)

        late.inbox.record_telegram_message = delayed_record
        late_created: dict = {}
        late_thread = create_in_thread(late, late_created, "late-send")
        late_output = io.StringIO()
        with mock.patch("sys.stdout", late_output):
            self.assertTrue(settlement.wait(3), "settlement was never gated")
            tap_in_thread(late, late_tap, "cb-after-return")
            release.set()
            late_thread.join(5)
        late_sent = late_created["card"]
        late_line = send_line(late_output.getvalue(), late_sent["request_id"])
        self.assertIn("overlap=no reason=claim_after_send", late_line)
        self.assertIn(f"claim_rid={late_tap['request_id']}", late_line)

        # RED: a tap before the transport entry despite the stored reservation.
        class PreStartAPI(SpanAPI):
            def __init__(self) -> None:
                super().__init__()
                self.pre: tuple[threading.Event, threading.Event] | None = None

            def send_message(
                self, chat_id, text, reply_markup, parse_mode=None, timing=None
            ):
                pre = self.pre
                if pre is not None:
                    self.pre = None
                    pre[0].set()
                    pre[1].wait(10)
                return super().send_message(
                    chat_id, text, reply_markup, parse_mode, timing
                )

        early_api = PreStartAPI()
        early = self.harness(api=early_api)
        early_tap = early.create(external_id="early-tap")
        early_gate = (threading.Event(), threading.Event())
        early_api.pre = early_gate
        early_created: dict = {}
        early_thread = create_in_thread(early, early_created, "early-send")
        early_output = io.StringIO()
        with mock.patch("sys.stdout", early_output):
            self.assertTrue(early_gate[0].wait(3), "the send never reached its gate")
            self.assertTrue(early_thread.is_alive(), "the send gate did not hold")
            with closing(sqlite3.connect(early.inbox.store_path)) as conn:
                rows = conn.execute("SELECT payload FROM cards").fetchall()
            reserved = [
                json.loads(row[0])
                for row in rows
                if json.loads(row[0]).get("external_id") == "early-send"
            ][0]
            self.assertTrue(reserved["notified"], "the reservation was not stored")
            self.assertIsNone(reserved["telegram_message_id"])
            tap_in_thread(early, early_tap, "cb-before-start")
            early_gate[1].set()
            early_thread.join(5)
        early_sent = early_created["card"]
        early_line = send_line(early_output.getvalue(), early_sent["request_id"])
        self.assertIn("overlap=unknown reason=no_claim", early_line)

        # Ambiguous: two claims strictly inside one span are never a pass.
        both_api = SpanAPI()
        both = self.harness(api=both_api)
        first = both.create(external_id="both-a")
        second = both.create(external_id="both-b")
        both_gate = (threading.Event(), threading.Event())
        both_api.gate = both_gate
        both_created: dict = {}
        both_thread = create_in_thread(both, both_created, "both-send")
        both_output = io.StringIO()
        with mock.patch("sys.stdout", both_output):
            self.assertTrue(both_gate[0].wait(3), "the send never blocked")
            tap_in_thread(both, first, "cb-both-a")
            tap_in_thread(both, second, "cb-both-b")
            both_gate[1].set()
            both_thread.join(5)
        both_sent = both_created["card"]
        both_line = send_line(both_output.getvalue(), both_sent["request_id"])
        self.assertIn("overlap=unknown reason=ambiguous_claim", both_line)

        # Absent bounds: an uninstrumented transport can never prove overlap.
        plain = self.harness(api=FakeBotAPI())
        plain_output = io.StringIO()
        with mock.patch("sys.stdout", plain_output):
            plain_card = plain.create(external_id="plain")
        plain_line = send_line(plain_output.getvalue(), plain_card["request_id"])
        self.assertIn("overlap=unknown reason=no_bounds", plain_line)

        # The real API samples its span around urlopen, not after the return.
        class StampedResponse(_FakeHTTPResponse):
            def read(self) -> bytes:
                stamps.append(("read", time.monotonic()))
                return super().read()

        api_real = TelegramBotAPI(TOKEN)
        stamps: list[tuple[str, float]] = []

        def fake_urlopen(request, timeout=None):
            stamps.append(("enter", time.monotonic()))
            return StampedResponse(b'{"ok":true,"result":{"message_id":7}}')

        bounds: list[float] = []
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            sent_result = api_real.send_message(OWNER, "hi", None, timing=bounds)
        self.assertEqual(sent_result["message_id"], 7)
        self.assertEqual(len(bounds), 2)
        enter = next(stamp for name, stamp in stamps if name == "enter")
        read = next(stamp for name, stamp in stamps if name == "read")
        self.assertLessEqual(bounds[0], enter)
        self.assertLessEqual(read, bounds[1])

    def test_durable_answer_in_another_process_proves_the_send_span(self) -> None:
        """G6/R2: the answer commits in another process while this process's
        send is in flight; the durable mark names the tapped card."""

        class SpanAPI(SessionAPI):
            def __init__(self) -> None:
                super().__init__()
                self.gate: tuple[threading.Event, threading.Event] | None = None

            def send_message(self, chat_id, text, reply_markup, parse_mode=None, timing=None):
                start = time.monotonic()
                gate = self.gate
                if gate is not None:
                    self.gate = None
                    gate[0].set()
                    gate[1].wait(10)
                try:
                    return super().send_message(chat_id, text, reply_markup, parse_mode)
                finally:
                    if timing is not None:
                        timing[:] = [start, time.monotonic()]

        def answer_in_other_process(harness: Harness, card: dict) -> subprocess.CompletedProcess:
            script = (
                "import os, sys\n"
                "from paraphe.inbox import Inbox\n"
                "store, rid, version, now = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])\n"
                f"inbox = Inbox(owner_telegram_id={OWNER}, default_ttl_seconds=14400, "
                "floor_ttl_seconds=900, store_path=store, clock=lambda: now)\n"
                "inbox.answer(rid, version=version, choice='Approve')\n"
                "inbox.close()\n"
                "print(f'child_pid={os.getpid()}')\n"
            )
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(harness.inbox.store_path),
                    card["request_id"],
                    str(card["version"]),
                    str(self.clock.now),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={
                    **os.environ,
                    "PYTHONPATH": str(SRC),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )

        def send_line(output: str, request_id: str) -> str:
            lines = [
                line
                for line in output.splitlines()
                if line.startswith("Telegram send ") and f" rid={request_id} " in line
            ]
            self.assertEqual(len(lines), 1, f"expected one send line for {request_id}")
            return lines[0]

        def create_in_thread(harness: Harness, result: dict, external_id: str):
            def run() -> None:
                result["card"] = harness.create(external_id=external_id)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            return thread

        # Positive: the other process's durable commit lands inside this
        # process's blocked send span, so the sender can name it.
        api = SpanAPI()
        harness = self.harness(api=api)
        tapped = harness.create(external_id="cross-process-tap")
        gate = (threading.Event(), threading.Event())
        api.gate = gate
        created: dict = {}
        thread = create_in_thread(harness, created, "cross-process-send")
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertTrue(gate[0].wait(3), "the send never reached its span")
            child = answer_in_other_process(harness, tapped)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertRegex(child.stdout, r"child_pid=\d+")
            self.assertNotIn(f"child_pid={os.getpid()}", child.stdout)
            gate[1].set()
            thread.join(5)
        sent = created["card"]
        line = send_line(output.getvalue(), sent["request_id"])
        self.assertIn("overlap=yes reason=inside_send", line)
        self.assertIn(f"claim_rid={tapped['request_id']}", line)
        self.assertIn(f"claim_v={tapped['version']}", line)
        self.assertIn(f"pid={os.getpid()}", line)
        self.assertEqual(harness.stored(tapped["request_id"])["state"], "tapped")

        # RED: the same cross-process answer committed after the transport
        # returned and before settlement is never overlap.
        late_api = SpanAPI()
        late = self.harness(api=late_api)
        late_tap = late.create(external_id="cross-process-late")
        original_record = late.inbox.record_telegram_message
        settlement, release = threading.Event(), threading.Event()

        def delayed_record(request_id, **kwargs):
            if not settlement.is_set():
                settlement.set()
                release.wait(10)
            return original_record(request_id, **kwargs)

        late.inbox.record_telegram_message = delayed_record
        late_created: dict = {}
        late_thread = create_in_thread(late, late_created, "cross-process-late-send")
        late_output = io.StringIO()
        with mock.patch("sys.stdout", late_output):
            self.assertTrue(settlement.wait(3), "settlement was never gated")
            child = answer_in_other_process(late, late_tap)
            self.assertEqual(child.returncode, 0, child.stderr)
            release.set()
            late_thread.join(5)
        late_line = send_line(late_output.getvalue(), late_created["card"]["request_id"])
        self.assertIn("overlap=no reason=claim_after_send", late_line)
        self.assertIn(f"claim_rid={late_tap['request_id']}", late_line)


class SendOverlapBranchCase(unittest.TestCase):
    """The send-span verdict branches, without threads: each shape alone."""

    RID = "send-card"

    def verdict(self, marks, *, version: int = 2) -> str:
        class StubInbox:
            def claims_since(self, moment: float):
                return [mark for mark in marks if mark[1] > moment]

        adapter = tg.TelegramAdapter(
            StubInbox(), object(), owner_id=OWNER, bot_token=TOKEN
        )
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            adapter._log_send_span(
                {"request_id": self.RID, "version": version}, [100.0, 100.5]
            )
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, "expected one send line")
        return lines[0]

    def test_one_claim_inside_the_span_names_the_tapped_card(self) -> None:
        line = self.verdict([(100.1, 100.2, "tapped-card", 4)])
        self.assertIn("overlap=yes reason=inside_send", line)
        self.assertIn("claim_rid=tapped-card claim_v=4", line)

    def test_claim_after_send_is_not_overlap(self) -> None:
        line = self.verdict([(100.6, 100.7, "tapped-card", 1)])
        self.assertIn("overlap=no reason=claim_after_send", line)
        self.assertIn("claim_rid=tapped-card claim_v=1", line)

    def test_claim_version_mismatch_stays_unproven(self) -> None:
        line = self.verdict([(100.1, 100.2, self.RID, 3)])
        self.assertIn("overlap=unknown reason=claim_version_mismatch", line)
        self.assertIn(f"claim_rid={self.RID} claim_v=3", line)

    def test_two_claims_in_one_span_are_ambiguous(self) -> None:
        line = self.verdict([(100.1, 100.2, "first", 1), (100.3, 100.4, "second", 1)])
        self.assertIn("overlap=unknown reason=ambiguous_claim", line)


if __name__ == "__main__":
    unittest.main()
