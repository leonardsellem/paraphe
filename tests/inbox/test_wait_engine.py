"""The reserved wait surface, made real: park, wake, window end, and the CLI."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe import cli as cli_module
from paraphe import inbox as inbox_module

Inbox = inbox_module.Inbox

BEARER = "test-mcp-bearer"
OWNER_ID = 999001


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def notify(self, payload: object) -> None:
        self.sent.append(payload)


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _InboxHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmpdir = Path(self._tmpdir.name)
        self.notifier = FakeNotifier()
        self.clock = FakeClock()
        self.store_path = self.tmpdir / "inbox.sqlite"
        self.inbox = self._new_inbox()

    def _new_inbox(self) -> Inbox:
        inbox = Inbox(
            owner_telegram_id=OWNER_ID,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=self.store_path,
            mcp_create_bearer=BEARER,
            bot_token="test-bot-token",
            notifier=self.notifier,
            clock=self.clock,
        )
        self.addCleanup(inbox.close)
        return inbox

    def _ask(self, external_id: str, **fields: object) -> dict:
        args = {"question": "Ship the cut?", "external_id": external_id, **fields}
        return self.inbox.call_tool("ask_question", args, bearer=BEARER)

    def _await_card(self, external_id: str, timeout: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.inbox._lock:
                for card in self.inbox._cards.values():
                    if card.external_id == external_id:
                        return card.request_id
            time.sleep(0.005)
        self.fail("the card never appeared")

    def _parked_ask(self, external_id: str, wait_seconds: float) -> tuple[threading.Thread, list, list]:
        results: list[dict] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(self._ask(external_id, wait_seconds=wait_seconds))
            except BaseException as exc:  # pragma: no cover - surfaced by assertions
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, results, errors


class TestWaitEngine(_InboxHarness):
    def test_a_tap_inside_the_window_returns_the_answered_envelope(self) -> None:
        start = time.monotonic()
        thread, results, errors = self._parked_ask("ans-1", wait_seconds=5)
        rid = self._await_card("ans-1")
        self.inbox.claim(rid, version=1, from_id=OWNER_ID, choice="Ship")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        envelope = results[0]
        self.assertEqual(envelope["status"], "answered")
        self.assertEqual(envelope["response"]["choice"], "Ship")
        self.assertFalse(envelope["pending"])
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual(self.inbox._waiters, {})

    def test_the_window_end_returns_the_pending_envelope_and_a_later_read_answers(self) -> None:
        start = time.monotonic()
        envelope = self._ask("win-1", wait_seconds=0.3)
        elapsed = time.monotonic() - start
        self.assertEqual(envelope["status"], "pending")
        self.assertTrue(envelope["pending"])
        self.assertIsNone(envelope["response"])
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 3.0)
        rid = envelope["request_id"]
        self.inbox.claim(rid, version=1, from_id=OWNER_ID, choice="Later")
        read = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(read["response"]["choice"], "Later")
        self.assertEqual(self.inbox._waiters, {})

    def test_a_call_without_wait_seconds_does_not_sleep(self) -> None:
        # ponytail-style regression pin: the reserved window exists, but a call
        # that did not ask for it must return without parking. The absence of a
        # sleep is asserted by construction — the engine's blocking primitives
        # are never reached — not by a wall-clock bound a loaded runner can trip.
        with mock.patch.object(threading.Event, "wait") as event_wait:
            with mock.patch.object(time, "sleep") as time_sleep:
                no_window = self._ask("nosleep-1")
                zero_window = self._ask("nosleep-2", wait_seconds=0)
        self.assertIn("request_id", no_window)
        self.assertIn("request_id", zero_window)
        self.assertEqual(event_wait.call_args_list, [])
        self.assertEqual(time_sleep.call_args_list, [])

    def test_a_waited_get_response_on_an_answered_card_returns_at_once(self) -> None:
        created = self._ask("already-1")
        rid = created["request_id"]
        self.inbox.claim(rid, version=1, from_id=OWNER_ID, choice="Yes")
        start = time.monotonic()
        envelope = self.inbox.call_tool(
            "get_response", {"request_id": rid, "wait_seconds": 5}
        )
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(envelope["status"], "answered")

    def test_the_same_tap_writes_the_same_fields_with_and_without_a_waiter(self) -> None:
        thread, results, errors = self._parked_ask("fields-a", wait_seconds=5)
        rid_a = self._await_card("fields-a")
        self.inbox.claim(rid_a, version=1, from_id=OWNER_ID, choice="Ship")
        thread.join(timeout=5)
        self.assertEqual(errors, [])
        created_b = self._ask("fields-b")
        rid_b = created_b["request_id"]
        self.inbox.claim(rid_b, version=1, from_id=OWNER_ID, choice="Ship")
        a = self.inbox._cards[rid_a]
        b = self.inbox._cards[rid_b]
        for name in ("state", "version", "response_choice", "responded_at", "responded_via"):
            self.assertEqual(getattr(a, name), getattr(b, name), msg=name)

    def test_a_cancel_inside_the_window_wakes_the_waiter_promptly(self) -> None:
        start = time.monotonic()
        thread, results, errors = self._parked_ask("cancel-1", wait_seconds=5)
        rid = self._await_card("cancel-1")
        self.inbox.call_tool("cancel_request", {"request_id": rid})
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual(self.inbox._waiters, {})

    def test_an_expiry_observed_by_a_read_wakes_the_waiter(self) -> None:
        start = time.monotonic()
        thread, results, errors = self._parked_ask("expiry-1", wait_seconds=5)
        rid = self._await_card("expiry-1")
        self.clock.now += 20_000
        read = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(read["status"], "expired")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "expired")
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual(self.inbox._waiters, {})

    def test_an_expiry_nobody_observes_is_reported_at_the_window_end(self) -> None:
        thread, results, errors = self._parked_ask("expiry-2", wait_seconds=0.3)
        rid = self._await_card("expiry-2")
        self.clock.now += 20_000
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "expired")
        self.assertEqual(self.inbox._waiters, {})


class TestWaitHttpSurface(_InboxHarness):
    def setUp(self) -> None:
        super().setUp()
        self.handle = self.inbox.serve("127.0.0.1", 0)
        self.addCleanup(self.handle.close)
        self.handle._server.handle_error = lambda *args, **kwargs: None
        self.url = f"http://127.0.0.1:{self.handle.port}/mcp"

    def _rpc(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {BEARER}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["result"]

    def _call_tool(self, name: str, arguments: dict) -> dict:
        result = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        self.assertNotIn("isError", result, msg=str(result))
        return json.loads(result["content"][0]["text"])

    def _create(self, external_id: str) -> str:
        created = self._call_tool(
            "ask_question", {"question": "Ready?", "external_id": external_id}
        )
        return created["request_id"]

    def _await_parked(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.inbox._waiters:
                return
            time.sleep(0.005)
        self.fail("no waiter registered")

    def test_the_server_serves_other_calls_while_one_call_parks(self) -> None:
        rid = self._create("srv-1")
        parked: list[dict] = []

        def run() -> None:
            parked.append(
                self._call_tool(
                    "get_response", {"request_id": rid, "wait_seconds": 1.0}
                )
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._await_parked()
        start = time.monotonic()
        listing = self._rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertIn("get_response", [tool["name"] for tool in listing["tools"]])
        self.inbox.claim(rid, version=1, from_id=OWNER_ID, choice="Ship")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(parked[0]["status"], "answered")
        self.assertEqual(self.inbox._waiters, {})

    def test_a_disconnected_client_leaves_no_residue_and_the_card_unchanged(self) -> None:
        rid = self._create("drop-1")
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get_response",
                    "arguments": {"request_id": rid, "wait_seconds": 0.6},
                },
            }
        )
        conn = http.client.HTTPConnection("127.0.0.1", self.handle.port, timeout=5)
        conn.request(
            "POST",
            "/mcp",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {BEARER}",
            },
        )
        conn.close()
        self._await_parked()
        # Other traffic still succeeds while the orphan parks.
        self._rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}})
        card = self.inbox._cards[rid]
        self.assertEqual(card.state, "open")
        self.assertEqual(card.version, 1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.inbox._waiters:
            time.sleep(0.01)
        self.assertEqual(self.inbox._waiters, {})


class TestWaitCommand(_InboxHarness):
    def setUp(self) -> None:
        super().setUp()
        self.handle = self.inbox.serve("127.0.0.1", 0)
        self.addCleanup(self.handle.close)
        self.url = f"http://127.0.0.1:{self.handle.port}/mcp"

    def _env(self, **overrides: str) -> mock._patch_dict:
        env = {"PARAPHE_MCP_URL": self.url, "PARAPHE_MCP_CREATE_BEARER": BEARER}
        env.update(overrides)
        return mock.patch.dict(os.environ, env, clear=True)

    def _card(self, external_id: str):
        with self.inbox._lock:
            for card in self.inbox._cards.values():
                if card.external_id == external_id:
                    return card
        self.fail("card not found")

    def test_ask_prints_the_request_id_and_creates_the_card(self) -> None:
        with self._env():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_module.main(
                    [
                        "ask",
                        "Ship the cut?",
                        "--external-id",
                        "cli-1",
                        "--context",
                        "the release train",
                        "--choice",
                        "Ship",
                        "--choice",
                        "Hold",
                    ]
                )
        self.assertEqual(code, 0)
        printed = out.getvalue().strip()
        card = self._card("cli-1")
        self.assertEqual(printed, card.request_id)
        self.assertEqual(card.question, "Ship the cut?")
        self.assertEqual(card.context, "the release train")
        self.assertEqual(card.choices, ["Ship", "Hold"])

    def test_wait_exits_zero_with_the_answer_for_an_answered_card(self) -> None:
        created = self._ask("cli-answered")
        rid = created["request_id"]
        self.inbox.claim(rid, version=1, from_id=OWNER_ID, choice="Approve")
        with self._env():
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_module.main(["wait", rid])
        self.assertEqual(code, 0)
        envelope = json.loads(out.getvalue())
        self.assertEqual(envelope["status"], "answered")
        self.assertEqual(envelope["response"]["choice"], "Approve")
        self.assertEqual(err.getvalue(), "")

    def test_wait_exits_three_for_an_expired_card(self) -> None:
        created = self._ask("cli-expired")
        rid = created["request_id"]
        self.clock.now += 20_000
        with self._env():
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_module.main(["wait", rid])
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("expired", err.getvalue())

    def test_wait_exits_four_for_an_unknown_request_id(self) -> None:
        with self._env():
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_module.main(["wait", "00000000-0000-4000-8000-000000000000"])
        self.assertEqual(code, 4)
        self.assertEqual(out.getvalue(), "")

    def test_wait_loops_over_the_per_call_window_until_the_tap(self) -> None:
        created = self._ask("cli-loop")
        rid = created["request_id"]

        def tap() -> None:
            time.sleep(0.15)
            self.inbox.claim(rid, version=1, from_id=OWNER_ID, choice="Loop")

        thread = threading.Thread(target=tap, daemon=True)
        thread.start()
        out = io.StringIO()
        err = io.StringIO()
        with self._env():
            start = time.monotonic()
            code = cli_module.wait_for_answer(
                rid, url=self.url, window=0.1, out=out, err=err
            )
            elapsed = time.monotonic() - start
        thread.join(timeout=5)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(elapsed, 0.1)
        envelope = json.loads(out.getvalue())
        self.assertEqual(envelope["response"]["choice"], "Loop")

    def test_wait_uses_the_config_file_bearer_when_the_environment_is_unset(self) -> None:
        config = self.tmpdir / "paraphe.toml"
        config.write_text(f'mcp_create_bearer = "{BEARER}"\n', encoding="utf-8")
        with self._env(PARAPHE_CONFIG_PATH=str(config)):
            os.environ.pop("PARAPHE_MCP_CREATE_BEARER", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli_module.main(["ask", "From the config?", "--external-id", "cli-2"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), self._card("cli-2").request_id)

    def test_wait_refuses_without_a_create_bearer(self) -> None:
        created = self._ask("cli-nobearer")
        rid = created["request_id"]
        with self._env(PARAPHE_CONFIG_PATH=str(self.tmpdir / "missing.toml")):
            os.environ.pop("PARAPHE_MCP_CREATE_BEARER", None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_module.main(["wait", rid])
        self.assertEqual(code, 1)
        self.assertIn("no create bearer", err.getvalue())


if __name__ == "__main__":
    unittest.main()
