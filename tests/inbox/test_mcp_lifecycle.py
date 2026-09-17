"""Inbox-seam tests for MCP card lifecycle."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TWELVE_TOOLS = [
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
]

BEARER = "test-mcp-bearer"


def _load_inbox():
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from paraphe import inbox as module

    return module


_inbox = _load_inbox()
BindError = _inbox.BindError
Inbox = _inbox.Inbox
InboxError = _inbox.InboxError
NotifyRejected = _inbox.NotifyRejected


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def notify(self, payload: object) -> None:
        self.sent.append(payload)


class FailOnceNotifier:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.attempts = 0

    def notify(self, payload: object) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise NotifyRejected("notify rejected")
        self.sent.append(payload)


class BoomNotifier:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.attempts = 0

    def notify(self, payload: object) -> None:
        self.attempts += 1
        raise RuntimeError("lost response")


class AcceptedThenUnknownNotifier:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.attempts = 0

    def notify(self, payload: object) -> None:
        self.attempts += 1
        self.sent.append(payload)
        raise TimeoutError("lost after accept")


class FailOnRenotify:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def notify(self, payload: object) -> None:
        if isinstance(payload, dict) and payload.get("renotify") is True:
            raise RuntimeError("renotify failed")
        self.sent.append(payload)


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestMcpLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmpdir = Path(self._tmpdir.name)
        self.notifier = FakeNotifier()
        self.clock = FakeClock()
        self.store_path = self.tmpdir / "inbox.sqlite"
        self.inbox = self._new_inbox(notifier=self.notifier)

    def _new_inbox(
        self,
        *,
        notifier: object | None = None,
        clock: object = None,
    ) -> Inbox:
        inbox = Inbox(
            owner_telegram_id=999001,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=self.store_path,
            mcp_create_bearer=BEARER,
            bot_token="test-bot-token",
            notifier=notifier if notifier is not None else FakeNotifier(),
            clock=self.clock if clock is None else clock,
        )
        self.addCleanup(inbox.close)
        return inbox

    def _ask(self, **fields: object) -> dict:
        args = {"question": "Ship the cut?", "external_id": "ext-ask-1", **fields}
        return self.inbox.call_tool("ask_question", args, bearer=BEARER)

    def _approve(self, **fields: object) -> dict:
        args = {"title": "Merge the PR", "external_id": "ext-appr-1", **fields}
        return self.inbox.call_tool("request_approval", args, bearer=BEARER)

    def test_tools_list_returns_exactly_twelve_names(self) -> None:
        names = self.inbox.list_tools()
        self.assertEqual(names, TWELVE_TOOLS)

    def test_duplicate_external_id_returns_same_card_and_does_not_notify_twice(
        self,
    ) -> None:
        first = self._ask()
        second = self._ask()
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(len(self.notifier.sent), 1)

    def test_concurrent_duplicate_external_id_returns_one_card_and_notifies_once(
        self,
    ) -> None:
        def slow_clock() -> float:
            time.sleep(0.05)
            return self.clock()

        self.inbox = self._new_inbox(notifier=self.notifier, clock=slow_clock)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def create() -> dict:
            barrier.wait(timeout=5)
            return self.inbox.call_tool(
                "ask_question",
                {"question": "Ship the cut?", "external_id": "concurrent-dup"},
                bearer=BEARER,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(create) for _ in range(2)]
            results: list[dict] = []
            for fut in futs:
                try:
                    results.append(fut.result(timeout=10))
                except BaseException as exc:
                    errors.append(exc)

        self.assertEqual(errors, [], msg=f"concurrency error escaped: {errors!r}")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["request_id"], results[1]["request_id"])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_question_and_approval_fields_do_not_mix(self) -> None:
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "request_approval",
                {
                    "title": "Merge",
                    "question": "also a question",
                    "external_id": "mix-1",
                },
                bearer=BEARER,
            )
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "ask_question",
                {
                    "question": "Ship?",
                    "details": "approval body",
                    "external_id": "mix-2",
                },
                bearer=BEARER,
            )

    def test_get_response_nested_choice_is_not_consumed(self) -> None:
        created = self._approve()
        self.inbox.record_tap(created["request_id"], choice="Approve")
        first = self.inbox.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        second = self.inbox.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        self.assertEqual(first["response"]["choice"], "Approve")
        self.assertEqual(second["response"]["choice"], "Approve")
        self.assertEqual(first["status"], "answered")
        unprocessed = self.inbox.call_tool("list_unprocessed", {})
        self.assertEqual(len(unprocessed), 1)
        self.assertEqual(unprocessed[0]["response"]["choice"], "Approve")

    def test_update_request_wrong_expected_version_fails(self) -> None:
        created = self._ask()
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "update_request",
                {
                    "request_id": created["request_id"],
                    "expected_version": created["version"] + 1,
                },
            )
        ok = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "choices": ["Yes", "No"],
            },
        )
        self.assertEqual(ok["version"], created["version"] + 1)

    def test_report_execution_unknown_outcome_fails(self) -> None:
        created = self._approve()
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "report_execution",
                {"request_id": created["request_id"], "outcome": "shipped"},
            )
        self.inbox.record_tap(created["request_id"], choice="Approve")
        reported = self.inbox.call_tool(
            "report_execution",
            {"request_id": created["request_id"], "outcome": "accepted"},
        )
        self.assertEqual(reported["execution_status"], "accepted")

    def test_mark_processed_is_idempotent_and_read_still_shows_tap(self) -> None:
        created = self._approve()
        self.inbox.record_tap(created["request_id"], choice="Deny")
        before = self.inbox.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        self.assertEqual(before["response"]["choice"], "Deny")
        self.assertIsNone(before["processed_at"])
        first = self.inbox.call_tool(
            "mark_processed", {"request_id": created["request_id"]}
        )
        second = self.inbox.call_tool(
            "mark_processed", {"request_id": created["request_id"]}
        )
        self.assertEqual(first["processed_at"], second["processed_at"])
        self.assertIsNotNone(first["processed_at"])
        after = self.inbox.call_tool(
            "get_response", {"request_id": created["request_id"]}
        )
        self.assertEqual(after["response"]["choice"], "Deny")

    def test_create_without_bearer_fails(self) -> None:
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "ask_question",
                {"question": "Ship?", "external_id": "no-bearer"},
            )

    def test_rule_key_on_create_is_accepted_and_ignored(self) -> None:
        created = self._approve(rule_key="deploy-staging")
        self.assertTrue(created["request_id"])
        self.assertFalse(created.get("duplicate"))

    def test_cancel_reason_enum(self) -> None:
        created = self._ask(external_id="cancel-1")
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "cancel_request",
                {"request_id": created["request_id"], "reason": "nevermind"},
            )
        cancelled = self.inbox.call_tool(
            "cancel_request",
            {"request_id": created["request_id"], "reason": "resolved_elsewhere"},
        )
        self.assertEqual(cancelled["status"], "cancelled")

    def test_ttl_floor_and_max_and_required_lengths(self) -> None:
        with self.assertRaises(InboxError):
            self._ask(external_id="ttl-60", expires_in_seconds=60)
        ok = self._ask(external_id="ttl-default")
        self.assertTrue(ok["pending"])
        with self.assertRaises(InboxError):
            self._ask(external_id="ttl-high", expires_in_seconds=2_592_001)
        with self.assertRaises(InboxError):
            self._ask(external_id="q-long", question="x" * 201)
        with self.assertRaises(InboxError):
            self._approve(external_id="t-long", title="y" * 201)
        with self.assertRaises(InboxError):
            self._ask(
                external_id="choices-long",
                choices=["a", "b", "c", "d", "e"],
            )
        with self.assertRaises(InboxError):
            self._ask(external_id="choice-item", choices=["z" * 41])
        with self.assertRaises(InboxError):
            self._ask(
                external_id="notes-mismatch",
                choices=["a", "b"],
                choice_notes=["only one"],
            )
        with self.assertRaises(InboxError):
            self._ask(external_id="notes-long", choices=["a"], choice_notes=["n" * 121])
        with self.assertRaises(InboxError):
            self._ask(external_id="notes-no-choices", choice_notes=["n"])
        with self.assertRaises(InboxError):
            self._ask(external_id="runtime-long", runtime="x" * 41)
        with self.assertRaises(InboxError):
            self._approve(external_id="repo-long", repo="y" * 121)
        with self.assertRaises(InboxError):
            self._ask(external_id="worktree-long", worktree="z" * 121)
        with self.assertRaises(InboxError):
            self._ask(external_id="ticket-long", ticket="t" * 201)
        with self.assertRaises(InboxError):
            self._ask(external_id="bad-risk", risk="extreme")
        with self.assertRaises(InboxError):
            self._ask(external_id="bad-pri", priority="immediate")
        with self.assertRaises(InboxError):
            self._ask(external_id="")

    def test_choice_notes_round_trip_and_reset(self) -> None:
        created = self._ask(
            external_id="notes-ask",
            choices=["Approve", "Deny"],
            choice_notes=["do it now", "keep the old revision"],
            recommendation="Approve",
        )
        first = self.notifier.sent[-1]
        assert isinstance(first, dict)
        self.assertEqual(first["choice_notes"], ["do it now", "keep the old revision"])
        updated = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "choices": ["Ship", "Hold"],
                "renotify": True,
            },
        )
        # New choices without notes invalidate the old notes.
        second = self.notifier.sent[-1]
        assert isinstance(second, dict)
        self.assertEqual(second["choice_notes"], [])
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": updated["version"],
                "choice_notes": ["ship it", "wait"],
                "renotify": True,
            },
        )
        third = self.notifier.sent[-1]
        assert isinstance(third, dict)
        self.assertEqual(third["choice_notes"], ["ship it", "wait"])

    def test_provenance_fields_round_trip_on_create_and_update(self) -> None:
        created = self._approve(
            external_id="prov-1",
            runtime="Claude Code",
            repo="paraphe",
            worktree="feature-better-tg-cards",
            ticket="https://tracker.example/card-3119",
        )
        payload = self.notifier.sent[-1]
        assert isinstance(payload, dict)
        self.assertEqual(payload["runtime"], "Claude Code")
        self.assertEqual(payload["repo"], "paraphe")
        self.assertEqual(payload["worktree"], "feature-better-tg-cards")
        self.assertEqual(payload["ticket"], "https://tracker.example/card-3119")
        card = self.inbox._cards[created["request_id"]]
        self.assertEqual(card.runtime, "Claude Code")
        self.assertEqual(card.repo, "paraphe")
        self.assertEqual(card.worktree, "feature-better-tg-cards")
        self.assertEqual(card.ticket, "https://tracker.example/card-3119")

        # Older clients that omit the fields keep working; nothing is invented.
        self._ask(external_id="prov-2")
        bare = self.notifier.sent[-1]
        assert isinstance(bare, dict)
        self.assertIsNone(bare["runtime"])
        self.assertIsNone(bare["repo"])
        self.assertIsNone(bare["worktree"])
        self.assertIsNone(bare["ticket"])

        # update_request can change part of the provenance and keeps the rest.
        updated = self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "worktree": "canonical",
                "renotify": True,
            },
        )
        after = self.notifier.sent[-1]
        assert isinstance(after, dict)
        self.assertEqual(after["worktree"], "canonical")
        self.assertEqual(after["repo"], "paraphe")
        self.assertEqual(after["runtime"], "Claude Code")
        self.assertEqual(updated["version"], created["version"] + 1)

    def test_notify_user_creates_no_card(self) -> None:
        result = self.inbox.call_tool(
            "notify_user",
            {"title": "Status only", "message": "working"},
            bearer=BEARER,
        )
        self.assertEqual(result["kind"], "notify")
        self.assertEqual(self.inbox.call_tool("list_pending", {}), [])
        again = self.inbox.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "notify-1"},
            bearer=BEARER,
        )
        dup = self.inbox.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "notify-1"},
            bearer=BEARER,
        )
        self.assertFalse(again["duplicate"])
        self.assertTrue(dup["duplicate"])
        self.assertEqual(self.inbox.call_tool("list_pending", {}), [])

    def test_how_to_use_and_feedback_names_exist(self) -> None:
        guide = self.inbox.call_tool("how_to_use", {})
        self.assertIn("ask_question", guide)
        created = self.inbox.call_tool(
            "request_feedback",
            {"title": "Draft", "external_id": "fb-1"},
            bearer=BEARER,
        )
        self.assertEqual(created["kind"], "feedback")

    def test_authenticated_http_tools_list_returns_exactly_twelve(self) -> None:
        handle = self.inbox.serve(host="127.0.0.1", port=0)
        self.addCleanup(handle.close)
        body = self._rpc(
            handle.url,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        names = [tool["name"] for tool in body["result"]["tools"]]
        self.assertEqual(names, TWELVE_TOOLS)

    def test_unauthenticated_http_fails(self) -> None:
        handle = self.inbox.serve(host="127.0.0.1", port=0)
        self.addCleanup(handle.close)
        req = urllib.request.Request(
            handle.url,
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_sse_streamable_http_post_and_get(self) -> None:
        handle = self.inbox.serve(host="127.0.0.1", port=0)
        self.addCleanup(handle.close)
        req = urllib.request.Request(
            handle.url,
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {BEARER}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            raw = resp.read().decode("utf-8")
        self.assertIn("event: message", raw)
        data_line = [line for line in raw.splitlines() if line.startswith("data:")][0]
        payload = json.loads(data_line[5:].strip())
        names = [tool["name"] for tool in payload["result"]["tools"]]
        self.assertEqual(names, TWELVE_TOOLS)

        get = urllib.request.Request(
            handle.url,
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {BEARER}",
            },
            method="GET",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(get, timeout=5)
        self.assertEqual(caught.exception.code, 405)
        self.assertNotIn(
            "text/event-stream", caught.exception.headers.get("Content-Type", "")
        )

    def test_public_wildcard_bind_refuses(self) -> None:
        with self.assertRaises(BindError):
            self.inbox.serve(host="0.0.0.0", port=0)
        with self.assertRaises(BindError):
            self.inbox.serve(host="::", port=0)

    def test_card_state_survives_new_inbox_on_same_store(self) -> None:
        created = self._approve(external_id="persist-appr")
        rid = created["request_id"]
        updated = self.inbox.call_tool(
            "update_request",
            {
                "request_id": rid,
                "expected_version": created["version"],
                "details": "more",
            },
        )
        self.inbox.record_tap(rid, choice="Approve")
        self.inbox.call_tool(
            "report_execution",
            {"request_id": rid, "outcome": "accepted"},
        )
        self.inbox.call_tool("mark_processed", {"request_id": rid})
        restarted = self._new_inbox()
        got = restarted.call_tool("get_response", {"request_id": rid})
        self.assertEqual(got["response"]["choice"], "Approve")
        self.assertEqual(got["execution_status"], "accepted")
        self.assertIsNotNone(got["processed_at"])
        self.assertEqual(got["version"], updated["version"])

        question = self._ask(external_id="persist-cancel")
        self.inbox.call_tool(
            "cancel_request",
            {"request_id": question["request_id"], "reason": "cancelled"},
        )
        restarted_cancel = self._new_inbox()
        cancelled = restarted_cancel.call_tool(
            "get_response", {"request_id": question["request_id"]}
        )
        self.assertEqual(cancelled["status"], "cancelled")

    def test_http_tools_list_exposes_inventory_schemas(self) -> None:
        handle = self.inbox.serve(host="127.0.0.1", port=0)
        self.addCleanup(handle.close)
        body = self._rpc(
            handle.url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools = {tool["name"]: tool for tool in body["result"]["tools"]}
        self.assertEqual(list(tools), TWELVE_TOOLS)
        ask = tools["ask_question"]["inputSchema"]
        self.assertEqual(ask["type"], "object")
        self.assertIn("question", ask["required"])
        self.assertEqual(ask["properties"]["question"]["maxLength"], 200)
        self.assertEqual(ask["properties"]["choices"]["maxItems"], 4)
        self.assertEqual(ask["properties"]["choices"]["items"]["maxLength"], 40)
        self.assertEqual(ask["properties"]["choice_notes"]["maxItems"], 4)
        self.assertEqual(ask["properties"]["choice_notes"]["items"]["maxLength"], 120)
        self.assertEqual(ask["properties"]["runtime"]["maxLength"], 40)
        self.assertEqual(ask["properties"]["repo"]["maxLength"], 120)
        self.assertEqual(ask["properties"]["worktree"]["maxLength"], 120)
        self.assertEqual(ask["properties"]["ticket"]["maxLength"], 200)
        self.assertEqual(set(ask["properties"]["risk"]["enum"]), {"low", "medium", "high", "critical"})
        self.assertNotIn("title", ask["properties"])
        approval = tools["request_approval"]["inputSchema"]
        self.assertIn("title", approval["required"])
        self.assertEqual(approval["properties"]["title"]["maxLength"], 200)
        self.assertEqual(approval["properties"]["details"]["maxLength"], 2000)
        self.assertNotIn("question", approval["properties"])
        report = tools["report_execution"]["inputSchema"]
        self.assertEqual(set(report["required"]), {"request_id", "outcome"})
        self.assertEqual(
            set(report["properties"]["outcome"]["enum"]),
            {"accepted", "rejected", "completed", "failed"},
        )
        cancel = tools["cancel_request"]["inputSchema"]
        self.assertEqual(
            set(cancel["properties"]["reason"]["enum"]),
            {"cancelled", "resolved_elsewhere"},
        )
        update = tools["update_request"]["inputSchema"]
        self.assertEqual(set(update["required"]), {"request_id", "expected_version"})
        self.assertEqual(update["properties"]["choice_notes"]["items"]["maxLength"], 120)
        notify = tools["notify_user"]["inputSchema"]
        self.assertIn("title", notify["required"])
        self.assertEqual(notify["properties"]["message"]["maxLength"], 2000)
        # Provenance rides the asking tools and update_request only.
        self.assertNotIn("runtime", notify["properties"])
        self.assertEqual(
            update["properties"]["ticket"]["maxLength"], 200
        )

    def test_report_execution_only_for_tapped_approval(self) -> None:
        question = self._ask(external_id="close-q")
        self.inbox.record_tap(question["request_id"], choice="Yes")
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "report_execution",
                {"request_id": question["request_id"], "outcome": "accepted"},
            )
        approval = self._approve(external_id="close-a")
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "report_execution",
                {"request_id": approval["request_id"], "outcome": "accepted"},
            )
        self.inbox.record_tap(approval["request_id"], choice="Approve")
        reported = self.inbox.call_tool(
            "report_execution",
            {"request_id": approval["request_id"], "outcome": "accepted"},
        )
        self.assertEqual(reported["execution_status"], "accepted")

    def test_mark_processed_only_after_tap(self) -> None:
        question = self._ask(external_id="mark-q")
        with self.assertRaises(InboxError):
            self.inbox.call_tool("mark_processed", {"request_id": question["request_id"]})
        self.inbox.record_tap(question["request_id"], choice="Yes")
        marked = self.inbox.call_tool(
            "mark_processed", {"request_id": question["request_id"]}
        )
        self.assertIsNotNone(marked["processed_at"])
        again = self.inbox.call_tool(
            "mark_processed", {"request_id": question["request_id"]}
        )
        self.assertEqual(marked["processed_at"], again["processed_at"])
        approval = self._approve(external_id="mark-a")
        with self.assertRaises(InboxError):
            self.inbox.call_tool("mark_processed", {"request_id": approval["request_id"]})

    def test_duplicate_external_id_wrong_kind_refuses(self) -> None:
        first = self._ask(external_id="kind-mix")
        with self.assertRaises(InboxError):
            self._approve(external_id="kind-mix")
        self.assertEqual(len(self.notifier.sent), 1)
        same = self._ask(external_id="kind-mix")
        self.assertEqual(same["request_id"], first["request_id"])
        self.assertTrue(same["duplicate"])
        self.assertEqual(same["kind"], "question")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_expiry_moves_open_card_off_pending_and_rejects_tap(self) -> None:
        created = self._ask(external_id="exp-1", expires_in_seconds=900)
        self.clock.now += 901
        pending = self.inbox.call_tool("list_pending", {})
        ids = [card["request_id"] for card in pending]
        self.assertNotIn(created["request_id"], ids)
        got = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(got["status"], "expired")
        with self.assertRaises(InboxError):
            self.inbox.record_tap(created["request_id"], choice="Yes")
        notified = len(self.notifier.sent)
        retry = self._ask(external_id="exp-1", expires_in_seconds=900)
        self.assertEqual(retry["request_id"], created["request_id"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["status"], "expired")
        self.assertFalse(retry["pending"])
        self.assertEqual(len(self.notifier.sent), notified)
        restarted = self._new_inbox()
        again = restarted.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(again["status"], "expired")
        pending_again = restarted.call_tool("list_pending", {})
        self.assertNotIn(created["request_id"], [card["request_id"] for card in pending_again])

    def test_tailnet_v4_and_v6_allowed_and_ipv6_loopback_binds(self) -> None:
        http_mod = sys.modules["paraphe.inbox.http"]
        self.assertTrue(http_mod.bind_host_allowed("100.64.1.2"))
        self.assertTrue(http_mod.bind_host_allowed("fd7a:115c:a1e0::1"))
        self.assertFalse(http_mod.bind_host_allowed("8.8.8.8"))
        self.assertFalse(http_mod.bind_host_allowed("2001:db8::1"))
        handle = self.inbox.serve(host="::1", port=0)
        self.addCleanup(handle.close)
        self.assertGreater(handle.port, 0)

    def test_oversized_content_length_returns_413_without_secrets(self) -> None:
        handle = self.inbox.serve(host="127.0.0.1", port=0)
        self.addCleanup(handle.close)
        conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=2)
        try:
            conn.putrequest("POST", "/mcp")
            conn.putheader("Authorization", f"Bearer {BEARER}")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "1000000")
            conn.endheaders()
            response = conn.getresponse()
            body = response.read().decode("utf-8")
        finally:
            conn.close()
        self.assertEqual(response.status, 413)
        self.assertNotIn(BEARER, body)
        self.assertNotIn("test-bot-token", body)

    def test_unread_rejected_body_on_keepalive_does_not_become_next_request(self) -> None:
        handle = self.inbox.serve(host="127.0.0.1", port=0)
        self.addCleanup(handle.close)
        poison = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode("utf-8")
        follow = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        ).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", handle.port, timeout=2)
        try:
            conn.putrequest("POST", "/mcp")
            conn.putheader("Authorization", "Bearer wrong")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(poison)))
            conn.putheader("Connection", "keep-alive")
            conn.endheaders()
            conn.send(poison)
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 401)
            self.assertEqual((first.getheader("Connection") or "").lower(), "close")
            conn.putrequest("POST", "/mcp")
            conn.putheader("Authorization", f"Bearer {BEARER}")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Accept", "application/json")
            conn.putheader("Content-Length", str(len(follow)))
            conn.endheaders()
            conn.send(follow)
            second = conn.getresponse()
            second_body = json.loads(second.read().decode("utf-8"))
        finally:
            conn.close()
        self.assertEqual(second.status, 200)
        names = [tool["name"] for tool in second_body["result"]["tools"]]
        self.assertEqual(names, TWELVE_TOOLS)

    def test_failed_save_leaves_no_ghost_external_id(self) -> None:
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None
        original = store.save_card

        def boom(payload: dict) -> None:
            raise OSError("disk full")

        store.save_card = boom  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            self._ask(external_id="ghost-save")
        store.save_card = original  # type: ignore[method-assign]
        created = self._ask(external_id="ghost-save")
        self.assertFalse(created.get("duplicate"))
        self.assertNotIn("notified", created)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_fail_once_notify_retries_after_restart_and_keeps_request_id(self) -> None:
        notifier = FailOnceNotifier()
        self.inbox = self._new_inbox(notifier=notifier)
        with self.assertRaises(NotifyRejected):
            self._ask(external_id="notify-once")
        self.assertEqual(notifier.attempts, 1)
        self.assertEqual(len(notifier.sent), 0)
        retry = self._ask(external_id="notify-once")
        self.assertTrue(retry.get("duplicate"))
        self.assertNotIn("notified", retry)
        self.assertEqual(len(notifier.sent), 1)
        rid = retry["request_id"]
        restarted_notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=restarted_notifier)
        again = restarted.call_tool(
            "ask_question",
            {"question": "Ship the cut?", "external_id": "notify-once"},
            bearer=BEARER,
        )
        self.assertEqual(again["request_id"], rid)
        self.assertTrue(again.get("duplicate"))
        self.assertNotIn("notified", again)
        self.assertEqual(len(restarted_notifier.sent), 0)
        envelope = restarted.call_tool("get_response", {"request_id": rid})
        self.assertNotIn("notified", envelope)

    def test_owner_notify_unknown_error_does_not_duplicate_after_restart(self) -> None:
        notifier = BoomNotifier()
        self.inbox = self._new_inbox(notifier=notifier)
        with self.assertRaises(RuntimeError):
            self._ask(external_id="lost-owner")
        self.assertEqual(notifier.attempts, 1)
        self.assertEqual(len(notifier.sent), 0)
        self.inbox.close()
        restarted_notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=restarted_notifier)
        retry = restarted.call_tool(
            "ask_question",
            {"question": "Ship the cut?", "external_id": "lost-owner"},
            bearer=BEARER,
        )
        self.assertTrue(retry.get("duplicate"))
        self.assertEqual(len(restarted_notifier.sent), 0)

    def test_legacy_payload_missing_notified_does_not_renotify_on_duplicate(self) -> None:
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None
        store.save_card(
            {
                "request_id": "legacy-rid",
                "kind": "question",
                "version": 1,
                "external_id": "legacy-ext",
                "state": "open",
                "question": "Ship the cut?",
                "expires_in_seconds": 14400,
            }
        )
        notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=notifier)
        retry = restarted.call_tool(
            "ask_question",
            {"question": "Ship the cut?", "external_id": "legacy-ext"},
            bearer=BEARER,
        )
        self.assertEqual(retry["request_id"], "legacy-rid")
        self.assertTrue(retry.get("duplicate"))
        self.assertEqual(len(notifier.sent), 0)

    def test_corrupt_load_fails_closed_and_does_not_serve_prefix(self) -> None:
        created = self._ask(external_id="prefix-a")
        rid = created["request_id"]
        inbox_mod = sys.modules["paraphe.inbox"]
        original = inbox_mod.Store.load_cards

        def fake_load(self_store: object) -> list[dict]:
            payloads = original(self_store)
            payloads.append({"kind": "approval", "external_id": "prefix-c"})
            return payloads

        inbox_mod.Store.load_cards = fake_load  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(inbox_mod.Store, "load_cards", original))
        broken = self._new_inbox(notifier=FakeNotifier())
        with self.assertRaises(TypeError):
            broken.call_tool("list_pending", {})
        with self.assertRaises(TypeError):
            broken.call_tool("get_response", {"request_id": rid})
        with self.assertRaises(TypeError):
            broken.call_tool(
                "ask_question",
                {"question": "Ship the cut?", "external_id": "prefix-a"},
                bearer=BEARER,
            )

    def test_notify_save_failure_after_send_does_not_duplicate(self) -> None:
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None
        original = store.save_card

        def boom(payload: dict) -> None:
            if payload.get("notified") is True:
                raise OSError("disk full")
            original(payload)

        store.save_card = boom  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            self._ask(external_id="after-accept")
        store.save_card = original  # type: ignore[method-assign]
        retry = self._ask(external_id="after-accept")
        self.assertTrue(retry.get("duplicate"))
        self.assertEqual(len(self.notifier.sent), 1)

    def test_notify_save_failure_after_accept_does_not_duplicate_after_restart(self) -> None:
        notifier = AcceptedThenUnknownNotifier()
        self.inbox = self._new_inbox(notifier=notifier)
        with self.assertRaises(TimeoutError):
            self._ask(external_id="after-accept-restart")
        self.assertEqual(notifier.attempts, 1)
        self.assertEqual(len(notifier.sent), 1)
        self.inbox.close()
        restarted_notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=restarted_notifier)
        retry = restarted.call_tool(
            "ask_question",
            {"question": "Ship the cut?", "external_id": "after-accept-restart"},
            bearer=BEARER,
        )
        self.assertTrue(retry.get("duplicate"))
        self.assertEqual(len(restarted_notifier.sent), 0)
        again = restarted.call_tool(
            "ask_question",
            {"question": "Ship the cut?", "external_id": "after-accept-restart"},
            bearer=BEARER,
        )
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(len(restarted_notifier.sent), 0)

    def test_notify_user_save_failure_after_send_does_not_duplicate(self) -> None:
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None

        def boom(external_id: str) -> None:
            raise OSError("disk full")

        original = store.save_notification_id
        store.save_notification_id = boom  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            self.inbox.call_tool(
                "notify_user",
                {"title": "Status only", "external_id": "status-save"},
                bearer=BEARER,
            )
        store.save_notification_id = original  # type: ignore[method-assign]
        retry = self.inbox.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "status-save"},
            bearer=BEARER,
        )
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertFalse(retry.get("duplicate"))

    def test_notify_user_save_failure_after_accept_does_not_duplicate_after_restart(
        self,
    ) -> None:
        notifier = AcceptedThenUnknownNotifier()
        self.inbox = self._new_inbox(notifier=notifier)
        with self.assertRaises(TimeoutError):
            self.inbox.call_tool(
                "notify_user",
                {"title": "Status only", "external_id": "status-save-restart"},
                bearer=BEARER,
            )
        self.assertEqual(notifier.attempts, 1)
        self.assertEqual(len(notifier.sent), 1)
        self.inbox.close()
        restarted_notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=restarted_notifier)
        retry = restarted.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "status-save-restart"},
            bearer=BEARER,
        )
        self.assertTrue(retry.get("duplicate"))
        self.assertEqual(len(restarted_notifier.sent), 0)
        again = restarted.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "status-save-restart"},
            bearer=BEARER,
        )
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(len(restarted_notifier.sent), 0)

    def test_fail_once_notify_user_retries_and_survives_restart(self) -> None:
        notifier = FailOnceNotifier()
        self.inbox = self._new_inbox(notifier=notifier)
        with self.assertRaises(NotifyRejected):
            self.inbox.call_tool(
                "notify_user",
                {"title": "Status only", "external_id": "status-1"},
                bearer=BEARER,
            )
        self.assertEqual(notifier.attempts, 1)
        self.assertEqual(len(notifier.sent), 0)
        retry = self.inbox.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "status-1"},
            bearer=BEARER,
        )
        self.assertFalse(retry.get("duplicate"))
        self.assertEqual(len(notifier.sent), 1)
        dup = self.inbox.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "status-1"},
            bearer=BEARER,
        )
        self.assertTrue(dup.get("duplicate"))
        self.assertEqual(len(notifier.sent), 1)
        restarted_notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=restarted_notifier)
        again = restarted.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "status-1"},
            bearer=BEARER,
        )
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(len(restarted_notifier.sent), 0)
        self.assertEqual(restarted.call_tool("list_pending", {}), [])

    def test_notify_user_unknown_error_does_not_duplicate_after_restart(self) -> None:
        notifier = BoomNotifier()
        self.inbox = self._new_inbox(notifier=notifier)
        with self.assertRaises(RuntimeError):
            self.inbox.call_tool(
                "notify_user",
                {"title": "Status only", "external_id": "lost-status"},
                bearer=BEARER,
            )
        self.assertEqual(notifier.attempts, 1)
        self.assertEqual(len(notifier.sent), 0)
        self.inbox.close()
        restarted_notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=restarted_notifier)
        retry = restarted.call_tool(
            "notify_user",
            {"title": "Status only", "external_id": "lost-status"},
            bearer=BEARER,
        )
        self.assertTrue(retry.get("duplicate"))
        self.assertEqual(len(restarted_notifier.sent), 0)

    def test_update_renotify_save_failure_keeps_prior_version(self) -> None:
        created = self._approve(external_id="renotify-save")
        rid = created["request_id"]
        version = created["version"]
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None
        original = store.save_card

        def boom(payload: dict) -> None:
            raise OSError("disk full")

        store.save_card = boom  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            self.inbox.call_tool(
                "update_request",
                {
                    "request_id": rid,
                    "expected_version": version,
                    "title": "revised",
                    "renotify": True,
                },
            )
        store.save_card = original  # type: ignore[method-assign]
        got = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(got["version"], version)
        restarted = self._new_inbox()
        again = restarted.call_tool("get_response", {"request_id": rid})
        self.assertEqual(again["version"], version)
        ok = restarted.call_tool(
            "update_request",
            {
                "request_id": rid,
                "expected_version": version,
                "title": "revised",
            },
        )
        self.assertEqual(ok["version"], version + 1)

    def test_update_renotify_notifier_failure_exposes_committed_version(self) -> None:
        notifier = FailOnRenotify()
        self.inbox = self._new_inbox(notifier=notifier)
        created = self._approve(external_id="renotify-n")
        rid = created["request_id"]
        version = created["version"]
        with self.assertRaises(RuntimeError):
            self.inbox.call_tool(
                "update_request",
                {
                    "request_id": rid,
                    "expected_version": version,
                    "title": "revised",
                    "renotify": True,
                },
            )
        got = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(got["version"], version + 1)
        with self.assertRaises(InboxError):
            self.inbox.call_tool(
                "update_request",
                {"request_id": rid, "expected_version": version, "title": "again"},
            )
        restarted_notifier = FakeNotifier()
        restarted = self._new_inbox(notifier=restarted_notifier)
        again = restarted.call_tool("get_response", {"request_id": rid})
        self.assertEqual(again["version"], version + 1)
        ok = restarted.call_tool(
            "update_request",
            {
                "request_id": rid,
                "expected_version": version + 1,
                "renotify": True,
            },
        )
        self.assertEqual(ok["version"], version + 2)
        self.assertEqual(len(restarted_notifier.sent), 1)

    def test_failed_tap_save_keeps_open_in_memory_and_after_restart(self) -> None:
        created = self._approve(external_id="tap-save")
        rid = created["request_id"]
        self.inbox.call_tool("list_pending", {})
        store = self.inbox._store
        assert store is not None
        original = store.save_card

        def boom(payload: dict) -> None:
            raise OSError("disk full")

        store.save_card = boom  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            self.inbox.record_tap(rid, choice="Approve")
        store.save_card = original  # type: ignore[method-assign]
        got = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(got["status"], "pending")
        self.assertIsNone(got["response"])
        restarted = self._new_inbox()
        again = restarted.call_tool("get_response", {"request_id": rid})
        self.assertEqual(again["status"], "pending")
        self.assertIsNone(again["response"])
        restarted.record_tap(rid, choice="Approve")
        tapped = restarted.call_tool("get_response", {"request_id": rid})
        self.assertEqual(tapped["response"]["choice"], "Approve")

    def test_failed_mark_processed_save_keeps_unprocessed(self) -> None:
        created = self._approve(external_id="mark-save")
        rid = created["request_id"]
        self.inbox.record_tap(rid, choice="Approve")
        store = self.inbox._store
        assert store is not None
        original = store.save_card

        def boom(payload: dict) -> None:
            raise OSError("disk full")

        store.save_card = boom  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            self.inbox.call_tool("mark_processed", {"request_id": rid})
        store.save_card = original  # type: ignore[method-assign]
        got = self.inbox.call_tool("get_response", {"request_id": rid})
        self.assertEqual(got["status"], "answered")
        self.assertIsNone(got["processed_at"])
        restarted = self._new_inbox()
        again = restarted.call_tool("get_response", {"request_id": rid})
        self.assertEqual(again["status"], "answered")
        self.assertIsNone(again["processed_at"])

    def _rpc(self, url: str, payload: dict) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {BEARER}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
