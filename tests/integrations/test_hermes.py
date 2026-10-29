"""Offline unit fixtures only: no installed Hermes, cards, credentials or network."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock
import uuid

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe.integrations import hermes as plugin

CARD_ID = "12345678-1234-1234-1234-123456789abc"
SNAPSHOTS = {"transport-registration-latest.json", "transport-attempt-latest.json"}
REAL_SPAWN = plugin._spawn_closeout  # the async seam; most tests run the close inline


def fake_atomic_json_write(path, row, *, indent, ensure_ascii, mode, fsync_dir):
    """Fake unit host helper; the separate real-stack exercise proves native I/O."""
    assert (indent, ensure_ascii, mode, fsync_dir) == (2, False, 0o600, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(row, stream, indent=indent, ensure_ascii=ensure_ascii)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class FakeUnitState:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    def get(self, *args):
        raise AssertionError("legacy state must not be read")

    def set(self, *args):
        raise AssertionError("legacy state must not be written")


class FakeUnitContext:
    def __init__(self, data_dir, *, choice="Approve", replies=None):
        self.state = FakeUnitState(data_dir)
        self.choice = choice
        self.replies = replies or {}
        self.calls = []
        self.timeouts = []
        self.hooks = {}

    def register_approval_transport(self, name, callback):
        assert name == "paraphe"
        self.present = callback

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def call_mcp(self, server, tool, arguments, timeout=None):
        assert server == "paraphe"
        self.calls.append((tool, arguments))
        self.timeouts.append(timeout)
        if tool in self.replies:
            reply = self.replies[tool]
            if isinstance(reply, Exception):
                raise reply
            return reply() if callable(reply) else reply
        result = ({"request_id": CARD_ID} if tool == "request_approval" else
                  {"status": "answered", "pending": False, "response": {"choice": self.choice}})
        return {"ok": True, "result": result}


def request(**fields):
    answers = []
    return SimpleNamespace(command="printf example", description="[MEDIUM] command needs consent",
                           pattern_key="example", request_id=uuid.uuid4().hex,
                           digest=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), timeout_seconds=30,
                           respond=lambda choice: answers.append(choice) or choice,
                           answers=answers, **fields)


class TestHermesTransport(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        host = ModuleType("utils")
        setattr(host, "atomic_json_write", fake_atomic_json_write)
        self.host = host
        self._patch(mock.patch.dict(sys.modules, {"utils": host}))
        self._patch(mock.patch.object(plugin, "_work_context", return_value={}))
        self._patch(mock.patch.object(plugin, "_origin", return_value="example session"))
        self._patch(mock.patch.object(plugin, "_CTX", None))
        # Inline by default: the close is a side effect, not the thing under test here.
        self.spawn = self._patch(mock.patch.object(
            plugin, "_spawn_closeout", side_effect=plugin._close_card))
        old = dict(plugin._WARNING_STATE)
        self.addCleanup(lambda: plugin._WARNING_STATE.update(old))
        plugin._WARNING_STATE.update(lock=threading.Lock(), pid=None)

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def context(self, **kwargs):
        ctx = FakeUnitContext(self.root, **kwargs)
        plugin.register(ctx)
        return ctx

    def row(self, kind="attempt"):
        return json.loads((self.root / f"transport-{kind}-latest.json").read_text(encoding="utf-8"))

    def test_registration_fingerprint_hook_and_context(self):
        ctx = self.context()
        row = self.row("registration")
        self.assertEqual(set(row), {"schema", "pid", "registration_id", "phase", "code",
                                    "file_at_registration_sha256"})
        self.assertEqual((row["schema"], row["pid"], row["phase"], row["code"]),
                         (1, os.getpid(), "registration", "registered"))
        self.assertRegex(row["registration_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(row["file_at_registration_sha256"], hashlib.sha256(Path(plugin.__file__).read_bytes()).hexdigest())
        self.assertIs(ctx.hooks["post_approval_response"], plugin.on_post_approval_response)

    def test_approve_and_deny_are_the_only_owner_decisions(self):
        for choice, answer, code in (("Approve", "once", "owner_approve"), ("Deny", "deny", "owner_deny")):
            with self.subTest(choice=choice):
                ctx = self.context(choice=choice)
                req = request()
                self.assertEqual(ctx.present(req), answer)
                self.assertEqual(req.answers, [answer])
                self.assertEqual(self.row()["code"], code)
                # The closeout calls follow the decision, never precede it.
                self.assertEqual([name for name, _ in ctx.calls],
                                 ["request_approval", "get_response", "report_execution", "mark_processed"])
                self.assertEqual(ctx.calls[1][1], {"request_id": CARD_ID})

    def test_approve_reports_accepted_then_marks_processed(self):
        ctx = self.context()
        self.assertEqual(ctx.present(request()), "once")
        self.assertEqual(ctx.calls[2:], [("report_execution", {"request_id": CARD_ID, "outcome": "accepted"}),
                                        ("mark_processed", {"request_id": CARD_ID})])
        self.assertEqual(ctx.timeouts[2:], [1.0, 1.0])

    def test_deny_reports_rejected_then_marks_processed(self):
        ctx = self.context(choice="Deny")
        self.assertEqual(ctx.present(request()), "deny")
        self.assertEqual(ctx.calls[2:], [("report_execution", {"request_id": CARD_ID, "outcome": "rejected"}),
                                        ("mark_processed", {"request_id": CARD_ID})])

    def test_closeout_failure_keeps_the_owner_decision(self):
        for reply in (ConnectionError("sensitive-example-marker"), {"ok": False, "error": "sensitive-example-marker"}):
            with self.subTest(failure=type(reply).__name__):
                ctx = self.context(replies={"report_execution": reply})
                req = request()
                with self.assertLogs(plugin.logger, "WARNING") as logs:
                    self.assertEqual(ctx.present(req), "once")
                self.assertEqual(req.answers, ["once"])
                self.assertIn("report_execution", logs.output[0])
                self.assertNotIn("sensitive-example-marker", "".join(logs.output))
        # A closeout thread that cannot start is a closeout failure too.
        self.spawn.side_effect = REAL_SPAWN
        ctx = self.context()
        with mock.patch.object(plugin.threading, "Thread", side_effect=RuntimeError("can't start new thread")), \
                self.assertLogs(plugin.logger, "WARNING") as logs:
            self.assertEqual(ctx.present(request()), "once")
        self.assertIn("closeout not started", logs.output[0])

    def test_failed_report_still_marks_processed(self):
        for reply in (ConnectionError("sensitive-example-marker"), {"ok": False, "error": "sensitive-example-marker"},
                      {"ok": True, "result": "not-json-sensitive-example-marker"}):
            with self.subTest(failure=type(reply).__name__), self.assertLogs(plugin.logger, "WARNING"):
                ctx = self.context(replies={"report_execution": reply})
                self.assertEqual(ctx.present(request()), "once")
                self.assertEqual(ctx.calls[-2:], [("report_execution", {"request_id": CARD_ID, "outcome": "accepted"}),
                                                  ("mark_processed", {"request_id": CARD_ID})])

    def test_decision_returns_before_closeout_completes(self):
        self.spawn.side_effect = REAL_SPAWN
        entered, release = threading.Event(), threading.Event()
        ctx = self.context()
        def call_mcp(server, tool, arguments, timeout=None):
            ctx.calls.append((tool, arguments))
            ctx.timeouts.append(timeout)
            if tool == "report_execution":
                entered.set()
                self.assertTrue(release.wait(5))
            result = ({"status": "answered", "pending": False, "response": {"choice": "Approve"}}
                      if tool == "get_response" else {"request_id": CARD_ID})
            return {"ok": True, "result": result}
        ctx.call_mcp = call_mcp
        seen = []
        req = request()
        answer = req.respond
        req.respond = lambda choice: seen.append([name for name, _ in ctx.calls]) or answer(choice)
        try:
            self.assertEqual(ctx.present(req), "once")
            self.assertEqual(seen, [["request_approval", "get_response"]])
            self.assertTrue(entered.wait(5))
            self.assertEqual([name for name, _ in ctx.calls][-1], "report_execution")
        finally:
            release.set()
        deadline = time.monotonic() + 5
        while "mark_processed" not in [name for name, _ in ctx.calls] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([name for name, _ in ctx.calls][-2:], ["report_execution", "mark_processed"])

    def test_timeout_cancel_expiry_and_invalid_never_close(self):
        cases = (({"get_response": {"ok": True, "result": {"status": "cancelled"}}}, "card_cancelled"),
                 ({"get_response": {"ok": True, "result": {"status": "expired"}}}, "card_expired"),
                 ({"get_response": {"ok": True, "result": {"pending": False, "response": {"choice": "Always"}}}},
                  "invalid_response"),
                 ({"get_response": {"ok": True, "result": {"pending": True}}}, "timeout"))
        for replies, error in cases:
            with self.subTest(error=error), self.assertLogs(plugin.logger, "WARNING"):
                ctx = self.context(replies=replies)
                with self.assertRaisesRegex(RuntimeError, f"^{error}$"):
                    if error == "timeout":
                        ctx.present(request(), clock=iter((0, 31)).__next__, sleep=lambda _: None)
                    else:
                        ctx.present(request())
                self.assertEqual([name for name, _ in ctx.calls if name in ("report_execution", "mark_processed")], [])

    def test_invalid_owner_answers_fail_without_responding(self):
        for response in (None, "Approve", {}, {"choice": "approve"}, {"choice": "Always"}, {"text": "Approve"}):
            with self.subTest(response=response), self.assertLogs(plugin.logger, "WARNING"):
                ctx = self.context(replies={"get_response": {"ok": True, "result": {"pending": False, "response": response}}})
                req = request()
                with self.assertRaisesRegex(RuntimeError, "^invalid_response$"):
                    ctx.present(req)
                self.assertEqual(req.answers, [])

    def test_cancelled_and_expired_are_not_owner_denials(self):
        for status in ("cancelled", "expired"):
            with self.subTest(status=status), self.assertLogs(plugin.logger, "WARNING"):
                ctx = self.context(replies={"get_response": {"ok": True, "result": {"status": status}}})
                req = request()
                with self.assertRaisesRegex(RuntimeError, f"^card_{status}$"):
                    ctx.present(req)
                self.assertEqual(req.answers, [])
                self.assertEqual(self.row()["code"], f"card_{status}")

    def test_timeout_cancels_even_when_cancellation_fails(self):
        for cancel in ({"ok": True, "result": {}}, ConnectionError("example detail")):
            with self.subTest(cancel=type(cancel).__name__), self.assertLogs(plugin.logger, "WARNING"):
                ctx = self.context(replies={"get_response": {"ok": True, "result": {"pending": True}}, "cancel_request": cancel})
                req = request()
                with self.assertRaisesRegex(RuntimeError, "^timeout$"):
                    ctx.present(req, clock=iter((0, 31)).__next__, sleep=lambda _: self.fail("no sleep past deadline"))
                self.assertEqual(req.answers, [])
                self.assertEqual(ctx.calls[-1], ("cancel_request", {"request_id": CARD_ID, "reason": "resolved_elsewhere"}))
                self.assertEqual(self.row()["code"], "timeout")

    def test_pending_poll_respects_host_deadline(self):
        replies = iter(({"pending": True}, {"pending": False, "response": {"choice": "Approve"}}))
        ctx = self.context(replies={"get_response": lambda: {"ok": True, "result": next(replies)}})
        sleeps = []
        self.assertEqual(ctx.present(request(), clock=iter((0, 1)).__next__, sleep=sleeps.append), "once")
        self.assertEqual(sleeps, [3.0])

    def test_missing_id_no_context_and_oversize_never_respond(self):
        ctx = self.context(replies={"request_approval": {"ok": True, "result": {}}})
        for failure in ("no_request_id", "too_long", "no_context"):
            with self.subTest(failure=failure), self.assertLogs(plugin.logger, "WARNING"):
                req = request()
                if failure == "too_long":
                    req.command = "x" * 2001
                with self.assertRaisesRegex(RuntimeError, f"^{failure}$"):
                    if failure == "no_context":
                        with mock.patch.object(plugin, "_CTX", None):
                            plugin.present(req)
                    else:
                        ctx.present(req)
                self.assertEqual(req.answers, [])
        self.assertEqual(len(ctx.calls), 1)

    def test_technical_faults_preserve_stage_sanitization_and_no_retry(self):
        cases = ((PermissionError("sensitive-example-marker"), "client_exception", "PermissionError"),
                 (ConnectionError("sensitive-example-marker"), "client_exception", "ConnectionError"),
                 ({"ok": False, "error": "sensitive-example-marker"}, "mcp_error", "RuntimeError"),
                 ({"ok": True, "result": "not-json-sensitive-example-marker"}, "unparsable_result", "RuntimeError"),
                 ({"ok": True, "result": []}, "unparsable_result", "RuntimeError"),
                 (None, "mcp_error", "RuntimeError"))
        for stage in ("request_approval", "get_response"):
            for reply, code, category in cases:
                with self.subTest(stage=stage, code=code, category=category), self.assertLogs(plugin.logger, "WARNING") as logs:
                    ctx = self.context(replies={stage: reply})
                    req = request()
                    req.command = req.description = "sensitive-example-marker"
                    with self.assertRaisesRegex(RuntimeError, f"^mcp_error:{category}$") as error:
                        ctx.present(req)
                    self.assertEqual(req.answers, [])
                    row = self.row()
                    self.assertEqual((row["phase"], row["code"], row["exception_type"]), (stage, code, category))
                    self.assertEqual(sum(name == stage for name, _ in ctx.calls), 1)
                    self.assertNotIn("sensitive-example-marker", json.dumps(row) + str(error.exception) + "".join(logs.output))

    def test_json_string_results_duplicate_id_and_expiry_bounds(self):
        ctx = self.context(replies={"request_approval": {"ok": True, "result": json.dumps({"request_id": CARD_ID, "duplicate": True})}})
        for timeout, expiry in ((0, 900), (30, 900), (1000, 1060), (3000000, 2592000)):
            with self.subTest(timeout=timeout):
                req = request()
                req.timeout_seconds = timeout
                self.assertEqual(ctx.present(req), "once")
                card = [arguments for name, arguments in ctx.calls if name == "request_approval"][-1]
                self.assertEqual(card["expires_in_seconds"], expiry)
                self.assertEqual(req.timeout_seconds, timeout)
                self.assertEqual(card["external_id"], f"hermes-approval-{req.digest[:24]}")
                self.assertLessEqual(len(card["details"]), 2000)
                self.assertEqual(self.row()["paraphe_request_id"], CARD_ID)

    def test_diagnostic_fields_are_fixed_and_identity_validated(self):
        ctx = self.context(replies={"request_approval": {"ok": True, "result": {"request_id": "sensitive-example-marker"}}})
        req = request()
        req.command = req.description = req.pattern_key = req.request_id = req.digest = "sensitive-example-marker"
        self.assertEqual(ctx.present(req), "once")
        row = self.row()
        self.assertEqual(set(row), {"schema", "pid", "registration_id", "file_at_registration_sha256", "phase", "code"})
        self.assertNotIn("sensitive-example-marker", json.dumps(row))

    def test_snapshot_count_modes_and_repeated_replacement(self):
        for _ in range(40):
            ctx = self.context()
            req = request()
            self.assertEqual(ctx.present(req), "once")
            self.assertEqual(self.row()["host_request_id"], req.request_id)
        files = list(self.root.iterdir())
        self.assertEqual({p.name for p in files}, SNAPSHOTS)
        self.assertLessEqual(sum(p.stat().st_size for p in files), 4096)
        for path in files:
            self.assertLessEqual(path.stat().st_size, 2048)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_utf8_byte_cap_rejects_without_replacing_or_disabling_retry(self):
        ctx = self.context()
        for kind in ("registration", "attempt"):
            plugin._store_diagnostic(ctx, kind, {"example": "é" * 1000})
            path = self.root / f"transport-{kind}-latest.json"
            before = path.read_bytes()
            self.assertLessEqual(len(before), 2048)
            with self.assertLogs(plugin._DIAGNOSTIC_LOGGER, "WARNING") if kind == "registration" else self.assertNoLogs(plugin._DIAGNOSTIC_LOGGER, "WARNING"):
                plugin._store_diagnostic(ctx, kind, {"example": "é" * 1100})
            self.assertEqual(path.read_bytes(), before)
            plugin._store_diagnostic(ctx, kind, {"example": "fresh"})
            self.assertEqual(json.loads(path.read_text()), {"example": "fresh"})

    def test_legacy_full_malformed_and_unrelated_files_are_untouched(self):
        path = self.root / "state.json"
        for content in (b'{"legacy":"' + b'x' * 10485760 + b'"}', b'{malformed'):
            with self.subTest(size=len(content)):
                path.write_bytes(content)
                unrelated = self.root / "unrelated.log"
                unrelated.write_bytes(b"example unrelated data")
                ctx = self.context()
                self.assertEqual(ctx.present(request()), "once")
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(unrelated.read_bytes(), b"example unrelated data")
                self.assertEqual({p.name for p in self.root.iterdir()}, SNAPSHOTS | {"state.json", "unrelated.log"})

    def test_unknown_kinds_phases_codes_and_exception_names_cannot_escape(self):
        ctx = self.context()
        with self.assertLogs(plugin._DIAGNOSTIC_LOGGER, "WARNING"):
            plugin._store_diagnostic(ctx, "../escape", {})
        self.assertEqual({p.name for p in self.root.iterdir()}, {"transport-registration-latest.json"})
        token = plugin._ACTIVE_DIAGNOSTIC.set((ctx, "attempt", {}))
        try:
            plugin._persist("untrusted", "started")
            plugin._persist("present", "untrusted")
            self.assertFalse((self.root / "transport-attempt-latest.json").exists())
            plugin._persist("present", "started", exception_type="untrusted", paraphe_request_id="untrusted")
            self.assertEqual(self.row(), {"phase": "present", "code": "started"})
            plugin._persist("present", "client_exception", exception_type="OSError")
            self.assertEqual(self.row()["exception_type"], "OSError")
            plugin._persist("present", "owner_approve")
            self.assertNotIn("exception_type", self.row())
        finally:
            plugin._ACTIVE_DIAGNOSTIC.reset(token)

    def test_failed_persistence_warns_once_across_threads_and_retries(self):
        with mock.patch.object(self.host, "atomic_json_write", side_effect=OSError("sensitive-example-marker")) as writer:
            with self.assertLogs(plugin._DIAGNOSTIC_LOGGER, "WARNING") as logs:
                ctx = self.context()
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda _: plugin._store_diagnostic(ctx, "attempt", {}), range(32)))
                self.assertEqual(ctx.present(request()), "once")
                self.assertEqual(self.context(choice="Deny").present(request()), "deny")
            self.assertEqual(len(logs.output), 1)
            self.assertNotIn("sensitive-example-marker", logs.output[0])
            self.assertGreater(writer.call_count, 32)
        self.assertEqual(ctx.present(request()), "once")
        self.assertEqual(self.row()["code"], "owner_approve")

    def test_warning_budget_shared_across_import_names(self):
        spec = importlib.util.spec_from_file_location("example_plugin_alias", plugin.__file__)
        assert spec is not None and spec.loader is not None
        alias = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alias)
        self.assertIs(alias._WARNING_STATE, plugin._WARNING_STATE)
        ctx = self.context()
        with mock.patch.object(self.host, "atomic_json_write", side_effect=OSError("example")):
            with self.assertLogs(plugin._DIAGNOSTIC_LOGGER, "WARNING") as logs:
                plugin._store_diagnostic(ctx, "attempt", {})
                alias._store_diagnostic(ctx, "attempt", {})
            self.assertEqual(len(logs.output), 1)

    @unittest.skipUnless(hasattr(os, "fork") and hasattr(os, "register_at_fork"), "requires POSIX fork")
    def test_fork_resets_inherited_lock_and_has_independent_warning_budget(self):
        ctx = self.context()
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        with mock.patch.object(self.host, "atomic_json_write", side_effect=OSError("example")):
            with self.assertLogs(plugin._DIAGNOSTIC_LOGGER, "WARNING") as logs:
                plugin._store_diagnostic(ctx, "attempt", {})
                lock = plugin._WARNING_STATE["lock"]
                lock.acquire()
                try:
                    pid = os.fork()
                    if pid == 0:
                        try:
                            os.close(read_fd)
                            plugin._store_diagnostic(ctx, "attempt", {})
                            plugin._store_diagnostic(ctx, "attempt", {})
                            os.write(write_fd, str(len(logs.output)).encode())
                            os._exit(0)
                        except BaseException:
                            os._exit(1)
                finally:
                    lock.release()
                os.close(write_fd)
                try:
                    ready, _, _ = select.select([read_fd], [], [], 5)
                    self.assertTrue(ready, "child inherited a locked warning budget")
                    self.assertEqual(os.read(read_fd, 100), b"2")
                finally:
                    waited, status = os.waitpid(pid, os.WNOHANG)
                    if not waited:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                plugin._store_diagnostic(ctx, "attempt", {})
                self.assertEqual(len(logs.output), 1)

    def test_concurrent_rows_keep_their_own_registration_and_ids(self):
        contexts, expected, rows = [], {}, []
        gate = threading.Barrier(16)
        lock = threading.Lock()
        for index in range(16):
            ctx = self.context(choice="Approve" if index % 2 == 0 else "Deny")
            registration = self.row("registration")
            req = request()
            expected[registration["registration_id"]] = req
            contexts.append((ctx, req))
        def capture(path, row, **kwargs):
            fake_atomic_json_write(path, row, **kwargs)
            if path.name == "transport-attempt-latest.json":
                with lock:
                    rows.append(dict(row))
        def run(pair):
            ctx, req = pair
            gate.wait(timeout=5)
            return ctx.present(req)
        with mock.patch.object(self.host, "atomic_json_write", side_effect=capture):
            with ThreadPoolExecutor(max_workers=16) as pool:
                answers = list(pool.map(run, contexts))
        self.assertEqual(answers, ["once" if i % 2 == 0 else "deny" for i in range(16)])
        self.assertEqual(len([r for r in rows if r["code"] in {"owner_approve", "owner_deny"}]), 16)
        for row in rows:
            req = expected[row["registration_id"]]
            self.assertEqual((row["host_request_id"], row["host_digest"], row["pid"]), (req.request_id, req.digest, os.getpid()))
            self.assertEqual(row["file_at_registration_sha256"], hashlib.sha256(Path(plugin.__file__).read_bytes()).hexdigest())
        self.assertIn(self.row(), rows)
        self.assertEqual({p.name for p in self.root.iterdir()}, SNAPSHOTS)

    def test_latest_means_last_replace_not_latest_started_attempt(self):
        ctx = self.context()
        entered, release = threading.Event(), threading.Event()
        old, new = request(), request()
        def hold(path, row, **kwargs):
            if row.get("host_request_id") == old.request_id and row.get("code") == "owner_approve":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test writer not released")
            fake_atomic_json_write(path, row, **kwargs)
        with mock.patch.object(self.host, "atomic_json_write", side_effect=hold):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(ctx.present, old)
                try:
                    self.assertTrue(entered.wait(5))
                    ctx.present(new)
                    self.assertEqual(self.row()["host_request_id"], new.request_id)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=5), "once")
        self.assertEqual(self.row()["host_request_id"], old.request_id)
        self.assertIsNone(plugin._ACTIVE_DIAGNOSTIC.get())

    def test_missing_optional_hook_does_not_disable_transport(self):
        ctx = FakeUnitContext(self.root)
        ctx.register_hook = mock.Mock(side_effect=AttributeError("example older host"))
        with self.assertLogs(plugin.logger, "WARNING"):
            plugin.register(ctx)
        self.assertEqual(ctx.present(request()), "once")

    def test_smart_admission_behavior_is_separate_and_best_effort(self):
        ledger = self.root / "example-receipts.jsonl"
        with mock.patch.dict(os.environ, {"ESCALATION_BAND_RECEIPTS": str(ledger)}):
            plugin.on_post_approval_response(choice="once", command="example")
            self.assertFalse(ledger.exists())
            plugin.on_post_approval_response(choice="smart_approve", command="é" * 250 + "\nsecond", pattern_key="example", session_key="example-session")
            row = json.loads(ledger.read_text())
            self.assertEqual((row["phase"], row["verdict"], row["command_head"]), ("smart", "act", "é" * 200))
            with mock.patch("builtins.open", side_effect=OSError("sensitive-example-marker")), self.assertLogs(plugin.logger, "WARNING") as logs:
                plugin.on_post_approval_response(choice="smart_approve")
            self.assertNotIn("sensitive-example-marker", "".join(logs.output))

    def test_risk_card_caps_and_work_context_helpers(self):
        for command, description, risk in (("printf product", "[MEDIUM] example", "medium"),
                                            ("printf hermes", "[HIGH] example", "high"),
                                            ("rm example", "[LOW] example", "high"),
                                            ("deploy example", "example", "high")):
            self.assertEqual(plugin._risk("example", command, description), risk)
        with mock.patch.object(plugin, "_work_context", return_value={"repo": "r" * 200, "worktree": "w" * 200, "ticket": "t" * 250}):
            ctx = self.context()
            req = request()
            req.command = "x" * 2000
            self.assertEqual(ctx.present(req), "once")
            card = ctx.calls[0][1]
            self.assertLessEqual(len(card["title"]), 200)
            self.assertLessEqual(len(card["details"]), 2000)
            self.assertEqual(tuple(len(card[key]) for key in ("repo", "worktree", "ticket")), (120, 120, 200))
        self.assertEqual(plugin._normalize_remote("ssh://git.example.org/example/project.git"), "example/project")
        self.assertEqual(plugin._normalize_remote("https://example.org/example/project.git"), "example/project")

    def test_normal_imports_and_entrypoint_import_need_no_hermes(self):
        script = '''
import importlib.abc, importlib.metadata, sys
sys.path.insert(0, sys.argv[1])
class NoHost(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'hermes_cli', 'hermes_constants', 'utils'}:
            raise AssertionError('host import: ' + fullname)
sys.meta_path.insert(0, NoHost())
import paraphe.__main__, paraphe.cli, paraphe.inbox.runtime, paraphe.inbox.http
assert 'paraphe.integrations.hermes' not in sys.modules
entry = importlib.metadata.EntryPoint(name='paraphe-approval-transport', value='paraphe.integrations.hermes', group='hermes_agent.plugins')
module = entry.load()
assert module._CTX is None and callable(module.register)
print('stdlib-only optional import: OK')
'''
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(SRC)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stdlib-only optional import: OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
