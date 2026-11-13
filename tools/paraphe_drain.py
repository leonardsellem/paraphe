"""Drain helper: freeze the answered backlog, adjudicate it, apply and verify.

Pure logic and CLI only. This module never talks to the network and never runs a
command: the Paraphe client is injected into every entry point, and the CLI
builds it in ``main()`` from ``tools/paraphe_send_client.py``.

Class -> verdict contract, assigned at T0 in this order (also the authority for
``--verify-ledger``):

    C     execution_status set on the card
          close_consumed, mark_only, outcome = execution_status_before,
          evidence.effect = {kind: card_field, handle: <status>, at: T0}
    T     external_id starts with hermes-approval-
          close_no_longer_actionable when expires_at < T0 (mark_only,
          outcome = not_recorded, reason = host_window_elapsed), never an
          accepted/rejected outcome; still_owed while the window is open
    A     origin traced, no read after the tap
          close_no_longer_actionable when the origin session is dead, else
          still_owed
    B_own origin traced, the asking session read the answer after the tap
          close_consumed, reason = own_read_after_tap
    B_list origin traced, only a list_unprocessed read after the tap
          still_owed by default; close_no_longer_actionable only with an
          independent effect handle dated after the tap
    E     no create trace in any Hermes state.db
          still_owed with a named owner; close only with an independent effect

The ledger is ``ledger.jsonl`` (one row per card), ``summary.json``,
``snapshot.json`` (keyed by request_id), ``signals.json`` (operator effects and
owners) and ``ledger.md`` (the still_owed owners as one fenced JSON block).
``ledger.md`` records ``{request_id, next_action, handle}``; for a
``kind=session`` owner the handle is ``{kind: session, value: <session_id>}``.
Handles of kind ``linear`` are not resolvable here, by design: it would need a
tracker call this module may not make; use a session, path or repo@sha handle.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import glob
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = "ls4060-drain/1"
CLASSES = ("T", "A", "B_own", "B_list", "C", "E")
VERDICTS = ("close_consumed", "close_no_longer_actionable", "still_owed")
REASONS = ("host_window_elapsed", "origin_session_ended", "own_read_after_tap",
           "recorded_execution_status", "independent_effect", "owner_continuation",
           "list_read_only", "no_origin_trace")
STATUSES = (None, "accepted", "rejected", "completed", "failed")
OUTCOMES = ("accepted", "rejected", "completed", "failed", "not_recorded")
ACTIONS = ("report_and_mark", "mark_only", "none")
STILL_OWED_REASONS = ("owner_continuation", "list_read_only", "no_origin_trace")
ALLOWED_VERDICTS = {
    "T": ("close_no_longer_actionable", "still_owed"),
    "A": ("close_no_longer_actionable", "still_owed"),
    "B_own": ("close_consumed",),
    "B_list": ("close_no_longer_actionable", "still_owed"),
    "C": ("close_consumed",),
    "E": ("close_no_longer_actionable", "still_owed"),
}
CLASS_REASONS = {
    "T": ("host_window_elapsed",),
    "A": ("origin_session_ended",),
    "B_own": ("own_read_after_tap",),
    "B_list": ("independent_effect",),
    "C": ("recorded_execution_status",),
    "E": ("independent_effect", "origin_session_ended"),
}
RESIDUE_CAUSE = ("the transport cards expired before the freeze and the host kept no boot "
                 "before 2026-11-03 20:25: their answer is recorded, no execution is")
NOTE_CAP = 200


class LedgerError(ValueError):
    """A ledger, a signal or a live state that the contract refuses."""


# -- time and small I/O ------------------------------------------------------

def _ts(value: Any) -> float | None:
    """Epoch seconds from an ISO-8601 string or a number; None when unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(value: Any) -> str | None:
    stamp = _ts(value)
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LedgerError(f"missing {path.name}") from None
    except ValueError:
        raise LedgerError(f"{path.name} is not JSON") from None


def _read_rows(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise LedgerError("missing ledger.jsonl") from None
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise LedgerError(f"ledger.jsonl line {number} is not JSON") from None
        if not isinstance(row, dict):
            raise LedgerError(f"ledger.jsonl line {number} is not an object")
        rows.append(row)
    return rows


def _write_rows(path: Path, rows: Iterable[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8")


# -- population and trace ----------------------------------------------------

def _card(item: dict) -> dict:
    """The snapshot view G4 re-derives from (get_response drops expires_at and kind)."""
    response = item.get("response") or {}
    return {
        "request_id": item.get("request_id"),
        "kind": item.get("kind"),
        "external_id": item.get("external_id"),
        "responded_at": response.get("responded_at"),
        "responded_via": response.get("responded_via"),
        "expires_at": item.get("expires_at"),
        "execution_status": item.get("execution_status"),
        "choice": response.get("choice"),
    }


def read_population(client: Any, t0: str) -> tuple[list[dict], list[str]]:
    """Partition one MCP read at T0: the frozen population, and the later arrivals."""
    envelope = client.list_unprocessed() or {}
    items = list(envelope.get("items") or [])
    count = envelope.get("count")
    if isinstance(count, int) and count != len(items):
        raise LedgerError(f"MCP count {count} disagrees with the {len(items)} items served")
    cut = _ts(t0)
    if cut is None:
        raise LedgerError(f"unreadable T0: {t0!r}")
    population: list[dict] = []
    excluded: list[str] = []
    for item in items:
        responded_at = (item.get("response") or {}).get("responded_at")
        stamp = _ts(responded_at)
        if item.get("processed_at") or item.get("status") != "answered" or stamp is None:
            continue
        if stamp > cut:
            excluded.append(str(item.get("request_id")))
        else:
            population.append(item)
    return population, sorted(excluded)


def _kind_of(text: str) -> str | None:
    if "list_unprocessed" in text:
        return "list"
    if "paraphe wait" in text or "paraphe-wait" in text:
        return "own"
    if "get_response" in text:
        return "own"
    if "mark_processed" in text:
        return "mark"
    if "report_execution" in text:
        return "report"
    if any(name in text for name in ("request_approval", "ask_question", "request_feedback")):
        return "create"
    return None


def _event_kinds(role: str, tool: str | None, calls_json: str | None, rid: str) -> list[str]:
    kinds: list[str] = []
    if role == "tool" and tool:
        kind = _kind_of(tool)
        if kind:
            kinds.append(kind)
    elif role == "assistant" and calls_json:
        try:
            calls = json.loads(calls_json)
        except ValueError:
            calls = []
        for call in calls if isinstance(calls, list) else []:
            function = call.get("function") or {}
            text = f"{function.get('name', '')} {function.get('arguments', '')}"
            if rid not in text and rid.replace("-", "") not in text:
                continue
            kind = _kind_of(text)
            if kind:
                kinds.append(kind)
    return kinds


def _state_dbs(dbs: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    if dbs is not None:
        return dbs
    found = [("default", os.path.expanduser("~/.hermes/state.db"))]
    for path in sorted(glob.glob(os.path.expanduser("~/.hermes/profiles/*/state.db"))):
        found.append((Path(path).parent.name, path))
    return found


def trace_origins(request_ids: Iterable[str], *,
                  dbs: list[tuple[str, str]] | None = None) -> dict[str, dict]:
    """Read-only FTS trace of every Hermes state.db: origin session and post-create reads.

    A read is a tool result or an assistant call naming a Paraphe read; it is
    classified ``own`` when it happens in the asking session, ``list`` otherwise.
    Message ids and instants only: never card content.
    """
    ids = list(request_ids)
    result: dict[str, dict] = {
        rid: {"origin": None, "message_ids": [], "reads": [], "mark_or_report_calls": []}
        for rid in ids
    }
    raw_reads: dict[str, list[dict]] = {rid: [] for rid in ids}
    for profile, path in _state_dbs(dbs):
        try:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                continue
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
        except (OSError, sqlite3.Error):
            continue
        try:
            for rid in ids:
                phrase = '"' + rid.replace("-", " ") + '"'
                try:
                    hits = connection.execute(
                        """SELECT m.id, m.session_id, m.role, m.tool_name, m.tool_calls,
                                  m.timestamp, s.ended_at, s.end_reason
                           FROM messages_fts f
                           JOIN messages m ON m.id = f.rowid
                           JOIN sessions s ON s.id = m.session_id
                           WHERE messages_fts MATCH ?""", (phrase,)).fetchall()
                except sqlite3.Error:
                    continue
                entry = result[rid]
                for mid, session, role, tool, calls, stamp, ended_at, end_reason in hits:
                    entry["message_ids"].append(mid)
                    for kind in _event_kinds(role, tool, calls, rid):
                        if kind == "create":
                            if entry["origin"] is None:
                                entry["origin"] = {"profile": profile, "session_id": session,
                                                   "ended_at": ended_at,
                                                   "end_reason": end_reason}
                        elif kind in ("own", "list"):
                            raw_reads[rid].append({"type": kind, "at": stamp,
                                                   "profile": profile, "session_id": session})
                        elif kind in ("mark", "report"):
                            entry["mark_or_report_calls"].append(kind)
        finally:
            connection.close()
    for rid, entry in result.items():
        entry["message_ids"] = sorted(set(entry["message_ids"]))
        entry["mark_or_report_calls"] = sorted(set(entry["mark_or_report_calls"]))
        origin = entry["origin"]
        key = (origin["profile"], origin["session_id"]) if origin else None
        reads = []
        for read in raw_reads[rid]:
            # An own read is a get_response/waiter in the asking session; a
            # list_unprocessed read never consumes, even in that session.
            own = (read["type"] == "own" and key is not None
                   and (read["profile"], read["session_id"]) == key)
            reads.append({"kind": "own" if own else "list", "at": read["at"]})
        entry["reads"] = sorted(reads, key=lambda row: (row["at"] if row["at"] is not None else 0))
    return result


def read_journal(path: str | os.PathLike | None) -> dict[str, dict]:
    """Last closeout attempt per request_id from the transport journal (U3)."""
    journal: dict[str, dict] = {}
    if not path:
        return journal
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return journal
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        rid = row.get("request_id")
        if not isinstance(rid, str) or not rid:
            continue
        journal[rid] = {"attempted": True, "last_stage": row.get("stage"),
                        "last_ok": row.get("ok") if isinstance(row.get("ok"), bool) else None}
    return journal


def _default_journal() -> str | None:
    pattern = os.path.expanduser(
        "~/.hermes/plugin-data/agent-plugin-paraphe-approval-transport-*/transport-closeout.jsonl")
    found = sorted(glob.glob(pattern), key=lambda path: os.path.getmtime(path))
    return found[-1] if found else None


def _read_after(trace: dict, kind: str, tap: float | None) -> float | None:
    if tap is None:
        return None
    instants = []
    for read in trace.get("reads") or []:
        stamp = _ts(read.get("at"))
        if read.get("kind") == kind and stamp is not None and stamp >= tap:
            instants.append(stamp)
    return min(instants) if instants else None


# -- class, verdict, rows ----------------------------------------------------

def classify(card: dict, trace: dict | None, now: str) -> str:
    """R2 precedence: execution_status, then the transport prefix, then the trace."""
    del now  # the class is frozen at T0; the instant is carried for callers
    if card.get("execution_status"):
        return "C"
    if str(card.get("external_id") or "").startswith("hermes-approval-"):
        return "T"
    trace = trace or {}
    if not trace.get("origin"):
        return "E"
    tap = _ts(card.get("responded_at"))
    if _read_after(trace, "own", tap) is not None:
        return "B_own"
    if _read_after(trace, "list", tap) is not None:
        return "B_list"
    return "A"


def _signal_ok(signal: Any, tap: float | None) -> bool:
    if not isinstance(signal, dict) or signal.get("kind") not in ("artifact", "session"):
        return False
    handle = signal.get("handle")
    at = _ts(signal.get("at"))
    return isinstance(handle, str) and bool(handle) and at is not None \
        and tap is not None and at > tap


def verdict(card: dict, trace: dict | None, signal: Any, owner: Any,
            now: str) -> tuple[str, str, Any]:
    """The class's allowed verdict with its reason and the owner when it stays owed."""
    cls = classify(card, trace, now)
    tap = _ts(card.get("responded_at"))
    if cls == "C":
        return "close_consumed", "recorded_execution_status", None
    if cls == "T":
        expires = _ts(card.get("expires_at"))
        cut = _ts(now)
        if expires is not None and cut is not None and expires < cut:
            return "close_no_longer_actionable", "host_window_elapsed", None
        return "still_owed", "owner_continuation", owner
    if cls == "A":
        where = (trace or {}).get("origin") or {}
        if where.get("ended_at") is not None or where.get("end_reason"):
            return "close_no_longer_actionable", "origin_session_ended", None
        return "still_owed", "owner_continuation", owner
    if cls == "B_own":
        return "close_consumed", "own_read_after_tap", None
    if cls == "B_list":
        if _signal_ok(signal, tap):
            return "close_no_longer_actionable", "independent_effect", None
        return "still_owed", "list_read_only", owner
    if _signal_ok(signal, tap):
        return "close_no_longer_actionable", "independent_effect", None
    return "still_owed", "no_origin_trace", owner


def _note(cls: str, result: str, reason: str) -> str:
    if result == "close_consumed":
        return ""
    return {
        "host_window_elapsed": "host window elapsed; the transport answer is recorded, no execution is claimed",
        "origin_session_ended": "origin session ended; the answer is no longer actionable",
        "own_read_after_tap": "the asking session read the answer after the tap",
        "recorded_execution_status": "the card already carries its execution status",
        "independent_effect": "an independent, dated effect signal closes the card",
        "owner_continuation": "origin is live; the answer is still owed to its named owner",
        "list_read_only": "only a listing read after the tap; no independent effect signal",
        "no_origin_trace": "no create trace in any Hermes state.db; owner continuation required",
    }[reason][:NOTE_CAP]


def _evidence_trace(trace: dict | None, card: dict) -> dict | None:
    where = (trace or {}).get("origin")
    if not where:
        return None
    tap = _ts(card.get("responded_at"))
    own, listed = _read_after(trace or {}, "own", tap), _read_after(trace or {}, "list", tap)
    reason = where.get("end_reason") or ("ended_at" if where.get("ended_at") is not None else None)
    return {
        "profile": where.get("profile"),
        "session_id": where.get("session_id"),
        "read_after_tap_at": _iso(own if own is not None else listed),
        "read_kind": "own" if own is not None else ("list" if listed is not None else None),
        "session_end_reason": reason,
        "message_ids": list((trace or {}).get("message_ids") or []),
    }


def _row(card: dict, trace: dict | None, journal: dict | None, owner: Any,
         effect: Any, cls: str, result: str, reason: str, now: str) -> dict:
    status = card.get("execution_status")
    if cls == "C":
        outcome, action = status, "mark_only"
        evidence_effect = {"kind": "card_field", "handle": status, "at": _iso(now)}
    elif result == "still_owed":
        outcome, action, evidence_effect = "not_recorded", "none", None
    else:
        outcome, action = "not_recorded", "mark_only"
        evidence_effect = None
        if cls in ("B_list", "E") and isinstance(effect, dict):
            evidence_effect = {"kind": effect.get("kind"), "handle": effect.get("handle"),
                               "at": _iso(effect.get("at"))}
    return {
        "request_id": card.get("request_id"),
        "external_id": card.get("external_id"),
        "kind": card.get("kind"),
        "class": cls,
        "verdict": result,
        "reason": reason,
        "note": _note(cls, result, reason),
        "responded_at": card.get("responded_at"),
        "responded_via": card.get("responded_via"),
        "execution_status_before": status if cls == "C" else None,
        "execution_status_after": status if cls == "C" else None,
        "outcome": outcome,
        "action": action,
        "evidence": {"trace": _evidence_trace(trace, card),
                     "effect": evidence_effect},
        "owner": owner if result == "still_owed" else None,
        "applied_at": None,
        "readback_processed_at": None,
        "journal": (journal or {"attempted": False, "last_stage": None, "last_ok": None})
        if cls == "T" else None,
    }


def write_ledger_md(out_dir: Path, rows: list[dict]) -> None:
    """A short header plus the still_owed owners as one fenced JSON block."""
    records = [{"request_id": row["request_id"],
                "next_action": (row.get("owner") or {}).get("next_action", ""),
                "handle": (row.get("owner") or {}).get("handle")}
               for row in rows if row.get("verdict") == "still_owed"]
    body = json.dumps(records, indent=2, sort_keys=True)
    (Path(out_dir) / "ledger.md").write_text(
        "# Drain ledger — still owed\n\n"
        "One record per still-owed card; the JSON block is the machine-readable copy.\n\n"
        f"```json\n{body}\n```\n", encoding="utf-8")


def build_ledger(out_dir: str | os.PathLike, client: Any, t0: str, inputs: dict, *,
                 trace_fn: Callable[[Iterable[str]], dict] = trace_origins,
                 journal_path: str | os.PathLike | None = None) -> dict:
    """Write the dry ledger, summary, snapshot, signals and ledger.md; no store write."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items, excluded = read_population(client, t0)
    cards = [_card(item) for item in items]
    traces = trace_fn([card["request_id"] for card in cards])
    journal = read_journal(journal_path if journal_path is not None else _default_journal())
    effects = dict((inputs or {}).get("effects") or {})
    owners = dict((inputs or {}).get("owners") or {})
    rows = []
    for card in cards:
        rid = card["request_id"]
        trace = traces.get(rid) or {}
        cls = classify(card, trace, t0)
        result, reason, owner = verdict(card, trace, effects.get(rid), owners.get(rid), t0)
        rows.append(_row(card, trace, journal.get(rid), owner, effects.get(rid),
                         cls, result, reason, t0))
    rows.sort(key=lambda row: (row["responded_at"] or "", row["request_id"]))
    _write_rows(out / "ledger.jsonl", rows)
    _write_json(out / "signals.json", {"effects": effects, "owners": owners})
    _write_json(out / "snapshot.json", {card["request_id"]: card for card in cards})
    write_ledger_md(out, rows)
    ids = sorted(card["request_id"] for card in cards)
    summary = {
        "schema": SCHEMA,
        "population_at_start": {
            "t0": _iso(t0),
            "count": len(cards),
            "mcp_count": len(cards) + len(excluded),
            "ids_sha256": _sha256(("\n".join(ids) + "\n").encode("utf-8")),
        },
        "class_counts": dict(sorted(Counter(row["class"] for row in rows).items())),
        "excluded_after_t0": excluded,
        "ledger_sha256": _sha256((out / "ledger.jsonl").read_bytes()),
        "residue_cause": RESIDUE_CAUSE,
    }
    _write_json(out / "summary.json", summary)
    return summary


# -- apply -------------------------------------------------------------------

def _rederive(row: dict, card: dict, signal: Any, t0: str) -> str:
    """Re-derive the verdict from the frozen row, the snapshot and signals.json.

    The class is frozen at T0; this never re-reads the trace, so a read or an
    answer that happened after the freeze cannot move a row under the apply.
    """
    cls = row["class"]
    if cls in ("C", "B_own"):
        return "close_consumed"
    if cls == "T":
        expires = _ts(card.get("expires_at"))
        cut = _ts(t0)
        return "close_no_longer_actionable" if expires is not None and cut is not None \
            and expires < cut else "still_owed"
    if cls == "A":
        trace = (row.get("evidence") or {}).get("trace") or {}
        return "close_no_longer_actionable" if trace.get("session_end_reason") else "still_owed"
    return "close_no_longer_actionable" if _signal_ok(signal, _ts(card.get("responded_at"))) \
        else "still_owed"


def apply(out_dir: str | os.PathLike, client: Any, *, now: str | None = None) -> dict:
    """Close the mark_only rows with mark_processed; reads back every row.

    Refuses a missing T0, a ledger/summary binding mismatch, and rows whose
    verdict no longer matches signals.json (the same binding, re-derived).
    """
    out = Path(out_dir)
    summary = _load_json(out / "summary.json")
    snapshot = _load_json(out / "snapshot.json")
    signals = _load_json(out / "signals.json")
    rows = _read_rows(out / "ledger.jsonl")
    frozen = (summary.get("population_at_start") or {}).get("t0")
    if not frozen:
        raise LedgerError("summary has no population_at_start.t0")
    expected = summary.get("ledger_sha256_after_apply") or summary.get("ledger_sha256")
    if _sha256((out / "ledger.jsonl").read_bytes()) != expected:
        raise LedgerError("ledger.jsonl does not match summary.ledger_sha256")
    effects = dict((signals or {}).get("effects") or {})
    owners = dict((signals or {}).get("owners") or {})
    for row in rows:
        rid = row["request_id"]
        if _rederive(row, snapshot[rid], effects.get(rid), frozen) != row["verdict"]:
            raise LedgerError(f"{rid}: ledger does not match signals.json")
        if row["verdict"] == "still_owed" and row.get("owner") != owners.get(rid):
            raise LedgerError(f"{rid}: owner does not match signals.json")
    stamp = now or _now_iso()
    for row in rows:
        rid = row["request_id"]
        if row["action"] != "mark_only":
            state = client.get_response(rid) or {}
            if state.get("processed_at"):
                row["readback_processed_at"] = state["processed_at"]
            continue
        state = client.get_response(rid) or {}
        if not state.get("processed_at"):
            client.mark_processed(rid)
            state = client.get_response(rid) or {}
        if state.get("processed_at") and not row["readback_processed_at"]:
            row["readback_processed_at"] = state["processed_at"]
        if state.get("processed_at") and row["applied_at"] is None:
            row["applied_at"] = stamp
        # G3(g)/R2: a non-C row never claims an outcome, and the drain never writes an execution status.
        row["execution_status_after"] = row["execution_status_before"]
    _write_rows(out / "ledger.jsonl", rows)
    write_ledger_md(out, rows)
    summary["ledger_sha256_after_apply"] = _sha256((out / "ledger.jsonl").read_bytes())
    _write_json(out / "summary.json", summary)
    return summary


# -- handle resolution -------------------------------------------------------

def _git_object_exists(repo: str, sha: str) -> bool:
    """A loose or packed object id, without running git."""
    if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha.lower()):
        return False
    gitdir = Path(repo) / ".git"
    if gitdir.is_file():
        marker = gitdir.read_text(encoding="utf-8", errors="replace").strip()
        if not marker.startswith("gitdir:"):
            return False
        gitdir = Path(marker.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = Path(repo) / gitdir
    if (gitdir / "objects" / sha[:2] / sha[2:]).is_file():
        return True
    for index in glob.glob(str(gitdir / "objects" / "pack" / "*.idx")):
        try:
            data = Path(index).read_bytes()
        except OSError:
            continue
        if data[:4] != b"\xfftOc" or len(data) < 8 + 256 * 4:
            continue
        fanout = int.from_bytes(data[8 + 255 * 4:8 + 256 * 4], "big")
        hashes = data[8 + 256 * 4:8 + 256 * 4 + fanout * 20]
        target = bytes.fromhex(sha)
        if target in (hashes[pos:pos + 20] for pos in range(0, len(hashes), 20)):
            return True
    return False


def _session_row(session_id: str, *, require_ended: bool | None,
                 state_dbs: list[tuple[str, str]] | None = None) -> bool:
    for _, path in _state_dbs(state_dbs):
        try:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                continue
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except (OSError, sqlite3.Error):
            continue
        try:
            found = connection.execute(
                "SELECT ended_at, end_reason FROM sessions WHERE id = ?", (session_id,)).fetchone()
        except sqlite3.Error:
            found = None
        finally:
            connection.close()
        if found is None:
            continue
        ended_at, end_reason = found
        if require_ended is None:
            return True
        if require_ended:
            return ended_at is not None or bool(end_reason)
        return ended_at is None
    return False


def _resolve_handle(handle: Any, *, state_dbs: list[tuple[str, str]] | None) -> None:
    if not isinstance(handle, dict):
        raise LedgerError("owner handle is not an object")
    kind, value = handle.get("kind"), handle.get("value")
    if not isinstance(value, str) or not value:
        raise LedgerError("owner handle has no value")
    if kind == "session":
        if not _session_row(value, require_ended=None, state_dbs=state_dbs):
            raise LedgerError(f"session handle {value} does not resolve")
    elif kind == "path":
        if not os.path.isabs(value) or not os.path.exists(value):
            raise LedgerError(f"path handle {value} does not exist")
    elif kind == "repo_sha":
        repo, _, sha = value.rpartition("@")
        if not repo or not _git_object_exists(repo, sha):
            raise LedgerError(f"repo@sha handle {value} does not resolve")
    elif kind == "linear":
        raise LedgerError(f"linear handle {value} needs the tracker CLI; use session, path or repo@sha")
    else:
        raise LedgerError(f"unknown handle kind {kind!r}")


def _resolve_owner(owner: Any, *, state_dbs: list[tuple[str, str]] | None = None) -> None:
    if not isinstance(owner, dict):
        raise LedgerError("still_owed row has no owner object")
    if owner.get("kind") not in ("session", "continuation"):
        raise LedgerError("owner kind must be session or continuation")
    handle = owner.get("handle")
    if owner["kind"] == "session":
        session_id = owner.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise LedgerError("session owner has no session_id")
        if not _session_row(session_id, require_ended=False, state_dbs=state_dbs):
            raise LedgerError(f"owner session {session_id} is not live")
        if handle != {"kind": "session", "value": session_id}:
            raise LedgerError("session owner handle must be that session id")
    else:
        if not isinstance(owner.get("next_action"), str) or not owner["next_action"].strip():
            raise LedgerError("continuation owner has no next_action")
        _resolve_handle(handle, state_dbs=state_dbs)


def _resolve_effect(effect: Any, tap: Any) -> None:
    if not isinstance(effect, dict):
        raise LedgerError("closure has no effect handle")
    at = _ts(effect.get("at"))
    tap_at = _ts(tap)
    if at is None or tap_at is None or at <= tap_at:
        raise LedgerError("effect handle is not dated after the tap")
    kind, handle = effect.get("kind"), effect.get("handle")
    if kind == "card_field":
        if handle not in ("accepted", "rejected", "completed", "failed"):
            raise LedgerError("card_field effect has no recorded status")
    elif kind == "artifact":
        if not isinstance(handle, str) or not handle:
            raise LedgerError("artifact effect has no handle")
        if handle.startswith("linear:"):
            _resolve_handle({"kind": "linear", "value": handle}, state_dbs=None)
        elif os.path.isabs(handle) and os.path.exists(handle):
            return
        elif "@" in handle:
            _resolve_handle({"kind": "repo_sha", "value": handle}, state_dbs=None)
        else:
            raise LedgerError(f"artifact handle {handle} does not resolve")
    elif kind == "session":
        if not isinstance(handle, str) or not _session_row(handle, require_ended=True):
            raise LedgerError(f"session effect {handle} has no recorded end")
    else:
        raise LedgerError(f"unknown effect kind {kind!r}")


# -- verify ------------------------------------------------------------------

def _parse_ledger_md(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    start = text.find("```json\n")
    end = text.find("\n```", start + 8) if start >= 0 else -1
    if start < 0 or end < 0:
        raise LedgerError("ledger.md has no fenced JSON block")
    try:
        records = json.loads(text[start + 8:end])
    except ValueError:
        raise LedgerError("ledger.md fenced block is not JSON") from None
    if not isinstance(records, list):
        raise LedgerError("ledger.md fenced block is not a list")
    return records


def verify_ledger(out_dir: str | os.PathLike, *,
                  state_dbs: list[tuple[str, str]] | None = None) -> dict:
    """G3: shape, class -> verdict, class -> signal, reasons, owners, digest."""
    out = Path(out_dir)
    summary = _load_json(out / "summary.json")
    if summary.get("schema") != SCHEMA:
        raise LedgerError(f"summary schema is not {SCHEMA}")
    snapshot = _load_json(out / "snapshot.json")
    signals = _load_json(out / "signals.json")
    del signals  # companions must exist; the rows carry their signal
    rows = _read_rows(out / "ledger.jsonl")
    if "ledger_sha256_after_apply" not in summary:
        if any(row.get("readback_processed_at") is not None for row in rows):
            raise LedgerError("readback_processed_at appears before an apply")
    frozen = (summary.get("population_at_start") or {}).get("t0")
    t0 = _ts(frozen)
    if t0 is None:
        raise LedgerError("summary has no population_at_start.t0")
    seen: set[str] = set()
    for row in rows:
        rid = row.get("request_id")
        if not isinstance(rid, str) or not rid:
            raise LedgerError("row without a request_id")
        if rid in seen:
            raise LedgerError(f"duplicate row for {rid}")
        seen.add(rid)
        if row.get("class") not in CLASSES:
            raise LedgerError(f"{rid}: unknown class")
        if row.get("verdict") not in VERDICTS:
            raise LedgerError(f"{rid}: unknown verdict")
        if row.get("reason") not in REASONS:
            raise LedgerError(f"{rid}: unknown reason")
        if row.get("kind") not in ("approval", "question"):
            raise LedgerError(f"{rid}: kind must be approval or question")
        if row.get("external_id") is not None and not isinstance(row["external_id"], str):
            raise LedgerError(f"{rid}: external_id must be a string or null")
        if not isinstance(row.get("responded_via"), str):
            raise LedgerError(f"{rid}: responded_via must be a string")
        if row.get("execution_status_before") not in STATUSES:
            raise LedgerError(f"{rid}: execution_status_before has an unknown value")
        if row.get("execution_status_after") not in STATUSES:
            raise LedgerError(f"{rid}: execution_status_after has an unknown value")
        if row.get("outcome") not in OUTCOMES:
            raise LedgerError(f"{rid}: unknown outcome")
        if row.get("action") not in ACTIONS:
            raise LedgerError(f"{rid}: unknown action")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict) or "trace" not in evidence or "effect" not in evidence:
            raise LedgerError(f"{rid}: evidence must carry trace and effect")
        for key in ("trace", "effect"):
            if evidence[key] is not None and not isinstance(evidence[key], dict):
                raise LedgerError(f"{rid}: evidence.{key} must be null or an object")
        if not isinstance(row.get("note"), str):
            raise LedgerError(f"{rid}: note must be a string")
        if row["verdict"] != "close_consumed" and not row["note"].strip():
            raise LedgerError(f"{rid}: a non-close row needs a note")
        if len(row["note"]) > NOTE_CAP:
            raise LedgerError(f"{rid}: note exceeds {NOTE_CAP} chars")
        if row.get("applied_at") is not None and _ts(row["applied_at"]) is None:
            raise LedgerError(f"{rid}: applied_at is not a timestamp")
        if row.get("readback_processed_at") is not None and _ts(row["readback_processed_at"]) is None:
            raise LedgerError(f"{rid}: readback_processed_at is not a timestamp")
        responded = _ts(row.get("responded_at"))
        if responded is None or responded > t0:
            raise LedgerError(f"{rid}: responded_at is not at or before T0")
        if row["class"] != "C" and row["execution_status_before"] is not None:
            raise LedgerError(f"{rid}: a non-C row has an execution_status_before")
        if row["verdict"] == "still_owed":
            if row.get("action") != "none":
                raise LedgerError(f"{rid}: still_owed needs action none")
            if row.get("applied_at") is not None:
                raise LedgerError(f"{rid}: still_owed cannot be applied")
            _resolve_owner(row.get("owner"), state_dbs=state_dbs)
        elif row.get("owner") is not None:
            raise LedgerError(f"{rid}: only a still_owed row carries an owner")
        if row["verdict"] not in ALLOWED_VERDICTS[row["class"]]:
            raise LedgerError(f"{rid}: {row['verdict']} is not allowed for class {row['class']}")
        if row["class"] == "T" and row["verdict"] == "close_consumed":
            raise LedgerError(f"{rid}: a transport row cannot be close_consumed")
        if row["class"] == "T":
            journal = row.get("journal")
            if not isinstance(journal, dict) or set(journal) != {"attempted", "last_stage", "last_ok"}:
                raise LedgerError(f"{rid}: a transport row needs its journal attempt")
            if not isinstance(journal["attempted"], bool):
                raise LedgerError(f"{rid}: journal.attempted must be a bool")
        self_check = row["verdict"] if row["verdict"] != "still_owed" else None
        if self_check is not None:
            allowed = CLASS_REASONS[row["class"]]
            if row["reason"] not in allowed:
                raise LedgerError(f"{rid}: reason {row['reason']} does not belong to {row['class']}")
        elif row["reason"] not in STILL_OWED_REASONS:
            raise LedgerError(f"{rid}: a still_owed row needs a still_owed reason")
        if row["class"] == "C":
            status = row["execution_status_before"]
            effect = evidence.get("effect") or {}
            if not (status in ("accepted", "rejected", "completed", "failed")
                    and row["outcome"] == status and row["action"] == "mark_only"
                    and row["execution_status_after"] == status
                    and effect.get("kind") == "card_field"
                    and effect.get("handle") == status
                    and _ts(effect.get("at")) is not None):
                raise LedgerError(f"{rid}: the C invariant is broken")
        else:
            if row["outcome"] != "not_recorded" or row["action"] == "report_and_mark":
                raise LedgerError(f"{rid}: a non-C row cannot claim an outcome")
            if row["action"] == "mark_only" and row["execution_status_after"] != row["execution_status_before"]:
                raise LedgerError(f"{rid}: mark_only changed the execution status")
            if row["class"] == "T" and row["execution_status_after"] is not None:
                raise LedgerError(f"{rid}: a transport row has an execution_status_after")
        card = snapshot.get(rid)
        if not isinstance(card, dict):
            raise LedgerError(f"{rid}: no snapshot record")
        if row["class"] == "T" and row["verdict"] == "close_no_longer_actionable":
            expires = _ts(card.get("expires_at"))
            if row["reason"] != "host_window_elapsed" or expires is None or expires >= t0:
                raise LedgerError(f"{rid}: a transport close needs an expiry before T0")
        if row["class"] == "A" and row["verdict"] == "close_no_longer_actionable":
            trace = evidence.get("trace") or {}
            if row["reason"] != "origin_session_ended" or not trace.get("session_end_reason"):
                raise LedgerError(f"{rid}: an A close needs the session end")
        if row["class"] == "B_own" and row["verdict"] == "close_consumed":
            trace = evidence.get("trace") or {}
            if row["reason"] != "own_read_after_tap" or trace.get("read_kind") != "own" \
                    or _ts(trace.get("read_after_tap_at")) is None:
                raise LedgerError(f"{rid}: a B_own close needs the own read")
        if row["class"] in ("B_list", "E") and row["verdict"] == "close_no_longer_actionable":
            effect = evidence.get("effect") or {}
            if row["reason"] != "independent_effect" or effect.get("kind") not in ("artifact", "session"):
                raise LedgerError(f"{rid}: this close needs an independent effect")
            _resolve_effect(effect, card.get("responded_at"))
    if any(row.get("reason") == "host_window_elapsed" for row in rows):
        if not str(summary.get("residue_cause") or "").strip():
            raise LedgerError("summary.residue_cause is empty beside a host_window_elapsed row")
    if len(rows) != summary["population_at_start"]["count"]:
        raise LedgerError("row count does not match population_at_start.count")
    expected = summary.get("ledger_sha256_after_apply") or summary.get("ledger_sha256")
    if _sha256((out / "ledger.jsonl").read_bytes()) != expected:
        raise LedgerError("ledger digest does not match summary")
    records = _parse_ledger_md(out / "ledger.md")
    still = [row for row in rows if row["verdict"] == "still_owed"]
    if len(records) != len(still):
        raise LedgerError("ledger.md does not carry one record per still_owed row")
    by_id = {record.get("request_id"): record for record in records}
    for row in still:
        record = by_id.get(row["request_id"])
        owner = row.get("owner") or {}
        if record is None or record.get("next_action") != owner.get("next_action") \
                or record.get("handle") != owner.get("handle"):
            raise LedgerError(f"{row['request_id']}: ledger.md owner does not match the row")
    return {"rows": len(rows),
            "class_counts": dict(sorted(Counter(row["class"] for row in rows).items()))}


def verify_live(out_dir: str | os.PathLike, client: Any, *,
                trace_fn: Callable[[Iterable[str]], dict] = trace_origins,
                state_dbs: list[tuple[str, str]] | None = None) -> dict:
    """G4/G5: re-derive every row from the snapshot, trace and live list; print it."""
    out = Path(out_dir)
    summary = _load_json(out / "summary.json")
    snapshot = _load_json(out / "snapshot.json")
    signals = _load_json(out / "signals.json") or {}
    rows = _read_rows(out / "ledger.jsonl")
    frozen = (summary.get("population_at_start") or {}).get("t0")
    if _ts(frozen) is None:
        raise LedgerError("summary has no population_at_start.t0")
    frozen_at, cut = str(frozen), _ts(frozen)
    effects = dict(signals.get("effects") or {})
    owners = dict(signals.get("owners") or {})
    traces = trace_fn([row["request_id"] for row in rows])
    live = {item.get("request_id"): item
            for item in (client.list_unprocessed() or {}).get("items") or []}
    errors: list[str] = []
    for row in rows:
        rid = row["request_id"]
        card = snapshot.get(rid)
        if not isinstance(card, dict):
            errors.append(f"{rid}: no snapshot record")
            continue
        trace = traces.get(rid) or {}
        cls = classify(card, trace, frozen_at)
        result, reason, owner = verdict(card, trace, effects.get(rid), owners.get(rid), frozen_at)
        if cls != row["class"]:
            errors.append(f"{rid}: class {row['class']} re-derives as {cls}")
        if result != row["verdict"]:
            errors.append(f"{rid}: verdict {row['verdict']} re-derives as {result}")
        if result == "still_owed":
            _resolve_owner(owner, state_dbs=state_dbs)
            if row.get("owner") != owner:
                errors.append(f"{rid}: owner differs from signals.json")
        expected = card.get("execution_status") if cls == "C" else None
        if row.get("execution_status_before") != expected:
            errors.append(f"{rid}: execution_status_before differs from the snapshot")
        if row.get("execution_status_after") != expected:
            errors.append(f"{rid}: execution_status_after differs from the snapshot")
        if row["class"] in ("B_list", "E") and row["verdict"] == "close_no_longer_actionable":
            try:
                _resolve_effect(row["evidence"]["effect"], card.get("responded_at"))
            except LedgerError as exc:
                errors.append(f"{rid}: {exc}")
        if row["verdict"] != "still_owed":
            state = client.get_response(rid) or {}
            if not state.get("processed_at"):
                errors.append(f"{rid}: a closed row is not processed")
            if expected is not None and state.get("execution_status") != expected:
                errors.append(f"{rid}: execution_status differs after the close")
            if not row.get("readback_processed_at"):
                errors.append(f"{rid}: a closed row has no readback")
        if row.get("kind") == "question" and row.get("outcome") != "not_recorded":
            errors.append(f"{rid}: a question carries an outcome")
        if row["class"] == "T":
            if row["verdict"] == "still_owed" and row["action"] != "none":
                errors.append(f"{rid}: a still_owed transport row needs action none")
            if row["verdict"] == "close_no_longer_actionable" and row["action"] != "mark_only":
                errors.append(f"{rid}: a closing transport row needs action mark_only")
            if row["outcome"] != "not_recorded":
                errors.append(f"{rid}: a transport row cannot claim an outcome")
        elif row["class"] == "C" and row["outcome"] != row["execution_status_before"]:
            errors.append(f"{rid}: the C outcome differs from the card field")
        elif row["class"] != "C" and row["outcome"] != "not_recorded":
            errors.append(f"{rid}: a non-C row cannot claim an outcome")
        if row["outcome"] in ("accepted", "rejected", "completed", "failed") \
                and not (row["evidence"].get("effect") or {}).get("handle"):
            errors.append(f"{rid}: an outcome without a handle")
        print(f"{rid} {row['kind']} {row['class']} {row['outcome']} "
              f"{row['execution_status_before']} -> {row['execution_status_after']} "
              f"{row['evidence'].get('effect')}")
    still = {row["request_id"] for row in rows if row["verdict"] == "still_owed"}
    for rid, item in live.items():
        at = _ts((item.get("response") or {}).get("responded_at"))
        if at is not None and cut is not None and at <= cut and rid not in still:
            errors.append(f"{rid}: a live answer in the frozen population is not still_owed")
    rows_by_id = {row["request_id"]: row for row in rows}
    for rid in sorted(still - set(live)):
        state = client.get_response(rid) or {}
        if not state.get("processed_at"):
            errors.append(f"{rid}: a still_owed row left the list without a readback")
        elif not rows_by_id[rid].get("readback_processed_at"):
            errors.append(f"{rid}: a benign departure is missing its readback")
    if errors:
        raise LedgerError("; ".join(errors))
    return {"rows": len(rows),
            "class_counts": dict(sorted(Counter(row["class"] for row in rows).items()))}


# -- CLI ---------------------------------------------------------------------

def _client_class():
    """Load the transport module beside this file, whatever the cwd or package root."""
    import importlib.util  # noqa: PLC0415 — CLI-only

    path = Path(__file__).resolve().parent / "paraphe_send_client.py"
    spec = importlib.util.spec_from_file_location("paraphe_send_client", path)
    if spec is None or spec.loader is None:
        raise LedgerError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ParapheSendClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adjudicate the answered Paraphe backlog.")
    parser.add_argument("--out", metavar="DIR")
    parser.add_argument("--t0", metavar="ISO8601Z")
    parser.add_argument("--signals", metavar="PATH")
    parser.add_argument("--apply", metavar="DIR")
    parser.add_argument("--verify-ledger", metavar="DIR")
    parser.add_argument("--verify-live", metavar="DIR")
    parser.add_argument("--send-bin", metavar="PATH")
    parser.add_argument("--journal", metavar="PATH")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.out, args.apply, args.verify_ledger,
                                     args.verify_live)) != 1:
        parser.error("exactly one of --out, --apply, --verify-ledger, --verify-live is required")
    try:
        if args.verify_ledger:
            report = verify_ledger(args.verify_ledger)
        elif args.out:
            out = Path(args.out)
            signals = Path(args.signals or out / "signals.json")
            if args.signals and not signals.is_file():
                parser.error(f"no signals file: {signals}")
            inputs = json.loads(signals.read_text(encoding="utf-8")) if signals.is_file() else {}
            t0 = args.t0 or _now_iso()
            summary = build_ledger(out, _client_class()(args.send_bin), t0, inputs,
                                   journal_path=args.journal)
            print(f"ledger written: {out} rows={summary['population_at_start']['count']}")
            return 0
        elif args.apply:
            summary = apply(args.apply, _client_class()(args.send_bin))
            print(f"applied: rows={summary['population_at_start']['count']}")
            return 0
        else:
            report = verify_live(args.verify_live, _client_class()(args.send_bin))
    except LedgerError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"rows={report['rows']}")
    print("classes=" + json.dumps(report["class_counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
