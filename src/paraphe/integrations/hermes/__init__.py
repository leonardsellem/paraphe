"""Optional Hermes approval transport through the configured Paraphe MCP server.

Register with Hermes, then select ``security.approval.transport: paraphe`` and
allow ``paraphe`` under the plugin's ``mcp_allowlist``. Only an explicit owner
Approve or Deny answers the host request; technical failures raise, and timeout
cancels the card best-effort before raising. No credential or HTTP client lives
here. The host supplies the redacted request and owns decision persistence.

Diagnostics keep two capped atomic latest-only snapshots in ``ctx.state.data_dir``;
legacy state is neither read nor changed. See the integration guide for the
separate optional smart-admission ledger and its retention boundary.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import partial
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
_DIAGNOSTIC_LOGGER = logging.getLogger("paraphe-approval-transport.diagnostics")
# The loader can import this module under different names in the same process.
_new_warning_state = {"lock": threading.Lock(), "pid": None}
_WARNING_STATE = vars(_DIAGNOSTIC_LOGGER).setdefault("_paraphe_warning_state", _new_warning_state)
if _WARNING_STATE is _new_warning_state and hasattr(os, "register_at_fork"):
    # A child must not inherit a lock held by a vanished parent thread.
    os.register_at_fork(after_in_child=lambda state=_WARNING_STATE: state.update(lock=threading.Lock()))

TRANSPORT = "paraphe"
SERVER = "paraphe"
TITLE_CAP = 200
DETAILS_CAP = 2000  # Paraphe details cap
POLL_S = 3.0
COMMAND_HEAD_CAP = 200  # the trace row keeps the command's first line only
# Match whole words; carry the severity explicitly named by the host guard.
HIGH_RISK_WORDS = frozenset(("rm", "delete", "force", "reset", "prod", "drop", "shred", "truncate"))
HIGH_RISK_PREFIXES = ("migrat", "deploy")  # migration family; deploy/deployed/deploying
_SEVERITY_RE = re.compile(r"\[(high|medium|low|info)\]", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")
# Paraphe identity-field caps (`request_approval` provenance).
REPO_CAP, WORKTREE_CAP, TICKET_CAP = 120, 120, 200
# Recognize generic tracker keys in branch or worktree names.
_TICKET_RE = re.compile(r"\b([a-z]{2,4})[-/](\d{2,5})\b", re.IGNORECASE)
_GIT_TIMEOUT_S = 2.0

_CTX: Any = None
LAST: dict[str, Any] = {"failure": None, "request_id": None, "diagnostic": None}
# LAST is legacy, process-global test/debug state; never use it for durable correlation.
_ACTIVE_DIAGNOSTIC: ContextVar[Any] = ContextVar("paraphe_transport_diagnostic", default=None)
_PHASES = frozenset(("registration", "present", "request_approval", "get_response", "cancel_request"))
_CODES = frozenset(("registered", "started", "created", "owner_approve", "owner_deny",
                    "no_context", "too_long", "no_request_id", "invalid_response", "card_cancelled",
                    "card_expired", "timeout", "client_exception", "mcp_error", "unparsable_result"))
_EXCEPTIONS = frozenset(("PermissionError", "TimeoutError", "ConnectionError", "OSError",
                         "RuntimeError", "ValueError", "TypeError", "Exception"))
_CARD_ID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _store_diagnostic(ctx: Any, kind: str, row: dict) -> None:
    try:
        if kind not in ("registration", "attempt"):
            raise ValueError("unknown diagnostic kind")
        if len(json.dumps(row, ensure_ascii=False, indent=2).encode("utf-8")) > 2048:
            raise ValueError("diagnostic snapshot exceeds byte cap")
        from utils import atomic_json_write  # public host helper; fake/older hosts may lack it
        atomic_json_write(ctx.state.data_dir / f"transport-{kind}-latest.json", row,
                          indent=2, ensure_ascii=False, mode=0o600, fsync_dir=True)
    except Exception as exc:
        # Suppress only warnings, never later writes or host/owner decisions.
        with _WARNING_STATE["lock"]:
            if _WARNING_STATE["pid"] != os.getpid():
                _WARNING_STATE["pid"] = os.getpid()
                _DIAGNOSTIC_LOGGER.warning(
                    "paraphe approval transport: diagnostic state not written (%s)", _exception_type(exc))


def _persist(phase: str, code: str, *, exception_type: str | None = None,
             paraphe_request_id: str | None = None) -> None:
    active = _ACTIVE_DIAGNOSTIC.get()
    if active is None or phase not in _PHASES or code not in _CODES:
        return
    ctx, kind, row = active
    row.update(phase=phase, code=code)
    row.pop("exception_type", None)
    if exception_type in _EXCEPTIONS:
        row["exception_type"] = exception_type
    if isinstance(paraphe_request_id, str) and _CARD_ID_RE.fullmatch(paraphe_request_id):
        row["paraphe_request_id"] = paraphe_request_id  # exact reply ID, never inferred from LAST
    _store_diagnostic(ctx, kind, row)


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    registration: dict[str, Any] = {"schema": 1, "pid": os.getpid(), "registration_id": uuid.uuid4().hex,
                    "phase": "registration", "code": "registered"}
    try:
        registration["file_at_registration_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except Exception as exc:
        logger.warning("paraphe approval transport: registration fingerprint unavailable (%s)", _exception_type(exc))
    # Bind this context/registration, not the most recently registered process-global _CTX.
    ctx.register_approval_transport(TRANSPORT, partial(present, ctx=ctx, registration=registration))
    _store_diagnostic(ctx, "registration", registration)
    try:
        ctx.register_hook("post_approval_response", on_post_approval_response)
    except Exception:  # a host without the plugin-hook seam keeps the transport working
        logger.warning("paraphe approval transport: approval hook not registered (no admission rows)")


def _payload(env: Any) -> dict:
    """The MCP envelope's result as a dict: ``{"ok": true, "result": <dict|json-string>}``."""
    if not isinstance(env, dict) or not env.get("ok"):
        raise RuntimeError("mcp_error")  # never propagate an untrusted error body
    result = env.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            raise RuntimeError("unparsable_result") from None
    if not isinstance(result, dict):
        raise RuntimeError("unparsable_result")
    return result


def _exception_type(exc: Exception) -> str:
    # Class names are untrusted too: report only these fixed categories.
    return next((cls.__name__ for cls in (
        PermissionError, TimeoutError, ConnectionError, OSError, RuntimeError, ValueError, TypeError
    ) if isinstance(exc, cls)), "Exception")


def _fail(request: Any, failure: str, *, phase: str = "present", code: str | None = None,
          exception_type: str | None = None) -> Any:
    diagnostic = {"phase": phase, "code": code or failure, "failure": failure}
    if exception_type:
        diagnostic["exception_type"] = exception_type
    for field, size in (("request_id", 32), ("digest", 64)):
        value = getattr(request, field, None)
        if isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value):
            diagnostic[f"host_{field}"] = value
    LAST.update(failure=failure, diagnostic=diagnostic)
    logger.warning("paraphe approval transport: failure %s", json.dumps(diagnostic, sort_keys=True))
    _persist(phase, code or failure, exception_type=exception_type)
    raise RuntimeError(failure) from None


def _call_mcp(request: Any, ctx: Any, tool: str, arguments: dict) -> dict:
    """Keep the failing stage, not MCP bodies or exception text; never retry."""
    _persist(tool, "started")
    try:
        envelope = ctx.call_mcp(SERVER, tool, arguments)
    except Exception as exc:
        category = _exception_type(exc)
        return _fail(request, f"mcp_error:{category}", phase=tool,
                     code="client_exception", exception_type=category)
    try:
        return _payload(envelope)
    except Exception as exc:
        # Whitelist codes even for unexpected parser failures (e.g. nesting depth).
        code = "mcp_error" if type(exc) is RuntimeError and exc.args == ("mcp_error",) else "unparsable_result"
        category = _exception_type(exc)
        return _fail(request, f"mcp_error:{category}", phase=tool,
                     code=code, exception_type=category)


def _marker_high(text: str) -> bool:
    words = set(_WORD_RE.findall(text.lower()))
    return any(w in HIGH_RISK_WORDS or w.startswith(HIGH_RISK_PREFIXES) for w in words)


def _risk(pattern_key: str, command: str, description: str = "") -> str:
    """Destructive whole-word markers win over the severity named by the guard."""
    if _marker_high(f"{pattern_key} {command}"):
        return "high"
    severities = {s.lower() for s in _SEVERITY_RE.findall(description or "")}
    return "high" if "high" in severities else "medium"


def _ledger_path() -> str:
    """The router's receipts ledger — the file the band hook appends to (same env override)."""
    return os.environ.get("ESCALATION_BAND_RECEIPTS") or os.path.join(
        os.path.expanduser("~"), ".hermes", "state", "escalation-band.receipts.jsonl")


def record_smart_admission(command: str = "", pattern_key: str = "", session_key: str = "", **_: Any) -> None:
    """Append an optional smart-admission receipt; failures never affect approval."""
    try:
        head = str(command or "").splitlines()[0][:COMMAND_HEAD_CAP] if command else ""
        row = {"ts": int(time.time()), "phase": "smart", "verdict": "act", "card_class": "terminal_admitted",
               "pattern_key": str(pattern_key or ""), "session_key": str(session_key or ""), "command_head": head}
        with open(_ledger_path(), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — a raising callback can kill every tool in the session
        logger.warning("paraphe approval transport: smart-admission row not written (%s)", _exception_type(exc))


def on_post_approval_response(choice: str = "", **kwargs: Any) -> None:
    """Plugin hook: the host's smart gate approved a flagged command with no human step."""
    if choice == "smart_approve":
        record_smart_admission(**kwargs)


def _git_run(cwd: str, *args: str) -> str | None:
    """Best-effort read-only git query in the lane's cwd; ``None`` on any failure.

    ``shell=False``; missing git, a non-repo cwd, a timeout or a nonzero exit all
    return ``None`` — the card is never delayed beyond the timeout nor blocked on
    context derivation."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _normalize_remote(url: str) -> str:
    """Normalize SCP-style or HTTPS Git remotes to ``org/repo``."""
    url = (url or "").strip()
    if url.endswith(".git"):
        url = url[:-4]
    if "://" in url:
        rest = url.split("://", 1)[1]
        return rest.split("/", 1)[1] if "/" in rest else ""
    if ":" in url:
        return url.split(":", 1)[1].lstrip("/")
    return url


def _work_context(cwd: str) -> dict[str, str]:
    """What the lane's cwd proves about the work — never guessed.

    ``repo`` (normalized origin URL), ``branch``, ``worktree`` (git toplevel) and
    ``ticket`` (read from the branch, else the worktree name). Missing pieces are
    absent; the whole dict is empty when nothing is derivable."""
    work: dict[str, str] = {}
    if not cwd or not os.path.isdir(cwd):
        return work
    toplevel = _git_run(cwd, "rev-parse", "--show-toplevel")
    branch = _git_run(cwd, "branch", "--show-current")
    remote = _git_run(cwd, "remote", "get-url", "origin")
    if remote:
        repo = _normalize_remote(remote)
        if repo:
            work["repo"] = repo
    if toplevel:
        work["worktree"] = toplevel
    if branch:
        work["branch"] = branch
    for source in (branch, os.path.basename(toplevel) if toplevel else None):
        match = _TICKET_RE.search(source) if source else None
        if match:
            work["ticket"] = f"{match.group(1)}-{match.group(2)}".upper()
            break
    return work


def _context_line(work: dict[str, str]) -> str:
    """The card's « Travail concerné » line — only the facts that exist, or ``""``."""
    bits = [f"{label} {work[key]}" for key, label in
            (("repo", "dépôt"), ("branch", "branche"), ("ticket", "tâche"), ("worktree", "worktree"))
            if work.get(key)]
    return f"Travail concerné : {' · '.join(bits)}.\n" if bits else ""


def _origin(cwd: str = "") -> str:
    """Where this request comes from: the Herdr pane when managed, plus the cwd."""
    bits = []
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        bits.append(
            f"espace {os.environ.get('HERDR_WORKSPACE_ID', '?')} — "
            f"onglet {os.environ.get('HERDR_TAB_ID', '?')} — fenêtre {pane}"
        )
    if cwd:
        bits.append(f"dossier {cwd}")
    return " · ".join(bits) or "session inconnue"


def _details(command: str, description: str, origin: str, work: dict[str, str]) -> str:
    """Why the card exists first, then the work it concerns, then origin, then the
    exact command, then what the tap does. The host hands only the flag text — never
    the lane's purpose — so the copy says that instead of inventing a why; the work
    facts the cwd proves are named so the owner can decide without decoding the
    command."""
    head = (
        f"Pourquoi cette carte : le garde a bloqué cette commande (« {description or 'signalée par le garde'} ») ; "
        "ton accord la déverrouille pour UNE exécution. Ce qu'elle sert dans le travail en cours : "
        "non transmis par la lane — en cas de doute, refuse et demande la raison.\n"
        f"{_context_line(work)}"
        f"D'où ça vient : {origin}.\n\n"
        "Commande exacte :\n"
    )
    tail = "\n\nApprove = elle s'exécute UNE seule fois, puis l'agent reprend. Deny = rien ne s'exécute."
    room = DETAILS_CAP - len(head) - len(tail)
    if len(command) > room:
        cut = max(room - 80, 0)
        command = command[:cut] + f"\n…[affichage tronqué : {len(command) - cut} caractères non montrés]"
    return head + command + tail


def present(request: Any, *, ctx: Any = None, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic, registration: dict | None = None) -> Any:
    ctx = ctx or _CTX
    row: dict[str, Any] = {"schema": 1, "pid": os.getpid()}
    if registration is not None:
        for field, size in (("registration_id", 32), ("file_at_registration_sha256", 64)):
            value = registration.get(field)
            if isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value):
                row[field] = value
    for field, size in (("request_id", 32), ("digest", 64)):
        value = getattr(request, field, None)
        if isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value):
            row[f"host_{field}"] = value
    # ponytail: latest commit wins, including overlapping attempts; no retained history.
    # Each row keeps its own registration even when another registration overwrites latest.
    token = _ACTIVE_DIAGNOSTIC.set((ctx, "attempt", row))
    try:
        _persist("present", "started")
        return _present(request, ctx=ctx, sleep=sleep, clock=clock)
    finally:
        _ACTIVE_DIAGNOSTIC.reset(token)


def _present(request: Any, *, ctx: Any, sleep: Callable[[float], None],
             clock: Callable[[], float]) -> Any:
    LAST.update(failure=None, request_id=None, diagnostic=None)
    if ctx is None:
        return _fail(request, "no_context")
    command = str(request.command or "")
    if len(command) > DETAILS_CAP:
        return _fail(request, "too_long")  # no card is created
    head = command.splitlines()[0] if command else ""
    description = str(getattr(request, "description", "") or "")
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    work = _work_context(cwd)
    origin = _origin(cwd)
    title = f"Autoriser : {description or head}"[:TITLE_CAP]
    card = {
        "title": title,
        "details": _details(command, description, origin, work),
        "risk": _risk(request.pattern_key, command, description),
        "consequence": ("Approve = la commande s'exécute UNE seule fois dans cette session, puis l'agent reprend son travail. "
                        "Deny = rien ne s'exécute ; l'agent est bloqué et doit passer par une autre voie."),
        "prohibitions": ["Pas d'approbation de session ni permanente : une seule exécution",
                         "Rien d'autre que cette commande exacte"],
        "external_id": f"hermes-approval-{request.digest[:24]}",
        # Card API bounds, not the host's decision deadline (which stays unchanged).
        "expires_in_seconds": max(900, min(2592000, int(request.timeout_seconds) + 60)),
        "agent_name": f"hermes@{os.environ.get('HERDR_PANE_ID', 'local')}",
        "runtime": "hermes",
    }
    if work.get("repo"):
        card["repo"] = work["repo"][:REPO_CAP]
    if work.get("worktree"):
        card["worktree"] = work["worktree"][:WORKTREE_CAP]
    if work.get("ticket"):
        card["ticket"] = work["ticket"][:TICKET_CAP]
    created = _call_mcp(request, ctx, "request_approval", card)
    request_id = str(created.get("request_id") or "")
    if not request_id:
        return _fail(request, "no_request_id")
    LAST["request_id"] = request_id  # a `duplicate: true` reply names the open card: poll it, no twin
    _persist("request_approval", "created", paraphe_request_id=request_id)
    deadline = clock() + max(float(request.timeout_seconds), 0.0)
    while True:
        state = _call_mcp(request, ctx, "get_response", {"request_id": request_id})
        if state.get("status") in ("cancelled", "expired"):
            return _fail(request, f"card_{state['status']}")
        if not state.get("pending", True) or state.get("status") in ("answered", "acknowledged"):
            response = state.get("response")
            if not isinstance(response, dict):
                return _fail(request, "invalid_response")
            choice = response.get("choice")
            if choice == "Approve":
                LAST["failure"] = None
                _persist("present", "owner_approve")
                return request.respond("once")
            if choice == "Deny":
                LAST["failure"] = "owner_deny"
                _persist("present", "owner_deny")
                return request.respond("deny")
            return _fail(request, "invalid_response")
        if clock() >= deadline:
            try:
                ctx.call_mcp(SERVER, "cancel_request", {"request_id": request_id, "reason": "resolved_elsewhere"})
            except Exception:  # best effort; the host deadline has passed either way
                pass
            return _fail(request, "timeout")
        sleep(POLL_S)
