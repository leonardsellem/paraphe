"""Inbox seam: fail-closed setup, store path, and MCP card lifecycle."""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, fields as dataclass_fields
from pathlib import Path
from typing import Any, Callable, Mapping

from .card import Card
from .claim import ClaimRefused, FakeTelegramPort, NullTelegramPort
from .config import (
    DEFAULT_TTL_SECONDS,
    FLOOR_TTL_SECONDS,
    MAX_TTL_SECONDS,
    SetupError,
    load_settings,
)
from .http import BindError
from .store import Store, default_store_path

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "FLOOR_TTL_SECONDS",
    "BindError",
    "ClaimRefused",
    "FakeTelegramPort",
    "Inbox",
    "InboxError",
    "NotifyRejected",
    "default_store_path",
    "SetupError",
    "TOOL_NAMES",
]

TOOL_NAMES = [
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

# The tools whose `wait_seconds` parks the call until the owner answers or the
# window ends. The window is per call and never sleeps past it.
WAIT_TOOLS = frozenset({"ask_question", "request_approval", "get_response"})

ASK_ONLY = frozenset({"question", "context", "choices", "choice_notes", "allow_freeform"})
APPROVAL_ONLY = frozenset({"title", "details"})
FEEDBACK_ONLY = frozenset({"title", "details"})
NOTIFY_FIELDS = frozenset({"title", "message", "result"})
SHARED_CREATE = frozenset(
    {
        "priority",
        "agent_name",
        "wait_seconds",
        "external_id",
        "project",
        "source_thread",
        "links",
        "risk",
        "consequence",
        "rule_key",
        "recommendation",
        "prohibitions",
        "expires_in_seconds",
    }
)
# Provenance travels with the ask — taught, optional, never guessed. It rides
# the asking tools and update_request; the card renders it as the identity line.
PROVENANCE = frozenset({"runtime", "repo", "worktree", "ticket"})
UPDATE_FIELDS = frozenset(
    {
        "request_id",
        "expected_version",
        "title",
        "details",
        "choices",
        "choice_notes",
        "priority",
        "renotify",
        "external_id",
        "project",
        "source_thread",
        "links",
        "risk",
        "consequence",
        "rule_key",
        "recommendation",
        "prohibitions",
        "expires_in_seconds",
        "runtime",
        "repo",
        "worktree",
        "ticket",
    }
)
RISK_VALUES = frozenset({"low", "medium", "high", "critical"})
PRIORITY_VALUES = frozenset({"low", "normal", "high", "urgent"})
REPORT_OUTCOMES = frozenset({"accepted", "rejected", "completed", "failed"})
CANCEL_REASONS = frozenset({"cancelled", "resolved_elsewhere"})
RESULT_OUTCOMES = frozenset({"success", "failure", "partial"})

_LIMITS = {
    "question": 200,
    "title": 200,
    "context": 2000,
    "details": 2000,
    "message": 2000,
    "recommendation": 1000,
    "consequence": 1000,
    "note": 1000,
    "project": 120,
    "source_thread": 200,
    "agent_name": 60,
    "external_id": 200,
    "runtime": 40,
    "repo": 120,
    "worktree": 120,
    "ticket": 200,
    "rule_key": 200,
    "commit_message": 500,
}


def _field_schema(name: str) -> dict[str, Any]:
    if name in _LIMITS:
        return {"type": "string", "maxLength": _LIMITS[name]}
    if name == "choices":
        return {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 40}}
    if name == "choice_notes":
        return {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 120}}
    if name == "links":
        return {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}}
    if name == "prohibitions":
        return {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 200}}
    if name in {"allow_freeform", "renotify"}:
        return {"type": "boolean"}
    if name == "wait_seconds":
        return {"type": "number", "minimum": 0, "maximum": 60}
    if name == "expires_in_seconds":
        return {"type": "integer", "minimum": FLOOR_TTL_SECONDS, "maximum": MAX_TTL_SECONDS}
    if name == "expected_version":
        return {"type": "integer", "minimum": 1}
    if name == "request_id":
        return {"type": "string"}
    if name == "risk":
        return {"type": "string", "enum": sorted(RISK_VALUES)}
    if name == "priority":
        return {"type": "string", "enum": sorted(PRIORITY_VALUES)}
    if name == "outcome":
        return {"type": "string", "enum": sorted(REPORT_OUTCOMES)}
    if name == "reason":
        return {"type": "string", "enum": sorted(CANCEL_REASONS)}
    if name == "result":
        return {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": sorted(RESULT_OUTCOMES)},
                "files_changed": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {"type": "string", "maxLength": 300},
                },
                "tests_passed": {"type": "integer", "minimum": 0},
                "tests_failed": {"type": "integer", "minimum": 0},
                "commit_message": {"type": "string", "maxLength": 500},
                "duration_seconds": {"type": "integer", "minimum": 0},
            },
        }
    raise KeyError(name)


def _schema(fields: set[str] | frozenset[str], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {name: _field_schema(name) for name in sorted(fields)},
    }
    if required:
        schema["required"] = required
    return schema


TOOL_SCHEMAS = {
    "how_to_use": _schema(set()),
    "ask_question": _schema(ASK_ONLY | SHARED_CREATE | PROVENANCE, ["question"]),
    "request_approval": _schema(APPROVAL_ONLY | SHARED_CREATE | PROVENANCE, ["title"]),
    "get_response": _schema({"request_id", "wait_seconds"}, ["request_id"]),
    "list_unprocessed": _schema(set()),
    "list_pending": _schema(set()),
    "mark_processed": _schema({"request_id"}, ["request_id"]),
    "report_execution": _schema({"request_id", "outcome", "note", "result"}, ["request_id", "outcome"]),
    "update_request": _schema(UPDATE_FIELDS, ["request_id", "expected_version"]),
    "cancel_request": _schema({"request_id", "reason"}, ["request_id"]),
    "notify_user": _schema(NOTIFY_FIELDS | SHARED_CREATE, ["title"]),
    "request_feedback": _schema(FEEDBACK_ONLY | SHARED_CREATE | PROVENANCE, ["title"]),
}

# The served text: what a client that reads only `tools/list` learns. The
# asking tools carry "record the request id"; the reading tools carry the
# drain rule; the waiting parameter is a bounded window.
TOOL_DESCRIPTIONS = {
    "how_to_use": (
        "Read this first: the card lifecycle, the credential boundary and the "
        "async return protocol."
    ),
    "ask_question": (
        "Ask the owner a question. Compose it purpose first — why the ask "
        "exists before the action detail — with the origin stated and the "
        "tap semantics plain: what the answer authorises and what it does "
        "not (consequence, prohibitions). Record the returned request_id; "
        "read the answer with get_response, or pass wait_seconds (0-60) to "
        "let the call park until the answer arrives. Pass "
        "runtime/repo/worktree/ticket (optional) so the card shows where "
        "the ask comes from."
    ),
    "request_approval": (
        "Ask the owner to approve. Compose it purpose first — why the "
        "approval is needed before the action detail — and spell the tap "
        "semantics: what Approve authorises (consequence) and what it does "
        "not (prohibitions). Record the returned request_id; read the "
        "answer with get_response, or pass wait_seconds (0-60) to let the "
        "call park until the answer arrives. Pass runtime/repo/worktree/ticket "
        "(optional) so the card shows where the ask comes from."
    ),
    "get_response": (
        "Read a card; the answer is nested at response.choice and is not "
        "consumed. wait_seconds (0-60) parks the call until the card is "
        "answered or the window ends. An owner reply arrives as response.text "
        "with responded_via telegram-reply. Drain the answers that are yours "
        "when you next run."
    ),
    "list_unprocessed": (
        "The tapped answers not yet marked processed: drain them when you "
        "next run."
    ),
    "list_pending": "The still-open cards, waiting on the owner's answer.",
    "mark_processed": "Idempotent closeout: the agent has acted on the answer.",
    "report_execution": (
        "Record the outcome of a tapped approval: accepted, rejected, "
        "completed or failed."
    ),
    "update_request": (
        "Revise an open card in place; expected_version guards a stale "
        "writer and bumps version. Pass runtime/repo/worktree/ticket "
        "(optional) so the card shows where the ask comes from."
    ),
    "cancel_request": "Withdraw an open card; reason cancelled or resolved_elsewhere.",
    "notify_user": "Status to the owner, never a decision card.",
    "request_feedback": (
        "Ask the owner for feedback; a create like the other asking tools. "
        "Purpose first — why the feedback is needed before the detail — "
        "with the tap semantics plain. Pass runtime/repo/worktree/ticket "
        "(optional) so the card shows where the ask comes from."
    ),
}


class InboxError(Exception):
    """Invalid MCP lifecycle call."""


class NotifyRejected(Exception):
    """Provider response proved the send did not happen. Reservation may roll back."""


class _NullNotifier:
    def notify(self, payload: object) -> None:
        return None


class Inbox:
    def __init__(
        self,
        *,
        owner_telegram_id: int,
        default_ttl_seconds: int,
        floor_ttl_seconds: int,
        store_path: Path | str | None = None,
        mcp_create_bearer: str = "",
        bot_token: str = "",
        owner_answer_token: str = "",
        notifier: Any | None = None,
        clock: Callable[[], float] | None = None,
        telegram_port: Any | None = None,
    ) -> None:
        self.owner_telegram_id = owner_telegram_id
        self.default_ttl_seconds = default_ttl_seconds
        self.floor_ttl_seconds = floor_ttl_seconds
        self.store_path = Path(store_path) if store_path else default_store_path()
        self._mcp_create_bearer = mcp_create_bearer
        self._owner_answer_token = owner_answer_token
        self._bot_token = bot_token
        self._notifier = notifier if notifier is not None else _NullNotifier()
        self._telegram = telegram_port if telegram_port is not None else NullTelegramPort()
        self._clock = clock if clock is not None else time.time
        self._cards: dict[str, Card] = {}
        self._by_external_id: dict[str, str] = {}
        self._notify_external_ids: set[str] = set()
        self._store: Store | None = None
        # ponytail: inbox-wide RLock, per-card locks if throughput matters
        self._lock = threading.RLock()
        # Parked clients by request id; the answer claim sets their events.
        self._waiters: dict[str, list[threading.Event]] = {}
        self._wait_lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            f"Inbox(owner_telegram_id={self.owner_telegram_id!r}, "
            f"default_ttl_seconds={self.default_ttl_seconds!r}, "
            f"floor_ttl_seconds={self.floor_ttl_seconds!r}, "
            f"store_path={self.store_path!r})"
        )

    @classmethod
    def start(
        cls,
        config_path: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        store_path: Path | str | None = None,
    ) -> "Inbox":
        settings = load_settings(
            config_path=config_path,
            environ=os.environ if environ is None else environ,
        )
        return cls(
            owner_telegram_id=settings.owner_telegram_id,
            default_ttl_seconds=settings.default_ttl_seconds,
            floor_ttl_seconds=settings.floor_ttl_seconds,
            store_path=settings.store_path if store_path is None else Path(store_path),
            mcp_create_bearer=settings.mcp_create_bearer,
            owner_answer_token=settings.owner_answer_token,
            bot_token=settings.bot_token,
        )

    def prepare_store(self, path: Path | str | None = None) -> Path:
        return Store.prepare(self.store_path if path is None else path)

    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    def reconcile_notifications(self) -> None:
        with self._lock:
            self._ensure_store()
            cards = list(self._cards.values())
            remember = getattr(self._telegram, "remember_message", None)
            if remember is not None:
                for card in cards:
                    if card.telegram_chat_id is not None and card.telegram_message_id is not None:
                        remember(
                            card.request_id,
                            card.version,
                            card.telegram_chat_id,
                            card.telegram_message_id,
                        )
            for card in cards:
                if card.state == "open":
                    refreshed = self._refresh_expiry(card)
                    if refreshed.state == "open":
                        if (
                            refreshed.telegram_chat_id is not None
                            and refreshed.telegram_message_id is not None
                            and not refreshed.telegram_keyboard_attached
                        ):
                            self._finalize_telegram(refreshed)
                        else:
                            self._notify_owner(refreshed)
                else:
                    self._request_strip(card)

    def record_telegram_message(
        self,
        request_id: str,
        *,
        version: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        with self._lock:
            card = self._require_card(request_id)
            if version != card.version:
                raise InboxError("telegram message version does not match")
            if chat_id != self.owner_telegram_id or message_id <= 0:
                raise InboxError("telegram message identity is invalid")
            updated = deepcopy(card)
            updated.telegram_chat_id = chat_id
            updated.telegram_message_id = message_id
            updated.telegram_keyboard_attached = False
            self._save(updated)

    def record_telegram_keyboard_attached(
        self,
        request_id: str,
        *,
        version: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        with self._lock:
            card = self._require_card(request_id)
            if card.state != "open":
                raise InboxError("card is not open")
            if (
                version != card.version
                or chat_id != card.telegram_chat_id
                or message_id != card.telegram_message_id
            ):
                raise InboxError("telegram message identity does not match")
            updated = deepcopy(card)
            updated.telegram_keyboard_attached = True
            self._save(updated)

    def list_tools(self) -> list[str]:
        return list(TOOL_NAMES)

    def list_tool_descriptors(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "inputSchema": TOOL_SCHEMAS[name],
            }
            for name in TOOL_NAMES
        ]

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        bearer: str | None = None,
    ) -> Any:
        with self._lock:
            args = dict(arguments or {})
            if name not in TOOL_NAMES:
                raise InboxError("unknown tool")
            handlers = {
                "how_to_use": self._how_to_use,
                "ask_question": self._ask_question,
                "request_approval": self._request_approval,
                "get_response": self._get_response,
                "list_unprocessed": self._list_unprocessed,
                "list_pending": self._list_pending,
                "mark_processed": self._mark_processed,
                "report_execution": self._report_execution,
                "update_request": self._update_request,
                "cancel_request": self._cancel_request,
                "notify_user": self._notify_user,
                "request_feedback": self._request_feedback,
            }
            result = handlers[name](args, bearer=bearer)
        # The wait parks outside the lock: a parked call must not block the
        # inbox, and the handler above already validated `wait_seconds`.
        if name in WAIT_TOOLS and isinstance(result, dict) and "request_id" in result:
            wait_seconds = self._opt_wait(args) or 0.0
            if wait_seconds > 0:
                return self._envelope(self._park(result["request_id"], wait_seconds))
        return result

    def record_tap(
        self,
        request_id: str,
        *,
        choice: str | None = None,
        text: str | None = None,
        responded_via: str = "app",
    ) -> None:
        with self._lock:
            card = self._require_card(request_id)
            if card.state != "open":
                raise InboxError("card is not open")
            now = self._iso_now()
            updated = deepcopy(card)
            updated.state = "tapped"
            updated.response_choice = choice
            updated.response_text = text
            updated.responded_at = now
            updated.responded_via = responded_via
            self._save(updated)

    def claim(
        self,
        card_id: str,
        *,
        version: int,
        from_id: int | None = None,
        choice: str | None = None,
        choice_index: int | None = None,
        text: str | None = None,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
        responded_via: str = "telegram",
        owner_verified: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if not owner_verified and from_id != self.owner_telegram_id:
                raise ClaimRefused("not_owner")
            self._ensure_store()
            card = self._cards.get(card_id)
            if card is None:
                raise ClaimRefused("unknown")
            return self._claim_locked(
                card,
                version=version,
                choice=choice,
                choice_index=choice_index,
                text=text,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id,
                responded_via=responded_via,
            )

    def claim_reply(
        self,
        *,
        from_id: int | None,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> dict[str, Any]:
        """An owner reply to a card message is that card's answer.

        Resolution is by the replied-to message id over the loaded card
        state, so it survives a restart. The refusals mirror the tap path:
        an unknown or closed card records nothing.
        """
        with self._lock:
            if from_id != self.owner_telegram_id or chat_id != self.owner_telegram_id:
                raise ClaimRefused("not_owner")
            self._ensure_store()
            card = self._card_by_telegram_message(chat_id, message_id)
            if card is None:
                raise ClaimRefused("unknown")
            return self._claim_locked(
                card,
                version=card.version,
                text=text,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                responded_via="telegram-reply",
            )

    def _card_by_telegram_message(self, chat_id: int, message_id: int) -> Card | None:
        for card in self._cards.values():
            if card.telegram_chat_id == chat_id and card.telegram_message_id == message_id:
                return card
        return None

    def _claim_locked(
        self,
        card: Card,
        *,
        version: int,
        choice: str | None = None,
        choice_index: int | None = None,
        text: str | None = None,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
        responded_via: str = "telegram",
    ) -> dict[str, Any]:
        was_open = card.state == "open"
        card = self._refresh_expiry(card)
        if version != card.version:
            raise ClaimRefused("stale_version")
        if card.state != "open":
            if not (was_open and card.state == "expired"):
                self._request_strip(card)
            reasons = {
                "expired": "expired",
                "cancelled": "cancelled",
                "tapped": "already_tapped",
            }
            raise ClaimRefused(reasons.get(card.state, card.state))
        if choice_index is not None:
            choices = card.choices or ["Approve", "Deny"]
            if (
                choice is not None
                or isinstance(choice_index, bool)
                or not isinstance(choice_index, int)
                or not 0 <= choice_index < len(choices)
            ):
                raise ClaimRefused("invalid_choice")
            choice = choices[choice_index]
        if text is not None and (
            choice is not None
            or choice_index is not None
            or not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-16-le")) // 2 > 4096
        ):
            raise ClaimRefused("invalid_answer")
        if (telegram_chat_id is None) != (telegram_message_id is None):
            raise ClaimRefused("invalid_message")
        if telegram_chat_id is not None and (
            telegram_chat_id != self.owner_telegram_id
            or isinstance(telegram_message_id, bool)
            or not isinstance(telegram_message_id, int)
            or telegram_message_id <= 0
        ):
            raise ClaimRefused("invalid_message")
        current_message = (card.telegram_chat_id, card.telegram_message_id)
        if (
            telegram_chat_id is not None
            and current_message != (None, None)
            and (telegram_chat_id, telegram_message_id) != current_message
        ):
            raise ClaimRefused("stale_message")
        updated = deepcopy(card)
        updated.state = "tapped"
        updated.response_choice = choice
        if text is not None:
            updated.response_text = text
        if telegram_chat_id is not None and telegram_message_id is not None:
            updated.telegram_chat_id = telegram_chat_id
            updated.telegram_message_id = telegram_message_id
            updated.telegram_keyboard_attached = True
        updated.responded_at = self._iso_now()
        updated.responded_via = responded_via
        self._save(updated)
        self._notify_waiters(updated.request_id)
        if telegram_chat_id is not None and telegram_message_id is not None:
            remember = getattr(self._telegram, "remember_message", None)
            if remember is not None:
                remember(
                    updated.request_id,
                    updated.version,
                    telegram_chat_id,
                    telegram_message_id,
                )
        self._request_strip(updated)
        return self._envelope(updated)

    def _request_strip(self, card: Card) -> None:
        try:
            self._telegram.edit_and_strip(card.request_id, card.version)
        except Exception:
            return

    def _park(self, request_id: str, wait_seconds: float) -> Card:
        """Block until the card leaves `open`, or the window ends.

        Runs outside ``self._lock``: parked calls must not block the inbox.
        The card is re-checked after the waiter registers, so an answer that
        landed just before the registration is not missed.
        """
        deadline = time.monotonic() + wait_seconds
        while True:
            card = self._require_card(request_id)
            if card.state != "open":
                return card
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return card
            event = self._register_waiter(request_id)
            try:
                card = self._require_card(request_id)
                if card.state != "open":
                    return card
                event.wait(remaining)
            finally:
                self._unregister_waiter(request_id, event)

    def _register_waiter(self, request_id: str) -> threading.Event:
        event = threading.Event()
        with self._wait_lock:
            self._waiters.setdefault(request_id, []).append(event)
        return event

    def _unregister_waiter(self, request_id: str, event: threading.Event) -> None:
        with self._wait_lock:
            waiters = self._waiters.get(request_id)
            if waiters is None:
                return
            try:
                waiters.remove(event)
            except ValueError:
                return
            if not waiters:
                del self._waiters[request_id]

    def _notify_waiters(self, request_id: str) -> None:
        """Set the parked calls' events. Called after the durable write, never before."""
        with self._wait_lock:
            waiters = list(self._waiters.get(request_id, ()))
        for event in waiters:
            event.set()

    def serve(self, host: str, port: int = 0) -> Any:
        from .http import serve_inbox

        return serve_inbox(self, host, port)

    def check_bearer(self, bearer: str | None) -> bool:
        expected = self._mcp_create_bearer
        if not expected or bearer is None:
            return False
        return secrets.compare_digest(str(bearer), expected)

    def _require_create_bearer(self, bearer: str | None) -> None:
        if not self.check_bearer(bearer):
            raise InboxError("create bearer is required")

    def check_answer_token(self, token: str | None) -> bool:
        """The owner identity for the local answer path, never the create bearer."""
        expected = self._owner_answer_token
        if not expected or token is None:
            return False
        if token == self._mcp_create_bearer:
            return False
        return secrets.compare_digest(str(token), expected)

    def answer(
        self,
        request_id: str,
        *,
        version: int,
        choice: str | None = None,
        responded_via: str = "owner",
    ) -> dict[str, Any]:
        """Owner-only answer path. The credential is checked at the boundary.

        It routes through `claim`, so the owner and version checks and the
        refusal reasons are the same ones the tap path uses. It never uses the
        recording shortcut.
        """
        return self.claim(
            request_id,
            version=version,
            from_id=self.owner_telegram_id,
            choice=choice,
            responded_via=responded_via,
            owner_verified=True,
        )

    def _how_to_use(self, args: dict[str, Any], *, bearer: str | None) -> str:
        _ = args, bearer
        return (
            "Call this first. Paraphe is the owner decision inbox: an agent "
            "raises a decision, the owner answers it, the agent resumes.\n"
            "Tools: "
            + ", ".join(TOOL_NAMES)
            + ". Their fields and lifecycle are documented in docs/tools.md.\n"
            "Create with the shared MCP bearer and a stable external_id. "
            "Duplicate creates return the existing item and do not notify again.\n"
            "ask_question uses question/context/choices. "
            "request_approval uses title/details. Mixing those names fails.\n"
            "Record the request_id a create returns. ask_question, "
            "request_approval and get_response accept wait_seconds (0-60): the "
            "call parks until the owner answers inside that window or the "
            "window ends, then returns the answer envelope.\n"
            "The owner answers on the configured destination; read with "
            "get_response, where the answer is nested at response.choice and "
            "is not consumed. When you next run, drain the answers that are "
            "yours with list_unprocessed.\n"
            "On the phone a card renders as ordered rich sections: an "
            "identity line (agent, runtime, repository, worktree, ticket), a "
            "bold title, the context, numbered choices with one-line notes, "
            "what is recommended, limits and links, a reply hint and the "
            "expiry. Pass runtime/repo/worktree/ticket on the asking tools or "
            "update_request (optional, bounded, never guessed); a ticket that "
            "is a URL renders as a link.\n"
            "Compose every card to carry its origin and its purpose first: "
            "say who is asking and where it runs (agent_name and "
            "runtime/repo/worktree/ticket), lead with the purpose — why the "
            "action is needed — before the action detail (question/title "
            "carry the purpose; context/details open with the why, then the "
            "exact command or change), and spell the tap semantics: what "
            "the answer authorises (consequence) and what it does not "
            "(prohibitions). On an approval, say what Approve does and what "
            "Deny does.\n"
            "The owner may answer a card with a reply instead of a tap: the "
            "reply text arrives as response.text with responded_via "
            "telegram-reply, and the card closes exactly as a tap. A reply "
            "that is a question or not a decision is not a decision: do not "
            "execute it — explain, then re-ask with a fresh card.\n"
            "If you can background a command, `paraphe wait <request_id>` "
            "blocks until the card is answered or can no longer be answered, "
            "and prints the answer: exit 0 answered, 3 expired or not "
            "answerable, 4 unknown. A session ending with an open card "
            "records the request_id and its continuation for the next run.\n"
            "This credential creates and reads. It cannot answer a decision.\n"
            "update_request requires expected_version and bumps version.\n"
            "report_execution outcomes: accepted, rejected, completed, failed.\n"
            "cancel_request reasons: cancelled, resolved_elsewhere.\n"
            "mark_processed is idempotent. Reads do not consume answers.\n"
            "notify_user is status and never a decision card.\n"
            "rule_key is accepted and ignored. Always-allow is not product.\n"
            "Unknown parameters are rejected. Send exactly the documented fields.\n"
        )

    def _ask_question(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        self._require_create_bearer(bearer)
        self._reject_unknown(args, ASK_ONLY | SHARED_CREATE | PROVENANCE)
        self._reject_keys(args, APPROVAL_ONLY)
        question = self._require_str(args, "question", required=True)
        choices = self._opt_choices(args)
        return self._create_card(
            kind="question",
            args=args,
            question=question,
            context=self._opt_str(args, "context"),
            choices=choices,
            choice_notes=self._opt_choice_notes(args, choices),
            allow_freeform=args.get("allow_freeform"),
        )

    def _request_approval(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        self._require_create_bearer(bearer)
        self._reject_unknown(args, APPROVAL_ONLY | SHARED_CREATE | PROVENANCE)
        self._reject_keys(args, ASK_ONLY)
        title = self._require_str(args, "title", required=True)
        return self._create_card(
            kind="approval",
            args=args,
            title=title,
            details=self._opt_str(args, "details"),
        )

    def _request_feedback(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        self._require_create_bearer(bearer)
        self._reject_unknown(args, FEEDBACK_ONLY | SHARED_CREATE | {"wait_seconds"} | PROVENANCE)
        self._reject_keys(args, {"question", "context", "choices", "allow_freeform"})
        title = self._require_str(args, "title", required=True)
        return self._create_card(
            kind="feedback",
            args=args,
            title=title,
            details=self._opt_str(args, "details"),
        )

    def _notify_user(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        self._require_create_bearer(bearer)
        self._reject_unknown(args, NOTIFY_FIELDS | SHARED_CREATE)
        self._reject_keys(args, ASK_ONLY | {"details"})
        title = self._require_str(args, "title", required=True)
        message = self._opt_str(args, "message")
        if "result" in args:
            self._validate_result(args["result"])
        shared = self._opt_shared(args, require_external=False)
        external_id = shared["external_id"]
        store = self._ensure_store()
        if external_id and external_id in self._notify_external_ids:
            return {"ok": True, "duplicate": True, "kind": "notify"}
        if external_id:
            store.save_notification_id(external_id)
            self._notify_external_ids.add(external_id)
        try:
            self._notifier.notify(
                {
                    "kind": "notify",
                    "title": title,
                    "message": message,
                    "agent_name": shared["agent_name"],
                    "risk": shared["risk"],
                }
            )
        except Exception as exc:
            if external_id and isinstance(exc, NotifyRejected):
                self._notify_external_ids.discard(external_id)
                try:
                    store.delete_notification_id(external_id)
                except Exception:
                    # ponytail: leftover reservation fail-closes after restart
                    pass
            raise
        return {"ok": True, "duplicate": False, "kind": "notify"}

    def _get_response(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        _ = bearer
        self._reject_unknown(args, {"request_id", "wait_seconds"})
        request_id = self._require_str(args, "request_id", required=True)
        self._opt_wait(args)
        return self._envelope(self._require_card(request_id))

    def _list_unprocessed(self, args: dict[str, Any], *, bearer: str | None) -> list[dict[str, Any]]:
        _ = bearer
        self._reject_unknown(args, set())
        self._ensure_store()
        return [
            self._envelope(self._refresh_expiry(card))
            for card in self._cards.values()
            if card.state == "tapped" and card.processed_at is None
        ]

    def _list_pending(self, args: dict[str, Any], *, bearer: str | None) -> list[dict[str, Any]]:
        _ = bearer
        self._reject_unknown(args, set())
        self._ensure_store()
        return [
            self._envelope(card)
            for card in reversed(list(self._cards.values()))
            if self._refresh_expiry(card).state == "open"
        ]

    def _mark_processed(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        _ = bearer
        self._reject_unknown(args, {"request_id"})
        request_id = self._require_str(args, "request_id", required=True)
        card = self._require_card(request_id)
        if card.processed_at is None:
            if card.state != "tapped":
                raise InboxError("card is not tapped")
            updated = deepcopy(card)
            updated.processed_at = self._iso_now()
            self._save(updated)
            card = updated
        return self._envelope(card)

    def _report_execution(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        _ = bearer
        self._reject_unknown(args, {"request_id", "outcome", "note", "result"})
        request_id = self._require_str(args, "request_id", required=True)
        outcome = args.get("outcome")
        if outcome not in REPORT_OUTCOMES:
            raise InboxError("outcome is invalid")
        card = self._require_card(request_id)
        if card.kind != "approval":
            raise InboxError("report_execution is approval-kind")
        if card.state != "tapped":
            raise InboxError("card is not tapped")
        updated = deepcopy(card)
        if "note" in args:
            updated.execution_note = self._require_str(args, "note")
        if "result" in args:
            self._validate_result(args["result"])
            updated.execution_result = args["result"]
        updated.execution_status = str(outcome)
        self._save(updated)
        return self._envelope(updated)

    def _update_request(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        _ = bearer
        self._reject_unknown(args, UPDATE_FIELDS)
        request_id = self._require_str(args, "request_id", required=True)
        expected = args.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise InboxError("expected_version is invalid")
        card = self._require_card(request_id)
        if card.state != "open":
            raise InboxError("card is not open")
        if expected != card.version:
            raise InboxError("expected_version does not match")
        if card.kind == "question":
            self._reject_keys(args, APPROVAL_ONLY)
        elif card.kind == "approval":
            self._reject_keys(args, ASK_ONLY)
        original = deepcopy(card)
        updated = deepcopy(card)
        if "title" in args:
            updated.title = self._require_str(args, "title")
        if "details" in args:
            updated.details = self._require_str(args, "details")
        if "choices" in args:
            updated.choices = self._opt_choices(args)
        if "choice_notes" in args:
            updated.choice_notes = self._opt_choice_notes(args, updated.choices)
        elif "choices" in args:
            # New choices invalidate notes that were written for the old ones.
            updated.choice_notes = []
        self._apply_shared(updated, args)
        if args.get("expires_in_seconds") is not None:
            updated.expires_in_seconds = self._ttl(args)
            updated.expires_at = self._clock() + updated.expires_in_seconds
        renotify = args.get("renotify") is True
        if renotify:
            updated.notified = False
        updated.telegram_chat_id = None
        updated.telegram_message_id = None
        updated.telegram_keyboard_attached = False
        updated.version += 1
        self._save(updated)
        if original.telegram_chat_id is not None and original.telegram_message_id is not None:
            try:
                self._telegram.edit_and_strip(original.request_id, original.version)
            except Exception:
                self._save(original)
                raise
        if renotify:
            self._notify_owner(updated, renotify=True)
        return self._create_view(updated, duplicate=False)

    def _cancel_request(self, args: dict[str, Any], *, bearer: str | None) -> dict[str, Any]:
        _ = bearer
        self._reject_unknown(args, {"request_id", "reason"})
        request_id = self._require_str(args, "request_id", required=True)
        reason = args.get("reason", "cancelled")
        if reason not in CANCEL_REASONS:
            raise InboxError("reason is invalid")
        card = self._require_card(request_id)
        if card.state == "open":
            updated = deepcopy(card)
            updated.state = "cancelled"
            updated.cancel_reason = str(reason)
            self._save(updated)
            self._notify_waiters(updated.request_id)
            self._request_strip(updated)
            card = updated
        return self._envelope(card)

    def _create_card(self, *, kind: str, args: dict[str, Any], **fields: Any) -> dict[str, Any]:
        self._ensure_store()
        shared = self._opt_shared(args, require_external=True)
        external_id = shared["external_id"]
        existing_id = self._by_external_id.get(external_id)
        if existing_id is not None:
            existing = self._refresh_expiry(self._cards[existing_id])
            if existing.kind != kind:
                raise InboxError("external_id kind mismatch")
            self._notify_owner(existing)
            return self._create_view(existing, duplicate=True)
        ttl = self._ttl(args)
        now = self._clock()
        allow_freeform = fields.pop("allow_freeform", None)
        if allow_freeform is not None and not isinstance(allow_freeform, bool):
            raise InboxError("allow_freeform is invalid")
        card = Card(
            request_id=str(uuid.uuid4()),
            kind=kind,
            external_id=external_id,
            expires_in_seconds=ttl,
            expires_at=now + ttl,
            allow_freeform=allow_freeform,
            **fields,
            **{k: v for k, v in shared.items() if k != "external_id" and k != "wait_seconds"},
        )
        self._save(card)
        self._notify_owner(card)
        return self._create_view(card, duplicate=False)

    def _opt_shared(self, args: dict[str, Any], *, require_external: bool) -> dict[str, Any]:
        self._opt_wait(args)
        external_id = self._opt_str(args, "external_id")
        if require_external and not external_id:
            raise InboxError("external_id is required")
        risk = args.get("risk", "medium")
        if risk not in RISK_VALUES:
            raise InboxError("risk is invalid")
        priority = args.get("priority", "normal")
        if priority not in PRIORITY_VALUES:
            raise InboxError("priority is invalid")
        links = args.get("links", [])
        if links:
            links = self._opt_links(args)
        prohibitions = args.get("prohibitions", [])
        if prohibitions:
            prohibitions = self._opt_str_list(args, "prohibitions", max_items=8, max_len=200)
        # rule_key accepted and ignored
        if "rule_key" in args:
            self._require_str(args, "rule_key")
        return {
            "external_id": external_id,
            "risk": risk,
            "priority": priority,
            "project": self._opt_str(args, "project"),
            "source_thread": self._opt_str(args, "source_thread"),
            "links": links or [],
            "recommendation": self._opt_str(args, "recommendation"),
            "consequence": self._opt_str(args, "consequence"),
            "prohibitions": prohibitions or [],
            "agent_name": self._opt_str(args, "agent_name"),
            "runtime": self._opt_str(args, "runtime"),
            "repo": self._opt_str(args, "repo"),
            "worktree": self._opt_str(args, "worktree"),
            "ticket": self._opt_str(args, "ticket"),
        }

    def _apply_shared(self, card: Card, args: dict[str, Any]) -> None:
        if "risk" in args:
            if args["risk"] not in RISK_VALUES:
                raise InboxError("risk is invalid")
            card.risk = str(args["risk"])
        if "priority" in args:
            if args["priority"] not in PRIORITY_VALUES:
                raise InboxError("priority is invalid")
            card.priority = str(args["priority"])
        if "project" in args:
            card.project = self._require_str(args, "project")
        if "source_thread" in args:
            card.source_thread = self._require_str(args, "source_thread")
        if "links" in args:
            card.links = self._opt_links(args)
        if "recommendation" in args:
            card.recommendation = self._require_str(args, "recommendation")
        if "consequence" in args:
            card.consequence = self._require_str(args, "consequence")
        if "prohibitions" in args:
            card.prohibitions = self._opt_str_list(args, "prohibitions", max_items=8, max_len=200)
        if "rule_key" in args:
            self._require_str(args, "rule_key")
        if "external_id" in args:
            self._require_str(args, "external_id")
        for key in ("runtime", "repo", "worktree", "ticket"):
            if key in args:
                setattr(card, key, self._require_str(args, key))

    def _ttl(self, args: dict[str, Any]) -> int:
        raw = args.get("expires_in_seconds")
        if raw is None:
            return self.default_ttl_seconds
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InboxError("expires_in_seconds is invalid")
        if raw < self.floor_ttl_seconds:
            raise InboxError("expires_in_seconds is below floor")
        if raw > MAX_TTL_SECONDS:
            raise InboxError("expires_in_seconds is above max")
        return raw

    def _create_view(self, card: Card, *, duplicate: bool) -> dict[str, Any]:
        return {
            "request_id": card.request_id,
            "version": card.version,
            "duplicate": duplicate,
            "pending": card.state == "open",
            "status": self._status(card),
            "kind": card.kind,
            "read_with": "get_response",
        }

    def _envelope(self, card: Card) -> dict[str, Any]:
        response = None
        if card.state == "tapped" or card.response_choice is not None or card.response_text is not None:
            response = {
                "choice": card.response_choice,
                "text": card.response_text,
                "responded_at": card.responded_at,
                "responded_via": card.responded_via,
            }
        return {
            "request_id": card.request_id,
            "status": self._status(card),
            "version": card.version,
            "response": response,
            "processed_at": card.processed_at,
            "execution_status": card.execution_status,
            "kind": card.kind,
            "pending": card.state == "open",
        }

    def _status(self, card: Card) -> str:
        if card.processed_at is not None:
            return "acknowledged"
        if card.state == "tapped":
            return "answered"
        if card.state == "cancelled":
            return "cancelled"
        if card.state == "expired":
            return "expired"
        return "pending"

    def _require_card(self, request_id: str) -> Card:
        self._ensure_store()
        card = self._cards.get(request_id)
        if card is None:
            raise InboxError("unknown request_id")
        return self._refresh_expiry(card)

    def _refresh_expiry(self, card: Card) -> Card:
        if (
            card.state == "open"
            and card.expires_at is not None
            and self._clock() >= card.expires_at
        ):
            updated = deepcopy(card)
            updated.state = "expired"
            self._save(updated)
            self._notify_waiters(updated.request_id)
            self._request_strip(updated)
            return updated
        return card

    def _ensure_store(self) -> Store:
        if self._store is not None:
            return self._store
        store = Store(self.store_path)
        store.open()
        try:
            cards: dict[str, Card] = {}
            by_external: dict[str, str] = {}
            for payload in store.load_cards():
                card = self._card_from_payload(payload)
                cards[card.request_id] = card
                if card.external_id:
                    by_external[card.external_id] = card.request_id
            notify_ids = set(store.load_notification_ids())
        except Exception:
            store.close()
            raise
        self._cards = cards
        self._by_external_id = by_external
        self._notify_external_ids = notify_ids
        self._store = store
        return store

    def _card_from_payload(self, payload: dict[str, Any]) -> Card:
        if not isinstance(payload, dict):
            raise TypeError("card payload is not an object")
        allowed = {item.name for item in dataclass_fields(Card)}
        data = {key: value for key, value in payload.items() if key in allowed}
        if "notified" not in payload:
            data["notified"] = True
        if "telegram_keyboard_attached" not in payload:
            data["telegram_keyboard_attached"] = payload.get("telegram_message_id") is not None
        return Card(**data)

    def _notification_payload(
        self, card: Card, *, renotify: bool = False
    ) -> dict[str, Any]:
        payload = {
            "request_id": card.request_id,
            "version": card.version,
            "kind": card.kind,
            "title": card.question or card.title or "Paraphe",
            "details": card.context or card.details,
            "choices": list(card.choices),
            "choice_notes": list(card.choice_notes),
            "recommendation": card.recommendation,
            "consequence": card.consequence,
            "prohibitions": list(card.prohibitions),
            "links": list(card.links),
            "agent_name": card.agent_name,
            "risk": card.risk,
            "expires_at": card.expires_at,
            "runtime": card.runtime,
            "repo": card.repo,
            "worktree": card.worktree,
            "ticket": card.ticket,
        }
        if renotify:
            payload["renotify"] = True
        return payload

    def _finalize_telegram(self, card: Card) -> None:
        finalize = getattr(self._telegram, "finalize_message", None)
        if finalize is not None:
            finalize(self._notification_payload(card))

    def _notify_owner(self, card: Card, *, renotify: bool = False) -> None:
        if card.notified:
            return
        reserved = deepcopy(card)
        reserved.notified = True
        self._save(reserved)
        try:
            self._notifier.notify(self._notification_payload(card, renotify=renotify))
        except Exception as exc:
            current = self._cards.get(card.request_id)
            if (
                isinstance(exc, NotifyRejected)
                and current is not None
                and current.telegram_message_id is None
            ):
                rolled = deepcopy(reserved)
                rolled.notified = False
                try:
                    self._save(rolled)
                except Exception:
                    # ponytail: leftover reservation fail-closes after restart
                    self._cards[rolled.request_id] = rolled
                    if rolled.external_id:
                        self._by_external_id[rolled.external_id] = rolled.request_id
            raise

    def _save(self, card: Card) -> None:
        store = self._ensure_store()
        store.save_card(asdict(card))
        self._cards[card.request_id] = card
        if card.external_id:
            self._by_external_id[card.external_id] = card.request_id

    def _iso_now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))

    def _reject_unknown(self, args: Mapping[str, Any], allowed: frozenset[str] | set[str]) -> None:
        unknown = set(args) - set(allowed)
        if unknown:
            raise InboxError("unknown parameter")

    def _reject_keys(self, args: Mapping[str, Any], forbidden: frozenset[str] | set[str]) -> None:
        if set(args) & set(forbidden):
            raise InboxError("kind fields do not mix")

    def _require_str(self, args: Mapping[str, Any], key: str, *, required: bool = False) -> str:
        if key not in args or args[key] is None:
            if required:
                raise InboxError(f"{key} is required")
            raise InboxError(f"{key} is invalid")
        value = args[key]
        if not isinstance(value, str):
            raise InboxError(f"{key} is invalid")
        if not value.strip():
            raise InboxError(f"{key} is required" if required else f"{key} is invalid")
        limit = _LIMITS.get(key)
        if limit is not None and len(value) > limit:
            raise InboxError(f"{key} is too long")
        return value

    def _opt_str(self, args: Mapping[str, Any], key: str) -> str | None:
        if key not in args or args[key] is None:
            return None
        return self._require_str(args, key)

    def _opt_choices(self, args: Mapping[str, Any]) -> list[str]:
        raw = args.get("choices")
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise InboxError("choices is invalid")
        if len(raw) > 4:
            raise InboxError("choices is too long")
        choices: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip() or len(item) > 40:
                raise InboxError("choices is invalid")
            choices.append(item)
        return choices

    def _opt_choice_notes(self, args: Mapping[str, Any], choices: list[str]) -> list[str]:
        raw = args.get("choice_notes")
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 4:
            raise InboxError("choice_notes is invalid")
        notes: list[str] = []
        for item in raw:
            if not isinstance(item, str) or len(item) > 120:
                raise InboxError("choice_notes is invalid")
            notes.append(item)
        if len(notes) != len(choices):
            raise InboxError("choice_notes must match choices")
        return notes

    def _opt_str_list(
        self,
        args: Mapping[str, Any],
        key: str,
        *,
        max_items: int,
        max_len: int,
    ) -> list[str]:
        raw = args.get(key)
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > max_items:
            raise InboxError(f"{key} is invalid")
        items: list[str] = []
        for item in raw:
            if not isinstance(item, str) or len(item) > max_len:
                raise InboxError(f"{key} is invalid")
            items.append(item)
        return items

    def _opt_links(self, args: Mapping[str, Any]) -> list[str]:
        raw = args.get("links")
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 8:
            raise InboxError("links is invalid")
        links: list[str] = []
        for item in raw:
            if not isinstance(item, str) or len(item) > 500:
                raise InboxError("links is invalid")
            if "://" not in item:
                raise InboxError("links is invalid")
            links.append(item)
        return links

    def _opt_wait(self, args: Mapping[str, Any]) -> float | None:
        raw = args.get("wait_seconds")
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InboxError("wait_seconds is invalid")
        if raw < 0 or raw > 60:
            raise InboxError("wait_seconds is invalid")
        return float(raw)

    def _validate_result(self, result: object) -> None:
        if not isinstance(result, dict):
            raise InboxError("result is invalid")
        extra = set(result) - {
            "outcome",
            "files_changed",
            "tests_passed",
            "tests_failed",
            "commit_message",
            "duration_seconds",
        }
        if extra:
            raise InboxError("result is invalid")
        if result.get("outcome") not in RESULT_OUTCOMES:
            raise InboxError("result is invalid")
        if "files_changed" in result:
            files = result["files_changed"]
            if not isinstance(files, list) or len(files) > 50:
                raise InboxError("result is invalid")
            for path in files:
                if not isinstance(path, str) or len(path) > 300:
                    raise InboxError("result is invalid")
        for key in ("tests_passed", "tests_failed", "duration_seconds"):
            if key in result:
                value = result[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise InboxError("result is invalid")
        if "commit_message" in result:
            msg = result["commit_message"]
            if not isinstance(msg, str) or len(msg) > 500:
                raise InboxError("result is invalid")
