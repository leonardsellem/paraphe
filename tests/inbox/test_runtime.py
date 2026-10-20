"""Runnable startup and shutdown checks for the private Paraphe process."""

from __future__ import annotations

import io
import json
import os
import selectors
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
            "PARAPHE_OWNER_ANSWER_TOKEN": "test-owner-token",
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
        self._start_lifecycle(runtime)

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

    def _assert_http(self, runtime, request_id=None):
        request = urllib.request.Request(runtime.server.url)
        if request_id is not None:
            request.add_header("Authorization", "Bearer test-mcp-bearer")
            request.add_header("Content-Type", "application/json")
            request.data = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_response", "arguments": {"request_id": request_id}},
            }).encode()
            with urllib.request.urlopen(request, timeout=1) as response:
                body = json.load(response)
            self.assertNotIn("error", body)
            self.assertFalse(body["result"].get("isError", False))
            self.assertEqual(json.loads(body["result"]["content"][0]["text"])["request_id"], request_id)
            return body
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=1)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def _start_lifecycle(self, runtime):
        with mock.patch.object(runtime, "poll_once", side_effect=runtime.stop):
            runtime.run()
        runtime._stopping.clear()

    def _run_one_poll(self, runtime):
        original = runtime.poll_once

        def once():
            try:
                original()
            finally:
                runtime.stop()

        with mock.patch.object(runtime, "poll_once", side_effect=once):
            runtime.run()
        runtime._stopping.clear()

    def test_start_returns_before_event_blocked_telegram(self):
        module = _load_runtime()
        api = FakeTelegramAPI()
        pending, release = threading.Event(), threading.Event()

        def bootstrap(*, offset, timeout):
            pending.set()
            release.wait(2)
            return []

        api.get_updates = bootstrap
        runtimes = []
        with mock.patch.object(module, "TelegramBotAPI", return_value=api):
            starter = threading.Thread(target=lambda: runtimes.append(module.Runtime.start(self.environ)))
            starter.start()
            try:
                starter.join(1)
                self.assertFalse(starter.is_alive(), "local startup waited for Telegram")
                runtime = runtimes[0]
                self.assertFalse(pending.is_set())
                worker = threading.Thread(target=runtime.run)
                worker.start()
                try:
                    self.assertTrue(pending.wait(1))
                    self._assert_http(runtime)
                finally:
                    runtime.stop()
                    release.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
            finally:
                release.set()
                starter.join(2)
                for runtime in runtimes:
                    runtime.close()

    def test_telegram_blackhole_does_not_delay_mcp_ready(self):
        pending, release = threading.Event(), threading.Event()

        class Blackhole(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                pending.set()
                release.wait(2)
                self.close_connection = True

            def log_message(self, format, *args):
                pass

        endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Blackhole)
        endpoint.daemon_threads = True
        thread = threading.Thread(target=endpoint.serve_forever)
        thread.start()
        code = (
            "from paraphe.inbox import runtime as r; "
            "original = r.TelegramBotAPI.__init__; "
            f"r.TelegramBotAPI.__init__ = lambda self, token: (original(self, token), "
            f"setattr(self, '_url', 'http://127.0.0.1:{endpoint.server_port}/')) and None; "
            "r.TELEGRAM_REQUEST_TIMEOUT_SECONDS = 1; r.main()"
        )
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, **self.environ, "PYTHONPATH": str(ROOT / "src"),
                 "PARAPHE_CONFIG_PATH": str(Path(self._tmpdir.name) / "absent.toml")},
        )
        output = b""
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)

        def until(text, deadline):
            nonlocal output
            while text not in output and time.monotonic() < deadline:
                if selector.select(max(0, deadline - time.monotonic())):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    output += chunk
            self.assertIn(text, output)

        try:
            until(b"Paraphe ready:", started + 2)
            self.assertTrue(pending.wait(1), output)
            ready = next(line for line in output.splitlines() if b"Paraphe ready:" in line)
            url = ready.split(b"mcp ")[1].split()[0].decode()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url, timeout=0.5)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            self.assertLess(time.monotonic() - started, 2)
            until(b"bootstrap failed", time.monotonic() + 2)
            self.assertIsNone(process.poll())
        finally:
            release.set()
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            selector.close()
            process.stdout.close()
            endpoint.shutdown()
            endpoint.server_close()
            thread.join(2)

    def test_reconcile_failure_is_logged_and_http_keeps_serving(self):
        module = _load_runtime()
        api = FakeTelegramAPI()
        output = io.StringIO()
        with mock.patch.object(module, "TelegramBotAPI", return_value=api), redirect_stdout(output):
            with mock.patch.object(module.Inbox, "reconcile_notifications", side_effect=RuntimeError(
                "https://example.invalid/bot-test-secret/card-private-content"
            )):
                runtime = module.Runtime.start(self.environ)
                self.addCleanup(runtime.close)
                self._assert_http(runtime)
                self._run_one_poll(runtime)
                self._assert_http(runtime)
        self.assertEqual(api.polls, [(-1, 0), (0, 25)])
        self.assertIn("reconcile started", output.getvalue())
        self.assertRegex(output.getvalue(), r"reconcile failed.*RuntimeError.*elapsed=")
        self.assertNotIn("test-secret", output.getvalue())
        self.assertNotIn("card-private-content", output.getvalue())
        created = runtime.inbox.call_tool("request_approval", {
            "title": "Closed before restart", "external_id": "closed-strip",
        }, bearer="test-mcp-bearer")
        runtime.inbox.record_tap(created["request_id"], choice="Approve")
        runtime.close()
        with mock.patch.object(module, "TelegramBotAPI", return_value=api), redirect_stdout(output):
            restarted = module.Runtime.start(self.environ)
            self.addCleanup(restarted.close)
            with mock.patch.object(api, "edit_message_reply_markup", side_effect=RuntimeError(
                "https://example.invalid/bot-test-secret/card-private-content"
            )):
                self._run_one_poll(restarted)
            self._assert_http(restarted)
        self.assertIn("strip failed: RuntimeError", output.getvalue())
        self.assertIn("reconcile degraded elapsed=", output.getvalue())
        self.assertNotIn("test-secret", output.getvalue())
        self.assertNotIn("card-private-content", output.getvalue())
        self.assertEqual(restarted.inbox.call_tool("get_response", {
            "request_id": created["request_id"],
        })["response"]["choice"], "Approve")

    def test_missing_cursor_bootstrap_preserves_new_card_answer(self):
        module = _load_runtime()
        api = FakeTelegramAPI()
        pending, release = threading.Event(), threading.Event()
        attempts = 0
        runtime = None

        def updates(*, offset, timeout):
            nonlocal attempts
            api.polls.append((offset, timeout))
            if offset == -1:
                attempts += 1
                if attempts == 1:
                    raise module.TelegramAPIError("bootstrap unavailable")
                pending.set()
                release.wait(2)
                return [{"update_id": 41}]
            assert runtime is not None
            runtime.stop()
            return [{"update_id": 42, "message": {
                "from": {"id": 999001}, "chat": {"id": 999001, "type": "private"},
                "reply_to_message": {"message_id": 1}, "text": "Approved",
            }}]

        api.get_updates = updates
        with mock.patch.object(module, "TelegramBotAPI", return_value=api):
            runtime = module.Runtime.start(self.environ)
        self.addCleanup(runtime.close)
        worker = threading.Thread(target=runtime.run)
        worker.start()
        try:
            self.assertTrue(pending.wait(2))
            self._assert_http(runtime)
            with self.assertRaises(module.TelegramAPIError):
                runtime.poll_once()
            with self.assertRaises(module.NotifyRejected):
                runtime.inbox.call_tool("notify_user", {
                    "title": "Bootstrap pending", "external_id": "status-pending",
                }, bearer="test-mcp-bearer")
            self.assertNotIn("status-pending", runtime.inbox._notify_external_ids)
            created = runtime.inbox.call_tool("request_approval", {
                "title": "Deferred decision", "external_id": "bootstrap-deferred",
            }, bearer="test-mcp-bearer")
            self.assertEqual(api.messages, [])
            with runtime.inbox._lock:
                assert runtime.inbox._store is not None
                self.assertIsNone(runtime.inbox._store.load_telegram_next_offset())
                self.assertFalse(runtime.inbox._cards[created["request_id"]].notified)
            self.assertEqual(api.polls, [(-1, 0), (-1, 0)])
        finally:
            release.set()
            worker.join(2)
            runtime.stop()
        self.assertFalse(worker.is_alive())
        answer = runtime.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(answer["response"]["text"], "Approved")
        self.assertEqual(len(api.messages), 1)
        runtime._stopping.clear()
        self._run_one_poll(runtime)
        self.assertEqual(len(api.messages), 1)
        runtime.close()
        with mock.patch.object(module, "TelegramBotAPI", return_value=api):
            restarted = module.Runtime.start(self.environ)
        self.addCleanup(restarted.close)
        self._run_one_poll(restarted)
        self.assertEqual(len(api.messages), 1)
        self.assertEqual([offset for offset, _ in api.polls].count(-1), 2)

    def test_reconcile_stall_keeps_authenticated_reads_responsive(self):
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            runtime = runtime_module.Runtime.start(environ=self.environ)
        self.addCleanup(runtime.close)
        with mock.patch.object(runtime.inbox, "_notify_owner"):
            created = runtime.inbox.call_tool("request_approval", {
                "title": "Pending delivery", "external_id": "reconcile-read",
            }, bearer="test-mcp-bearer")
        pending, release = threading.Event(), threading.Event()
        original = api.send_message

        def send(*args, **kwargs):
            pending.set()
            release.wait(2)
            return original(*args, **kwargs)

        api.send_message = send
        worker = threading.Thread(target=lambda: self._run_one_poll(runtime))
        worker.start()
        try:
            self.assertTrue(pending.wait(1))
            self._assert_http(runtime, created["request_id"])
        finally:
            release.set()
            worker.join(2)
            runtime.stop()
        self.assertFalse(worker.is_alive())

    def test_malformed_update_entries_are_telegram_api_errors(self) -> None:
        runtime_module = _load_runtime()
        api = FakeTelegramAPI()
        with mock.patch.object(runtime_module, "TelegramBotAPI", return_value=api):
            runtime = runtime_module.Runtime.start(environ=self.environ)
        self.addCleanup(runtime.close)
        self._start_lifecycle(runtime)
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

        self._start_lifecycle(runtime)
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

        self._start_lifecycle(runtime)
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
                self._start_lifecycle(runtime)
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
            self._start_lifecycle(first)
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
            failed = runtime_module.Runtime.start(environ=self.environ)
            with mock.patch.object(failed._stopping, "wait", side_effect=lambda _: failed.stop()):
                failed.run()
            with failed.inbox._lock:
                self.assertIsNone(failed._store().load_telegram_next_offset())
            failed.close()
            runtime = runtime_module.Runtime.start(environ=self.environ)
            self.addCleanup(runtime.close)
            self._start_lifecycle(runtime)
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
                    failed = runtime_module.Runtime.start(environ=environ)
                    with mock.patch.object(failed._stopping, "wait", side_effect=lambda _: failed.stop()):
                        failed.run()
                    with failed.inbox._lock:
                        self.assertIsNone(failed._store().load_telegram_next_offset())
                    failed.close()
                    runtime = runtime_module.Runtime.start(environ=environ)
                    self.addCleanup(runtime.close)
                    self._start_lifecycle(runtime)
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
            self._start_lifecycle(runtime)
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
                        self._start_lifecycle(first)
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
            self._start_lifecycle(first)
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
            self._start_lifecycle(first)
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
                self._start_lifecycle(first)
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
    def test_startup_telegram_timeouts_are_explicit(self):
        module = _load_runtime()
        api = module.TelegramBotAPI("test-token")
        body = b'{"ok":true,"result":{"message_id":1}}'
        with mock.patch("urllib.request.urlopen", return_value=_FakeHTTPResponse(body)) as opened:
            api.send_message(1, "hi", None)
            api.edit_message_reply_markup(1, 1, {"inline_keyboard": []})
            with mock.patch.object(_FakeHTTPResponse, "read", return_value=b'{"ok":true,"result":[]}'):
                api.get_updates(offset=-1, timeout=0)
                api.get_updates(offset=0, timeout=25)
        self.assertEqual([call.kwargs["timeout"] for call in opened.call_args_list], [15, 15, 15, 35])

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
