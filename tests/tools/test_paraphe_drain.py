"""The drain helper's own witness: injected client, injected trace, no network, no store.

One card per class, the class -> verdict table, the journal attachment and the
apply/verify contracts, all on hand-built records.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load by path: discovery imports this module as tools.*, where `tools` is the
# test package, not the repository's tools/ directory.
_SPEC = importlib.util.spec_from_file_location("paraphe_drain_under_test",
                                               ROOT / "tools" / "paraphe_drain.py")
assert _SPEC is not None and _SPEC.loader is not None
drain = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drain)

T0 = "2026-11-08T00:00:00Z"
EXPIRED = "2026-11-01T00:00:00Z"
FUTURE = "2026-12-01T00:00:00Z"
TAP = "2026-11-07T20:00:00Z"
AFTER_TAP = "2026-11-07T21:00:00Z"


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def card(rid: str, *, kind: str = "approval", external_id: str | None = None,
         responded_at: str = TAP, expires_at: str = FUTURE,
         execution_status: str | None = None, choice: str = "Approve") -> dict:
    return {
        "request_id": rid, "kind": kind, "external_id": external_id,
        "status": "answered", "pending": False, "version": 1,
        "expires_at": epoch(expires_at), "execution_status": execution_status,
        "processed_at": None,
        "response": {"choice": choice, "responded_at": responded_at,
                     "responded_via": "telegram", "text": None},
    }


def origin(session_id: str = "session-1", ended_at: str | None = None,
           end_reason: str | None = None) -> dict:
    return {"profile": "cole", "session_id": session_id,
            "ended_at": epoch(ended_at) if ended_at else None,
            "end_reason": end_reason}


def trace(where: dict | None = None, reads: tuple = (), mark: tuple = ()) -> dict:
    return {"origin": where, "message_ids": [1], "reads": list(reads),
            "mark_or_report_calls": list(mark)}


def read(kind: str, at: str = AFTER_TAP) -> dict:
    return {"kind": kind, "at": epoch(at)}


class FakeClient:
    """Records every call; changes are visible to the next read, like the store."""

    def __init__(self, items: list[dict]):
        self.items = items
        self.calls: list[tuple[str, str]] = []
        self.processed: dict[str, str] = {}

    def _open(self) -> list[dict]:
        return [item for item in self.items if not item["processed_at"]]

    def list_unprocessed(self) -> dict:
        items = self._open()
        return {"count": len(items), "items": items}

    def get_response(self, request_id: str) -> dict:
        self.calls.append(("get_response", request_id))
        state = next(item for item in self.items if item["request_id"] == request_id)
        return {"request_id": request_id, "processed_at": state["processed_at"],
                "execution_status": state["execution_status"]}

    def mark_processed(self, request_id: str) -> dict:
        self.calls.append(("mark_processed", request_id))
        self.processed[request_id] = "2026-11-08T00:05:00Z"
        state = next(item for item in self.items if item["request_id"] == request_id)
        state["processed_at"] = self.processed[request_id]
        return {"request_id": request_id, "processed_at": self.processed[request_id]}

    @property
    def mutations(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "get_response"]


class DrainCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        self.handle = self.out / "continuation.md"
        self.handle.write_text("next run: resume the drained ticket\n", encoding="utf-8")
        self.journal = self.out / "transport-closeout.jsonl"

    def owner(self, session: str = "session-1") -> dict:
        return {"kind": "continuation", "session_id": session,
                "next_action": "resume the ticket",
                "handle": {"kind": "path", "value": str(self.handle)}}

    def build(self, items: list[dict], traces: dict[str, dict], *,
              effects: dict | None = None, owners: dict | None = None):
        client = FakeClient(items)
        summary = drain.build_ledger(
            self.out, client, T0,
            {"effects": effects or {}, "owners": owners or {}},
            trace_fn=lambda ids: {rid: traces.get(rid, trace()) for rid in ids},
            journal_path=self.journal,
        )
        return client, summary

    def rows(self) -> dict[str, dict]:
        return {row["request_id"]: row for row in drain._read_rows(self.out / "ledger.jsonl")}


class TestDrain(DrainCase):
    def test_class_map_and_signals(self) -> None:
        evidence = self.out / "effect.md"
        evidence.write_text("shipped\n", encoding="utf-8")
        items = [
            card("c1", execution_status="completed", external_id="hermes-approval-x"),
            card("t1", external_id="hermes-approval-a", expires_at=EXPIRED),
            card("t2", external_id="hermes-approval-b", expires_at=FUTURE),
            card("a1"), card("a2"), card("b1"), card("b2"), card("b3"), card("e1"), card("e2"),
        ]
        traces = {
            "c1": trace(origin("s-c")),
            "t1": trace(), "t2": trace(),
            "a1": trace(origin("s-a1", ended_at="2026-11-07T22:00:00Z", end_reason="done")),
            "a2": trace(origin("s-a2")),
            "b1": trace(origin("s-b1"), reads=[read("own")]),
            "b2": trace(origin("s-b2"), reads=[read("list")]),
            "b3": trace(origin("s-b3"), reads=[read("list")]),
            "e1": trace(), "e2": trace(),
        }
        owners = {"t2": self.owner(), "a2": self.owner(), "b2": self.owner(),
                  "e1": self.owner()}
        effects = {"b3": {"kind": "artifact", "handle": str(evidence), "at": AFTER_TAP},
                   "e2": {"kind": "artifact", "handle": str(evidence), "at": AFTER_TAP}}
        _, summary = self.build(items, traces, effects=effects, owners=owners)
        rows = self.rows()
        self.assertEqual({rid: row["class"] for rid, row in rows.items()},
                         {"c1": "C", "t1": "T", "t2": "T", "a1": "A", "a2": "A",
                          "b1": "B_own", "b2": "B_list", "b3": "B_list",
                          "e1": "E", "e2": "E"})
        # C keeps the recorded status and its card-field evidence; the prefix loses to it.
        self.assertEqual((rows["c1"]["verdict"], rows["c1"]["outcome"], rows["c1"]["action"]),
                         ("close_consumed", "completed", "mark_only"))
        self.assertEqual(rows["c1"]["evidence"]["effect"],
                         {"kind": "card_field", "handle": "completed", "at": T0})
        # A dead origin closes, a live one stays owed; B_own consumes, B_list does not.
        self.assertEqual((rows["a1"]["verdict"], rows["a1"]["reason"]),
                         ("close_no_longer_actionable", "origin_session_ended"))
        self.assertEqual((rows["a2"]["verdict"], rows["a2"]["reason"]),
                         ("still_owed", "owner_continuation"))
        self.assertEqual((rows["b1"]["verdict"], rows["b1"]["reason"]),
                         ("close_consumed", "own_read_after_tap"))
        self.assertEqual((rows["b2"]["verdict"], rows["b2"]["reason"]),
                         ("still_owed", "list_read_only"))
        self.assertEqual((rows["b3"]["verdict"], rows["b3"]["reason"]),
                         ("close_no_longer_actionable", "independent_effect"))
        self.assertEqual(rows["e1"]["reason"], "no_origin_trace")
        self.assertEqual(rows["e2"]["verdict"], "close_no_longer_actionable")
        self.assertEqual(sum(summary["class_counts"].values()), summary["population_at_start"]["count"])
        self.assertEqual(summary["population_at_start"]["count"], len(items))

    def test_origin_dead_requires_a_liveness_signal(self) -> None:
        items = [card("a1"), card("a2"), card("e1")]
        traces = {
            "a1": trace(origin("s-a1", ended_at="2026-11-07T22:00:00Z", end_reason="done")),
            "a2": trace(origin("s-a2")),
            "e1": trace(),
        }
        self.build(items, traces, owners={"a2": self.owner(), "e1": self.owner()})
        rows = self.rows()
        self.assertEqual(rows["a1"]["verdict"], "close_no_longer_actionable")
        self.assertEqual(rows["a2"]["verdict"], "still_owed")
        # No trace is not death: without a signal the card stays owed.
        self.assertEqual(rows["e1"]["verdict"], "still_owed")

    def test_unverifiable_origin_stays_owed(self) -> None:
        items = [card("e1")]
        self.build(items, {"e1": trace()}, owners={"e1": self.owner()})
        row = self.rows()["e1"]
        self.assertEqual((row["class"], row["verdict"], row["reason"]),
                         ("E", "still_owed", "no_origin_trace"))
        self.assertEqual(row["owner"], self.owner())
        report = drain.verify_ledger(self.out)
        self.assertEqual(report["rows"], 1)
        self.assertEqual(report["class_counts"], {"E": 1})
        # A live session owner resolves by its state.db row, not by a path.
        db = self.out / "state.db"
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, ended_at REAL, end_reason TEXT)")
        connection.execute("INSERT INTO sessions VALUES ('live-1', NULL, NULL)")
        connection.commit()
        connection.close()
        session_owner = {"kind": "session", "session_id": "live-1", "next_action": "",
                         "handle": {"kind": "session", "value": "live-1"}}
        sub = self.out / "session-owner"
        drain.build_ledger(sub, FakeClient([card("e2")]), T0,
                           {"effects": {}, "owners": {"e2": session_owner}},
                           trace_fn=lambda ids: {"e2": trace()}, journal_path=self.journal)
        self.assertEqual(drain.verify_ledger(sub, state_dbs=[("test", str(db))])["rows"], 1)

    def test_transport_residue_is_mark_only(self) -> None:
        self.journal.write_text(
            json.dumps({"request_id": "t1", "stage": "report_execution", "ok": False,
                        "error_type": "ConnectionError"}) + "\n" +
            json.dumps({"request_id": "t1", "stage": "mark_processed", "ok": True,
                        "error_type": None}) + "\n",
            encoding="utf-8")
        items = [card("t1", external_id="hermes-approval-a", expires_at=EXPIRED),
                 card("t2", external_id="hermes-approval-b", expires_at=FUTURE)]
        self.build(items, {"t1": trace(), "t2": trace()}, owners={"t2": self.owner()})
        rows = self.rows()
        closed = rows["t1"]
        self.assertEqual((closed["verdict"], closed["reason"], closed["action"], closed["outcome"]),
                         ("close_no_longer_actionable", "host_window_elapsed", "mark_only",
                          "not_recorded"))
        self.assertNotIn(closed["verdict"], ("close_consumed",))
        self.assertNotIn(closed["outcome"], ("accepted", "rejected"))
        self.assertIsNone(closed["execution_status_before"])
        self.assertIsNone(closed["execution_status_after"])
        self.assertEqual(closed["journal"], {"attempted": True, "last_stage": "mark_processed",
                                             "last_ok": True})
        self.assertEqual((rows["t2"]["verdict"], rows["t2"]["action"]), ("still_owed", "none"))
        self.assertEqual(rows["t2"]["journal"]["attempted"], False)

    def test_verdict_keeps_a_live_origin_open(self) -> None:
        snapshot = drain._card(card("a1"))
        owner = self.owner()
        alive = drain.verdict(snapshot, trace(origin("s-a1")), None, owner, T0)
        self.assertEqual(alive, ("still_owed", "owner_continuation", owner))
        dead = drain.verdict(snapshot, trace(origin("s-a1", ended_at="2026-11-07T22:00:00Z")),
                             None, owner, T0)
        self.assertEqual(dead, ("close_no_longer_actionable", "origin_session_ended", None))

    def test_apply_calls_only_mark_processed(self) -> None:
        items = [
            card("c1", execution_status="completed"),
            card("t1", external_id="hermes-approval-a", expires_at=EXPIRED),
            card("a1"), card("b1"), card("e1"),
        ]
        traces = {
            "c1": trace(origin("s-c")),
            "t1": trace(),
            "a1": trace(origin("s-a1", ended_at="2026-11-07T22:00:00Z", end_reason="done")),
            "b1": trace(origin("s-b1"), reads=[read("own")]),
            "e1": trace(),
        }
        client, _ = self.build(items, traces, owners={"e1": self.owner()})
        drain.apply(self.out, client)
        mutating = client.mutations
        self.assertEqual([name for name, _ in mutating], ["mark_processed"] * 4)
        self.assertNotIn("report_execution", [name for name, _ in client.calls])
        rows = self.rows()
        for rid in ("c1", "t1", "a1", "b1"):
            self.assertIsNotNone(rows[rid]["applied_at"])
            self.assertEqual(rows[rid]["readback_processed_at"], "2026-11-08T00:05:00Z")
        self.assertIsNone(rows["e1"]["applied_at"])
        self.assertIsNone(rows["e1"]["readback_processed_at"])
        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256((self.out / "ledger.jsonl").read_bytes()).hexdigest()
        self.assertEqual(summary["ledger_sha256_after_apply"], digest)
        live = drain.verify_live(
            self.out, client,
            trace_fn=lambda ids: {rid: traces.get(rid, trace()) for rid in ids})
        self.assertEqual(live["rows"], 5)

    def test_still_owed_rows_are_refused_before_any_call(self) -> None:
        items = [card("e1")]
        client, _ = self.build(items, {"e1": trace()})
        with self.assertRaises(drain.LedgerError):
            drain.verify_ledger(self.out)
        drain.apply(self.out, client)
        self.assertEqual(client.mutations, [])
        row = self.rows()["e1"]
        self.assertEqual((row["verdict"], row["action"]), ("still_owed", "none"))
        self.assertIsNone(row["applied_at"])

    def test_second_apply_is_a_noop(self) -> None:
        items = [card("t1", external_id="hermes-approval-a", expires_at=EXPIRED), card("b1")]
        traces = {"t1": trace(), "b1": trace(origin("s-b1"), reads=(read("own"),))}
        client, _ = self.build(items, traces)
        # The owner closed and reported its card between T0 and the apply.
        items[1].update(processed_at="2026-11-08T00:05:00Z", execution_status="completed")
        drain.apply(self.out, client)
        rows = self.rows()
        self.assertEqual([rid for name, rid in client.mutations if rid == "b1"], [])
        self.assertEqual((rows["b1"]["readback_processed_at"], rows["b1"]["execution_status_after"]),
                         ("2026-11-08T00:05:00Z", "completed"))
        self.assertEqual(rows["b1"]["outcome"], "not_recorded")
        drain.verify_ledger(self.out)
        first = (self.out / "ledger.jsonl").read_bytes(), list(client.mutations)
        drain.apply(self.out, client)
        self.assertEqual((self.out / "ledger.jsonl").read_bytes(), first[0])
        self.assertEqual(client.mutations, first[1])

    def test_ledger_carries_no_card_content(self) -> None:
        sentinel = "zq-card-content-must-not-leak"
        item = card("e1")
        item.update(question=sentinel, context=sentinel, details=sentinel, title=sentinel)
        item["response"]["text"] = sentinel
        self.build([item], {"e1": trace()}, owners={"e1": self.owner()})
        ledger = (self.out / "ledger.jsonl").read_text(encoding="utf-8")
        ledger_md = (self.out / "ledger.md").read_text(encoding="utf-8")
        self.assertNotIn(sentinel, ledger)
        self.assertNotIn(sentinel, ledger_md)
        for row in self.rows().values():
            self.assertNotIn("choice", row)
            self.assertNotIn("question", row)

    def test_verify_rejects_a_truncated_ledger(self) -> None:
        items = [card("e1")]
        self.build(items, {"e1": trace()}, owners={"e1": self.owner()})
        drain.verify_ledger(self.out)
        path = self.out / "ledger.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        with self.assertRaises(drain.LedgerError):
            drain.verify_ledger(self.out)

    def test_verify_rejects_outcome_without_handle(self) -> None:
        items = [card("c1", execution_status="completed")]
        self.build(items, {"c1": trace(origin("s-c"))})
        path = self.out / "ledger.jsonl"
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        row["evidence"]["effect"] = None
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        summary = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        summary["ledger_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.out / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaises(drain.LedgerError):
            drain.verify_ledger(self.out)


if __name__ == "__main__":
    unittest.main()
