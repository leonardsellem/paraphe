"""Runnable startup and shutdown checks for the private Paraphe process."""

from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
from email.message import EmailMessage
from enum import IntEnum
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class IntSubclass(int):
    pass


class UpdateIdEnum(IntEnum):
    NINE = 9


def _load_runtime():
    src = ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from paraphe.inbox import runtime as module

    return module


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.polls: list[tuple[int, int]] = []
        self.messages: list[dict] = []

    def get_updates(self, *, offset: int, timeout: int) -> list[dict]:
        self.polls.append((offset, timeout))
        if offset < 0:
            return []
        return [{"update_id": 7, "message": {"text": "ignored"}}]

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None, parse_mode: str | None = None
    ) -> dict:
        message = {
            "message_id": len(self.messages) + 1,
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        }
        self.messages.append(message)
        return message

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> None:
        return None

    def answer_callback_query(self, callback_id: str) -> None:
        return None


class TestRuntime(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.store_path = Path(self._tmpdir.name) / "inbox.sqlite"
        self.environ = {
            "PARAPHE_BOT_TOKEN": "test-bot-token",
            "PARAPHE_OWNER_TELEGRAM_ID": "999001",
            "PARAPHE_MCP_CREATE_BEARER": "test-mcp-bearer",
            "PARAPHE_MCP_PORT": "0",
            "PARAPHE_STORE_PATH": str(self.store_path),
        }

    def test_start_uses_one_store_loopback_and_polls_telegram(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            runtime = runtime_module.Runtime.start(environ=self.environ)
        self.addCleanup(runtime.close)

        self.assertEqual(runtime.server.host, "127.0.0.1")
        self.assertGreater(runtime.server.port, 0)
        self.assertEqual(runtime.inbox.store_path, self.store_path)
        self.assertIs(runtime.inbox._telegram, runtime.telegram)
        self.assertIs(runtime.inbox._notifier, runtime.telegram)
        self.assertIsNotNone(runtime.inbox._store)

        created = runtime.inbox.call_tool(
            "request_approval",
            {
                "title": "Ship the private runtime?",
                "details": "One process, one store.",
                "external_id": "runtime-start",
            },
            bearer="test-mcp-bearer",
        )
        self.assertTrue(created["request_id"])
        self.assertEqual(len(api.messages), 1)
        self.assertIn("One process, one store.", api.messages[0]["text"])

        runtime.poll_once()
        self.assertEqual(api.polls, [(-1, 0), (0, 25)])
        self.assertEqual(runtime.next_update_id, 8)

    def test_malformed_update_entries_are_telegram_api_errors(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            runtime = runtime_module.Runtime.start(environ=self.environ)
        self.addCleanup(runtime.close)
        for bad in (None, "nope", 7, [], {}, {"update_id": "x"}):
            api.get_updates = (  # type: ignore[method-assign]
                lambda *, offset, timeout, bad=bad: [bad]
            )
            with self.assertRaises(runtime_module.TelegramAPIError):
                runtime.poll_once()
            self.assertEqual(runtime.next_update_id, 0)

    def test_run_keeps_alive_on_malformed_update_entries(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            runtime = runtime_module.Runtime.start(environ=self.environ)
        self.addCleanup(runtime.close)

        def get_updates(*, offset: int, timeout: int) -> list:
            return [None]

        api.get_updates = get_updates  # type: ignore[method-assign]
        runtime._stopping.wait = lambda timeout=None: runtime.stop() or True  # type: ignore[method-assign]
        runtime.run()

    def test_run_ignores_malformed_nested_updates_at_adapter_boundary(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            runtime = runtime_module.Runtime.start(environ=self.environ)
        self.addCleanup(runtime.close)
        batches = iter(
            [
                [
                    {"update_id": 10, "callback_query": "bad"},
                    {
                        "update_id": 11,
                        "callback_query": {
                            "id": "cb-11",
                            "from": [],
                            "message": {},
                            "data": "bad",
                        },
                    },
                    {
                        "update_id": 12,
                        "callback_query": {
                            "id": "cb-12",
                            "from": {"id": "bad"},
                            "message": {
                                "chat": {"id": 999001, "type": "private"}
                            },
                            "data": "bad",
                        },
                    },
                    {"update_id": 13, "message": "bad"},
                    {"update_id": 14, "message": {"from": [], "chat": {}}},
                    {
                        "update_id": 15,
                        "message": {
                            "from": {"id": "bad"},
                            "chat": {"id": 999001, "type": "private"},
                        },
                    },
                ],
                [],
            ]
        )

        def get_updates(*, offset: int, timeout: int) -> list:
            batch = next(batches)
            if not batch:
                runtime.stop()
            return batch

        api.get_updates = get_updates  # type: ignore[method-assign]
        runtime.run()
        self.assertEqual(runtime.next_update_id, 16)

    def test_close_stops_http_and_closes_the_store(self) -> None:
        runtime_module = _load_runtime()
        with mock.patch.object(
            runtime_module, "TelegramBotAPI", return_value=FakeTelegramAPI()
        ):
            runtime = runtime_module.Runtime.start(environ=self.environ)
        port = runtime.server.port
        store = runtime.inbox._store
        self.assertIsNotNone(store)

        runtime.close()
        runtime.close()

        self.assertIsNone(store._conn)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)

    def test_first_boot_bootstraps_without_dispatching_queued_updates(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        queued = {
            "update_id": 41,
            "callback_query": {
                "id": "cb-41",
                "from": {"id": 999001},
                "message": {
                    "message_id": 1,
                    "chat": {"id": 999001, "type": "private"},
                },
                "data": "approve",
            },
        }
        handled: list[object] = []
        orig = runtime_module.TelegramAdapter.handle_update

        def get_updates(*, offset: int, timeout: int) -> list[dict]:
            api.polls.append((offset, timeout))
            if offset < 0:
                return [queued]
            return []

        def wrapped(self, update):
            handled.append(update)
            return orig(self, update)

        api.get_updates = get_updates  # type: ignore[method-assign]
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            with mock.patch.object(
                runtime_module.TelegramAdapter, "handle_update", wrapped
            ):
                runtime = runtime_module.Runtime.start(environ=self.environ)
                self.addCleanup(runtime.close)
                runtime.poll_once()
        self.assertEqual(api.polls, [(-1, 0), (42, 25)])
        self.assertEqual(handled, [])
        self.assertEqual(runtime.next_update_id, 42)

    def test_bootstrap_persists_next_offset_across_restart(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()

        def get_updates(*, offset: int, timeout: int) -> list[dict]:
            api.polls.append((offset, timeout))
            if offset < 0:
                return [{"update_id": 41, "message": {"text": "queued"}}]
            return []

        api.get_updates = get_updates  # type: ignore[method-assign]
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            first = runtime_module.Runtime.start(environ=self.environ)
            first.close()
            self.assertEqual(api.polls, [(-1, 0)])
            second = runtime_module.Runtime.start(environ=self.environ)
            self.addCleanup(second.close)
            self.assertEqual(api.polls, [(-1, 0)])
            self.assertEqual(second.next_update_id, 42)
            second.poll_once()
        self.assertEqual(api.polls, [(-1, 0), (42, 25)])

    def test_malformed_bootstrap_persists_no_offset(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()

        def get_updates(*, offset: int, timeout: int) -> list:
            api.polls.append((offset, timeout))
            if len(api.polls) == 1:
                return [{"update_id": "x"}]
            return []

        api.get_updates = get_updates  # type: ignore[method-assign]
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            with self.assertRaises(runtime_module.TelegramAPIError):
                runtime_module.Runtime.start(environ=self.environ)
            runtime = runtime_module.Runtime.start(environ=self.environ)
            self.addCleanup(runtime.close)
        self.assertEqual([offset for offset, _ in api.polls], [-1, -1])
        self.assertEqual(runtime.next_update_id, 0)

    def test_bootstrap_rejects_coerced_update_ids(self) -> None:
        runtime_module = _load_runtime()
        for index, bad in enumerate(
            ("999999999", 8.9, True, -1, IntSubclass(9), UpdateIdEnum.NINE)
        ):
            with self.subTest(update_id=bad):
                store_path = Path(self._tmpdir.name) / f"boot-bad-{index}.sqlite"
                environ = {**self.environ, "PARAPHE_STORE_PATH": str(store_path)}
                api = FakeTelegramAPI()

                def get_updates(*, offset: int, timeout: int, bad=bad) -> list:
                    api.polls.append((offset, timeout))
                    if len(api.polls) == 1:
                        return [{"update_id": bad}]
                    return []

                api.get_updates = get_updates  # type: ignore[method-assign]
                with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
                    with self.assertRaises(runtime_module.TelegramAPIError):
                        runtime_module.Runtime.start(environ=environ)
                    runtime = runtime_module.Runtime.start(environ=environ)
                    self.addCleanup(runtime.close)
                self.assertEqual([offset for offset, _ in api.polls], [-1, -1])
                self.assertEqual(runtime.next_update_id, 0)

        store_path = Path(self._tmpdir.name) / "boot-valid.sqlite"
        environ = {**self.environ, "PARAPHE_STORE_PATH": str(store_path)}
        api = FakeTelegramAPI()

        def get_valid_updates(*, offset: int, timeout: int) -> list[dict]:
            api.polls.append((offset, timeout))
            if offset < 0:
                return [{"update_id": 41, "message": {"text": "queued"}}]
            return []

        api.get_updates = get_valid_updates  # type: ignore[method-assign]
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            runtime = runtime_module.Runtime.start(environ=environ)
            self.addCleanup(runtime.close)
        self.assertEqual(runtime.next_update_id, 42)

    def test_poll_rejects_coerced_update_ids_without_skipping(self) -> None:
        runtime_module = _load_runtime()
        orig = runtime_module.TelegramAdapter.handle_update
        for index, bad in enumerate(
            ("999999999", 8.9, True, -1, IntSubclass(9), UpdateIdEnum.NINE)
        ):
            with self.subTest(update_id=bad):
                store_path = Path(self._tmpdir.name) / f"poll-bad-{index}.sqlite"
                environ = {**self.environ, "PARAPHE_STORE_PATH": str(store_path)}
                api = FakeTelegramAPI()
                handled: list[object] = []
                queue = [
                    {"update_id": 10, "message": {"text": "ok"}},
                    {"update_id": bad, "message": {"text": "bad"}},
                    {"update_id": 12, "message": {"text": "later"}},
                ]

                def get_updates(*, offset: int, timeout: int) -> list:
                    api.polls.append((offset, timeout))
                    if offset < 0:
                        return []
                    if offset == 0:
                        return list(queue)
                    if offset == 11:
                        return list(queue[1:])
                    return []

                def wrapped(self, update):
                    handled.append(update["update_id"])
                    return orig(self, update)

                api.get_updates = get_updates  # type: ignore[method-assign]
                with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
                    with mock.patch.object(
                        runtime_module.TelegramAdapter, "handle_update", wrapped
                    ):
                        first = runtime_module.Runtime.start(environ=environ)
                        with self.assertRaises(runtime_module.TelegramAPIError):
                            first.poll_once()
                        self.assertEqual(handled, [10])
                        self.assertEqual(first.next_update_id, 11)
                        first.close()
                        second = runtime_module.Runtime.start(environ=environ)
                        self.addCleanup(second.close)
                        with self.assertRaises(runtime_module.TelegramAPIError):
                            second.poll_once()
                self.assertEqual(
                    [offset for offset, _ in api.polls],
                    [-1, 0, 11],
                )
                self.assertEqual(handled, [10])
                self.assertEqual(second.next_update_id, 11)

        store_path = Path(self._tmpdir.name) / "poll-valid.sqlite"
        environ = {**self.environ, "PARAPHE_STORE_PATH": str(store_path)}
        api = FakeTelegramAPI()

        def get_valid_updates(*, offset: int, timeout: int) -> list[dict]:
            api.polls.append((offset, timeout))
            if offset < 0:
                return []
            if offset == 0:
                return [{"update_id": 20, "message": {"text": "ok"}}]
            return []

        api.get_updates = get_valid_updates  # type: ignore[method-assign]
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            first = runtime_module.Runtime.start(environ=environ)
            first.poll_once()
            self.assertEqual(first.next_update_id, 21)
            first.close()
            second = runtime_module.Runtime.start(environ=environ)
            self.addCleanup(second.close)
            second.poll_once()
        self.assertEqual([offset for offset, _ in api.polls], [-1, 0, 21])
        self.assertEqual(second.next_update_id, 21)

    def test_poll_persists_each_successful_update_and_resumes_after_last(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        batch = [
            {"update_id": 10, "message": {"text": "a"}},
            {"update_id": 11, "message": {"text": "b"}},
            {"update_id": 12, "message": {"text": "c"}},
        ]

        def get_updates(*, offset: int, timeout: int) -> list[dict]:
            api.polls.append((offset, timeout))
            if offset < 0:
                return []
            if offset == 0:
                return batch
            return []

        api.get_updates = get_updates  # type: ignore[method-assign]
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            first = runtime_module.Runtime.start(environ=self.environ)
            first.poll_once()
            self.assertEqual(first.next_update_id, 13)
            first.close()
            second = runtime_module.Runtime.start(environ=self.environ)
            self.addCleanup(second.close)
            second.poll_once()
        self.assertEqual(api.polls, [(-1, 0), (0, 25), (13, 25)])
        self.assertEqual(second.next_update_id, 13)

    def test_failed_update_does_not_replay_earlier_or_skip_failed_item(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        handled: list[int] = []
        orig = runtime_module.TelegramAdapter.handle_update

        def get_updates(*, offset: int, timeout: int) -> list[dict]:
            api.polls.append((offset, timeout))
            if offset < 0:
                return []
            batch = [
                {"update_id": 10, "message": {"text": "a"}},
                {"update_id": 11, "message": {"text": "b"}},
                {"update_id": 12, "message": {"text": "c"}},
            ]
            return [item for item in batch if item["update_id"] >= offset]

        def wrapped(self, update):
            update_id = update["update_id"]
            handled.append(update_id)
            if update_id == 11:
                raise RuntimeError("fail N")
            return orig(self, update)

        api.get_updates = get_updates  # type: ignore[method-assign]
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            with mock.patch.object(
                runtime_module.TelegramAdapter, "handle_update", wrapped
            ):
                first = runtime_module.Runtime.start(environ=self.environ)
                with self.assertRaisesRegex(RuntimeError, "fail N"):
                    first.poll_once()
                self.assertEqual(handled, [10, 11])
                self.assertEqual(first.next_update_id, 11)
                first.close()
                second = runtime_module.Runtime.start(environ=self.environ)
                self.addCleanup(second.close)
                with self.assertRaisesRegex(RuntimeError, "fail N"):
                    second.poll_once()
        self.assertEqual(
            [offset for offset, _ in api.polls],
            [-1, 0, 11],
        )
        self.assertEqual(handled, [10, 11, 11])
        self.assertEqual(second.next_update_id, 11)


class _FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class TestTelegramBotAPI(unittest.TestCase):
    def _api(self):
        runtime_module = _load_runtime()
        rejected = sys.modules["paraphe.inbox"].NotifyRejected
        return runtime_module, runtime_module.TelegramBotAPI("test-token"), rejected

    def test_ok_false_is_notify_rejected(self) -> None:
        runtime_module, api, rejected = self._api()
        body = json.dumps({"ok": False, "description": "Bad Request"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(body)):
            with self.assertRaises(rejected):
                api.send_message(1, "hi", None)

    def test_http_error_ok_false_is_notify_rejected(self) -> None:
        runtime_module, api, rejected = self._api()
        fp = io.BytesIO(json.dumps({"ok": False, "error_code": 400}).encode())
        err = urllib.error.HTTPError(
            "https://example.invalid/sendMessage",
            400,
            "Bad Request",
            EmailMessage(),
            fp,
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(rejected):
                api.send_message(1, "hi", None)

    def test_timeout_is_ambiguous_telegram_error(self) -> None:
        runtime_module, api, rejected = self._api()
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(runtime_module.TelegramAPIError) as ctx:
                api.send_message(1, "hi", None)
        self.assertNotIsInstance(ctx.exception, rejected)

    def test_invalid_json_is_ambiguous_telegram_error(self) -> None:
        runtime_module, api, rejected = self._api()
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeHTTPResponse(b"not-json")
        ):
            with self.assertRaises(runtime_module.TelegramAPIError) as ctx:
                api.send_message(1, "hi", None)
        self.assertNotIsInstance(ctx.exception, rejected)

    def test_ok_true_returns_result(self) -> None:
        _runtime_module, api, _rejected = self._api()
        body = json.dumps({"ok": True, "result": {"message_id": 9}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(body)):
            sent = api.send_message(1, "hi", None)
        self.assertEqual(sent, {"message_id": 9})

    def test_get_me_returns_the_bot_identity(self) -> None:
        _runtime_module, api, _rejected = self._api()
        body = json.dumps(
            {"ok": True, "result": {"id": 42, "username": "paraphe_test_bot"}}
        ).encode()
        with mock.patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(body)):
            identity = api.get_me()
        self.assertEqual(identity["username"], "paraphe_test_bot")


if __name__ == "__main__":
    unittest.main()
