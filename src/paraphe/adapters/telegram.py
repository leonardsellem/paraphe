"""Private Telegram tap adapter."""

from __future__ import annotations

import json
import threading
import time
from html import escape as _html_escape
from typing import Any
from uuid import UUID

from ..inbox import NotifyRejected

EMPTY_KEYBOARD = {"inline_keyboard": []}

# The card renderer composes the message text from the card payload in a
# fixed section order. Telegram counts a message in UTF-16 code units and
# rejects anything over 4096; the budget keeps clear headroom. The renderer is
# total: every interpolated value is escaped and oversized content trims
# deterministically with a visible marker, so no field content can inject
# markup or fail a send.
PLATFORM_LIMIT = 4096
MESSAGE_BUDGET = 3800
TRIM_MARKER = "… [trimmed]"
REPLY_HINT = "Reply to this message to answer in your own words or ask a question."
WRITE_HINT = "Tap Write answer to answer in your own words."
PAGE_SIZE = 8
NAVIGATOR_DENIED = "That card changed or closed — nothing was recorded."
NAVIGATOR_REPLY_HINT = "Open the card and tap Write answer to answer in your own words."
_MIN_TRIM_UNITS = 40

KIND_LABELS = {
    "question": "Question",
    "approval": "Approval",
    "feedback": "Feedback",
    "notify": "Status",
}


def message_units(text: str) -> int:
    """The text's length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


_MARKER_UNITS = message_units(TRIM_MARKER)


def _sanitize(value: object) -> str:
    # A lone surrogate cannot reach the platform; replace it rather than let
    # a field fail the send.
    return str(value).encode("utf-8", "replace").decode("utf-8")


def _escape(value: object) -> str:
    return _html_escape(_sanitize(value), quote=False)


def _link(url: str) -> str:
    return f'<a href="{_html_escape(_sanitize(url), quote=True)}">{_escape(url)}</a>'


def _render_links(raw: str) -> str:
    return "\n".join(_link(line) for line in raw.split("\n") if line)


def _identity_line(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("agent_name", "runtime", "repo", "worktree"):
        value = payload.get(key)
        if value:
            parts.append(_escape(value))
    ticket = payload.get("ticket")
    if ticket:
        ticket_text = str(ticket)
        parts.append(_link(ticket_text) if "://" in ticket_text else _escape(ticket_text))
    return " · ".join(parts)


def _kind_line(payload: dict[str, Any]) -> str:
    kind = str(payload.get("kind") or "")
    label = KIND_LABELS.get(kind, kind.capitalize())
    risk = payload.get("risk")
    if risk in ("high", "critical"):
        label = f"{label} · risk {_escape(risk)}"
    return label


def _expiry_line(payload: dict[str, Any]) -> str | None:
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return None
    return f"Expires: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(expires_at))}"


def _options_lines(payload: dict[str, Any]) -> list[str]:
    choices = payload.get("choices") or []
    if not choices:
        return []
    notes = payload.get("choice_notes") or []
    recommendation = payload.get("recommendation")
    recommended = str(recommendation).strip() if recommendation else None
    recommended_index = None
    if recommended is not None:
        for index, choice in enumerate(choices):
            if str(choice).strip() == recommended:
                recommended_index = index
                break
    lines: list[str] = []
    for index, choice in enumerate(choices):
        line = f"{index + 1}. {choice}"
        if index == recommended_index:
            line += " (recommended)"
        note = notes[index] if index < len(notes) else None
        if note:
            line += f" — {note}"
        lines.append(line)
    return lines


class _Piece:
    """One renderable section: our markup around a cuttable raw field text."""

    __slots__ = ("prefix", "raw", "render", "suffix", "trimmable", "trimmed")

    def __init__(
        self,
        *,
        prefix: str = "",
        raw: str = "",
        render: Any = _escape,
        suffix: str = "",
        trimmable: bool = False,
    ) -> None:
        self.prefix = prefix
        self.raw = raw
        self.render = render
        self.suffix = suffix
        self.trimmable = trimmable
        self.trimmed = False

    def text(self) -> str:
        marker = TRIM_MARKER if self.trimmed else ""
        return self.prefix + self.render(self.raw) + marker + self.suffix


def _compose(pieces: list[_Piece]) -> str:
    return "\n\n".join(piece.text() for piece in pieces if piece.text())


def _shrink(piece: _Piece, target_units: int) -> str:
    """The longest raw prefix whose rendered length fits the target."""
    if message_units(piece.prefix + piece.render("")) > target_units:
        return ""
    raw = str(piece.raw)
    low, high, best = 0, len(raw), ""
    while low <= high:
        middle = (low + high) // 2
        candidate = raw[:middle]
        if message_units(piece.prefix + piece.render(candidate)) <= target_units:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _fit(pieces: list[_Piece]) -> str:
    """Trim the longest free-text sections until the message fits the budget."""
    while True:
        text = _compose(pieces)
        total = message_units(text)
        if total <= MESSAGE_BUDGET:
            return text
        over = total - MESSAGE_BUDGET
        candidates = [
            piece
            for piece in pieces
            if piece.trimmable
            and message_units(piece.text()) > _MIN_TRIM_UNITS + _MARKER_UNITS
        ]
        if not candidates:
            # Safety valve: with the bounded field set this is unreachable;
            # it exists so the renderer stays total instead of raising.
            for piece in sorted(
                (item for item in pieces if item.trimmable),
                key=lambda item: message_units(item.text()),
                reverse=True,
            ):
                piece.raw = ""
                if message_units(_compose(pieces)) <= MESSAGE_BUDGET:
                    break
            return _compose(pieces)
        victim = max(candidates, key=lambda piece: message_units(piece.text()))
        target = message_units(victim.text()) - over - _MARKER_UNITS
        victim.raw = _shrink(victim, max(target, _MIN_TRIM_UNITS))
        victim.trimmed = True


def render_card(payload: dict[str, Any], reply_hint: str = REPLY_HINT) -> str:
    """Compose a card's message text: fixed order, escaped, budgeted.

    Order: identity line, kind line, bold title, context, numbered options
    with notes, Recommended, If approved, Limits, links, reply hint, expiry.
    Status messages render the identity line, the title and the message only.
    """
    pieces: list[_Piece] = []
    identity = _identity_line(payload)
    if identity:
        pieces.append(_Piece(prefix=identity))
    kind_line = _kind_line(payload)
    if kind_line:
        pieces.append(_Piece(prefix=kind_line))
    title = payload.get("title")
    if title:
        pieces.append(_Piece(prefix="<b>", raw=str(title), suffix="</b>"))
    if payload.get("kind") == "notify":
        message = payload.get("message")
        if message:
            pieces.append(_Piece(raw=str(message), trimmable=True))
    else:
        details = payload.get("details")
        if details:
            pieces.append(_Piece(raw=str(details), trimmable=True))
        options = _options_lines(payload)
        if options:
            pieces.append(_Piece(raw="\n".join(options), trimmable=True))
        recommendation = payload.get("recommendation")
        if recommendation:
            pieces.append(
                _Piece(prefix="<b>Recommended:</b> ", raw=str(recommendation), trimmable=True)
            )
        consequence = payload.get("consequence")
        if consequence:
            pieces.append(
                _Piece(prefix="<b>If approved:</b> ", raw=str(consequence), trimmable=True)
            )
        prohibitions = payload.get("prohibitions") or []
        if prohibitions:
            listed = "\n".join(f"- {item}" for item in prohibitions)
            pieces.append(_Piece(prefix="<b>Limits:</b>\n", raw=listed, trimmable=True))
        links = payload.get("links") or []
        if links:
            pieces.append(
                _Piece(
                    prefix="<b>Links:</b>\n",
                    raw="\n".join(str(url) for url in links),
                    render=_render_links,
                    trimmable=True,
                )
            )
        pieces.append(_Piece(prefix=reply_hint))
        expiry = _expiry_line(payload)
        if expiry:
            pieces.append(_Piece(prefix=expiry))
    return _fit(pieces)


def _row_meta(card: Any) -> str:
    """Origin, a non-default priority or risk, and the expiry — when present."""
    parts: list[str] = []
    origin = " · ".join(
        str(value) for value in (card.agent_name, card.repo) if value
    )
    if origin:
        parts.append(_escape(origin))
    if card.priority and card.priority != "normal":
        parts.append(f"priority {_escape(card.priority)}")
    if card.risk and card.risk != "medium":
        parts.append(f"risk {_escape(card.risk)}")
    expires_at = card.expires_at
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        parts.append(
            "expires " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(expires_at))
        )
    return " · ".join(parts)


def render_pending_list(cards: list[Any], total: int, notice: str | None = None) -> str:
    """The Pending list's semantic view: notice, total, compact rows."""
    blocks: list[str] = []
    if notice:
        blocks.append(_escape(notice))
    blocks.append(f"Pending: {int(total)}")
    if not cards:
        blocks.append("Nothing is waiting for an answer.")
        return "\n\n".join(blocks)
    rows: list[str] = []
    for index, card in enumerate(cards, start=1):
        title = str(card.question or card.title or "Paraphe")[:60]
        row = f"{index}. <b>{_escape(title)}</b>"
        meta = _row_meta(card)
        if meta:
            row += f" — {meta}"
        rows.append(row)
    blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def encode_callback_data(card_id: str, version: int, choice_index: int) -> str:
    if (
        isinstance(choice_index, bool)
        or not isinstance(choice_index, int)
        or not 0 <= choice_index < 4
    ):
        raise ValueError("callback data is invalid")
    token = f"p1:{UUID(card_id).hex}:{version}:{choice_index}"
    if len(token.encode("utf-8")) > 64:
        raise ValueError("callback data is too long")
    return token


def decode_callback_data(callback_data: str) -> tuple[str, int, int]:
    try:
        prefix, hex_id, version_s, choice_s = callback_data.split(":")
        version = int(version_s)
        choice_index = int(choice_s)
    except (TypeError, ValueError):
        raise ValueError("callback data is invalid") from None
    if prefix != "p1" or version < 1 or not 0 <= choice_index < 4:
        raise ValueError("callback data is invalid")
    return str(UUID(hex=hex_id)), version, choice_index


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


NAVIGATOR_OPS = frozenset({"L", "P", "O", "A", "W", "B", "R"})

REFUSAL_LINES = {
    "stale_version": "this card changed — review it again",
    "stale_message": "this card's message changed",
    "expired": "this card expired",
    "cancelled": "this card was cancelled",
    "already_tapped": "this card was already answered",
    "invalid_choice": "that choice is not on the card any more",
    "invalid_answer": "that answer is not valid",
    "invalid_message": "that card's message changed",
    "unknown": "this card is unknown",
    "not_owner": "that is not the owner's card",
}


def _is_pending_command(text: str) -> bool:
    return text == "/pending" or text.startswith("/pending@")


def _updated_line(now: float) -> str:
    return "Updated " + time.strftime("%H:%M:%S UTC", time.gmtime(now))


def encode_navigator_callback(
    op: str,
    card_id: str | None = None,
    version: int | None = None,
    choice: int | None = None,
) -> str:
    """A navigator control: n1:<op>[:<uuid-hex>[:<version>[:<choice>]]]."""
    if op not in NAVIGATOR_OPS:
        raise ValueError("callback data is invalid")
    parts = ["n1", op]
    if op in {"L", "P"}:
        parts.append(UUID(card_id).hex if card_id else "0")
    elif op == "O":
        parts.append(UUID(str(card_id)).hex)
    elif op == "W":
        parts.extend([UUID(str(card_id)).hex, str(int(version or 0))])
    elif op == "A":
        parts.extend([UUID(str(card_id)).hex, str(int(version or 0)), str(int(choice or 0))])
    token = ":".join(parts)
    if len(token.encode("utf-8")) > 64:
        raise ValueError("callback data is too long")
    return token


def decode_navigator_callback(data: str) -> dict[str, Any]:
    if not isinstance(data, str):
        raise ValueError("callback data is invalid")
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "n1" or parts[1] not in NAVIGATOR_OPS:
        raise ValueError("callback data is invalid")
    op = parts[1]
    action: dict[str, Any] = {
        "op": op,
        "card_id": None,
        "version": None,
        "choice": None,
        "anchor": None,
    }
    try:
        if op in {"L", "P"}:
            if len(parts) != 3:
                raise ValueError("callback data is invalid")
            action["anchor"] = None if parts[2] == "0" else str(UUID(hex=parts[2]))
        elif op == "O":
            if len(parts) != 3:
                raise ValueError("callback data is invalid")
            action["card_id"] = str(UUID(hex=parts[2]))
        elif op == "W":
            if len(parts) != 4:
                raise ValueError("callback data is invalid")
            action["card_id"] = str(UUID(hex=parts[2]))
            action["version"] = int(parts[3])
            if action["version"] < 1:
                raise ValueError("callback data is invalid")
        elif op == "A":
            if len(parts) != 5:
                raise ValueError("callback data is invalid")
            action["card_id"] = str(UUID(hex=parts[2]))
            action["version"] = int(parts[3])
            action["choice"] = int(parts[4])
            if action["version"] < 1 or not 0 <= action["choice"] < 4:
                raise ValueError("callback data is invalid")
        elif len(parts) != 2:
            raise ValueError("callback data is invalid")
    except (TypeError, ValueError):
        raise ValueError("callback data is invalid") from None
    return action


class TelegramAdapter:
    def __init__(self, inbox: Any, api: Any, *, owner_id: int, bot_token: str) -> None:
        self._inbox = inbox
        self._api = api
        self._owner_id = owner_id
        self._bot_token = bot_token
        self._messages: dict[str, tuple[int, int, int]] = {}
        # Two navigator locks: `_nav_state` is short and memory-only,
        # `_nav_edit` spans one Telegram edit. Never wait for `_nav_edit`
        # while holding `_nav_state`, and never hold `_nav_state` across an
        # inbox or Telegram call.
        self._nav_state = threading.RLock()
        self._nav_edit = threading.RLock()
        self._nav: dict[str, Any] = {"message_id": None}
        self._nav_loaded = False
        self._nav_generation = 0
        self._nav_next_attempt = 0.0
        self._nav_backoff = 0.0
        self._commands_registered = False
        self._commands_next_attempt = 0.0
        self._commands_backoff = 0.0

    def notify(self, payload: dict[str, Any]) -> None:
        text = render_card(payload)
        if payload.get("kind") == "notify":
            self._api.send_message(self._owner_id, text, None, parse_mode="HTML")
            return
        self.send(payload, text=text)

    def _markup(self, created: dict[str, Any]) -> dict[str, Any]:
        choices = created.get("choices") or ["Approve", "Deny"]
        buttons = [
            {
                "text": choice,
                "callback_data": encode_callback_data(
                    created["request_id"], created["version"], choice_index
                ),
            }
            for choice_index, choice in enumerate(choices)
        ]
        return {"inline_keyboard": [buttons]}

    def send(self, created: dict[str, Any], *, text: str) -> None:
        sent = self._api.send_message(self._owner_id, text, None, parse_mode="HTML")
        message_id = _positive_int(sent.get("message_id") if isinstance(sent, dict) else None)
        if message_id is None:
            return
        self._inbox.record_telegram_message(
            created["request_id"],
            version=created["version"],
            chat_id=self._owner_id,
            message_id=message_id,
        )
        self.remember_message(
            created["request_id"], created["version"], self._owner_id, message_id
        )
        self.finalize_message(created)

    def remember_message(
        self, card_id: str, version: int, chat_id: int, message_id: int
    ) -> None:
        if version > 0 and chat_id == self._owner_id and message_id > 0:
            self._messages[card_id] = (version, chat_id, message_id)

    def finalize_message(self, created: dict[str, Any]) -> None:
        loc = self._messages.get(created["request_id"])
        if loc is None or loc[0] != created["version"]:
            return
        version, chat_id, message_id = loc
        self._api.edit_message_reply_markup(chat_id, message_id, self._markup(created))
        try:
            self._inbox.record_telegram_keyboard_attached(
                created["request_id"],
                version=version,
                chat_id=chat_id,
                message_id=message_id,
            )
        except Exception:
            try:
                self.strip_message(chat_id, message_id)
            except Exception:
                self._inbox._record_terminal_keyboard_retry(
                    created["request_id"], version=version, chat_id=chat_id, message_id=message_id
                )
            raise

    def edit_and_strip(self, card_id: str, version: int) -> None:
        _ = version
        loc = self._messages.get(card_id)
        if loc is None:
            return
        _version, chat_id, message_id = loc
        self.strip_message(chat_id, message_id)

    def strip_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._api.edit_message_reply_markup(chat_id, message_id, EMPTY_KEYBOARD)
        except NotifyRejected as exc:
            if exc.code != 400 or exc.reason != "message_not_modified":
                raise

    def strip_stale_message(self, card_id: str, chat_id: int, message_id: int) -> None:
        current = self._messages.get(card_id)
        if current is None or current[1:] != (chat_id, message_id):
            self.strip_message(chat_id, message_id)

    def handle_update(self, update: object) -> list[str]:
        if not isinstance(update, dict):
            return []
        callback = update.get("callback_query")
        if callback is not None:
            if not isinstance(callback, dict):
                return []
            self._on_callback(callback)
            return []
        message = update.get("message")
        if not isinstance(message, dict):
            return []
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return []
        from_id = _positive_int(sender.get("id"))
        chat_id = _positive_int(chat.get("id"))
        text = message.get("text")
        if (
            chat.get("type") != "private"
            or chat_id is None
            or chat_id != self._owner_id
            or from_id != self._owner_id
            or not isinstance(text, str)
        ):
            return []
        reply_to = message.get("reply_to_message")
        if isinstance(reply_to, dict):
            # A reply to a card message is the owner's answer to that card.
            reply_message_id = _positive_int(reply_to.get("message_id"))
            if reply_message_id is None:
                return []
            state = self._navigator_state()
            with self._nav_state:
                navigator_id = state.get("message_id")
            navigator_reply = navigator_id is not None and reply_message_id == navigator_id
            forwarded = message.get("forward_origin") is not None
            prompt_reply = self._inbox.reply_prompt(chat_id, reply_message_id) is not None
            if forwarded and (navigator_reply or prompt_reply):
                return []
            if navigator_reply:
                # A reply to the navigator is never an answer.
                self._send_line(NAVIGATOR_REPLY_HINT)
                return []
            self._on_reply(chat_id, reply_message_id, text)
            return []
        if _is_pending_command(text):
            if message.get("forward_origin") is None:
                self.open_navigator()
            return []
        if text.startswith("/config"):
            return ["ttl and owner knobs only"]
        return []

    def _send_line(self, text: str) -> None:
        self._api.send_message(self._owner_id, text, None)

    def _on_reply(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._inbox.claim_reply(
                from_id=self._owner_id,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except Exception as exc:
            # Refusals record nothing, wake nothing and raise nothing; a real
            # failure (the durable write) still surfaces.
            if type(exc).__name__ != "ClaimRefused":
                raise
            if self._inbox.reply_prompt(chat_id, message_id) is not None:
                self._send_line(NAVIGATOR_DENIED)

    def _on_callback(self, callback: dict[str, Any]) -> None:
        raw_callback_id = callback.get("id")
        callback_id = raw_callback_id if isinstance(raw_callback_id, str) else ""
        sender = callback.get("from")
        message = callback.get("message")
        if not isinstance(sender, dict) or not isinstance(message, dict):
            self._answer(callback_id)
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            self._answer(callback_id)
            return
        from_id = _positive_int(sender.get("id"))
        chat_id = _positive_int(chat.get("id"))
        if (
            from_id is None
            or chat_id is None
            or chat.get("type") != "private"
            or chat_id != self._owner_id
        ):
            self._answer(callback_id)
            return
        data = callback.get("data")
        if not isinstance(data, str):
            self._answer(callback_id)
            return
        message_id = _positive_int(message.get("message_id"))
        if data.startswith("n1:"):
            self._on_navigator(callback_id, chat_id, message_id, message, data)
            return
        try:
            card_id, version, choice_index = decode_callback_data(data)
        except ValueError:
            self._answer(callback_id)
            return
        try:
            self._inbox.claim(
                card_id,
                version=version,
                from_id=from_id,
                choice_index=choice_index,
                telegram_chat_id=chat_id if message_id is not None else None,
                telegram_message_id=message_id,
            )
        except Exception as exc:
            if (
                type(exc).__name__ == "ClaimRefused"
                and getattr(exc, "reason", None) in {"stale_message", "stale_version"}
                and message_id is not None
            ):
                try:
                    self.strip_stale_message(card_id, chat_id, message_id)
                except Exception:
                    pass
            self._answer(callback_id)
            if type(exc).__name__ != "ClaimRefused":
                raise
            return
        self._answer(callback_id)

    def _answer(self, callback_id: str) -> None:
        if not callback_id:
            return
        self._api.answer_callback_query(callback_id)

    # --- Pending navigator --------------------------------------------------

    @property
    def commands_registered(self) -> bool:
        return self._commands_registered

    def register_commands(self) -> None:
        """Give the owner chat its command list, with `pending` in it."""
        chat_scope = {"type": "chat", "chat_id": self._owner_id}
        try:
            commands = self._api.get_my_commands(chat_scope)
            if not commands:
                commands = self._api.get_my_commands({"type": "default"})
            merged = [dict(item) for item in commands]
            if not any(item.get("command") == "pending" for item in merged):
                merged.append(
                    {"command": "pending", "description": "Cards waiting for your answer"}
                )
            if merged != commands:
                # Kept for the documented rollback: the pre-change list.
                print(
                    "Telegram command list before registration: "
                    + json.dumps(commands),
                    flush=True,
                )
                self._api.set_my_commands(merged, chat_scope)
        except Exception as exc:
            print(
                f"Telegram command registration failed: {type(exc).__name__}",
                flush=True,
            )
            return
        self._commands_registered = True
        self._commands_backoff = 0.0
        self._commands_next_attempt = 0.0

    def retry_register_commands(self, now: float) -> None:
        if self._commands_registered or now < self._commands_next_attempt:
            return
        self.register_commands()
        if self._commands_registered:
            return
        self._commands_backoff = (
            1.0 if self._commands_backoff <= 0 else min(self._commands_backoff * 2.0, 60.0)
        )
        self._commands_next_attempt = now + self._commands_backoff

    def refresh_navigator(self, now: float) -> None:
        self._publish(now)

    def _navigator_state(self) -> dict[str, Any]:
        """The navigator state, loaded once from the durable store."""
        with self._nav_edit:
            with self._nav_state:
                if self._nav_loaded:
                    return self._nav
            loaded = self._inbox.load_navigator()
            with self._nav_state:
                if not self._nav_loaded:
                    self._nav = loaded if isinstance(loaded, dict) else {"message_id": None}
                    self._nav_loaded = True
                return self._nav

    def _navigator_intent(self, view: dict[str, Any], notice: str | None) -> None:
        with self._nav_state:
            self._nav["view"] = view
            self._nav["notice"] = notice
            self._nav_generation += 1

    def _compose(
        self, view: dict[str, Any], notice: str | None
    ) -> tuple[str, dict[str, Any], list[Any]]:
        """The semantic view for one intent: text and keyboard, no Updated line."""
        if view.get("kind") != "card":
            cards, total, next_anchor, expired = self._inbox.pending_page(view.get("anchor"))
            return (
                render_pending_list(cards, total, notice),
                self._list_markup(cards, next_anchor),
                expired,
            )
        card_id = str(view.get("request_id"))
        version = view.get("version")
        payload = self._inbox.navigation_payload(card_id)
        if (
            payload is not None
            and payload.get("state") == "open"
            and payload.get("version") == version
        ):
            text = render_card(payload, reply_hint=WRITE_HINT)
            if notice:
                text = f"{_escape(notice)}\n\n{text}"
            return text, self._card_markup(card_id, int(payload["version"]), payload), []
        if payload is not None and payload.get("state") != "open":
            changed = f"This card is closed: {_escape(payload.get('state'))}"
        else:
            changed = "This card changed — review it again"
        text = f"{_escape(notice)}\n\n{changed}" if notice else changed
        return text, self._notice_markup(card_id), []

    def _list_markup(self, cards: list[Any], next_anchor: str | None) -> dict[str, Any]:
        rows = [
            [
                {
                    "text": f"{index}. {str(card.question or card.title or 'Paraphe')[:40]}",
                    "callback_data": encode_navigator_callback("O", card.request_id),
                }
            ]
            for index, card in enumerate(cards, start=1)
        ]
        controls: list[dict[str, str]] = []
        if cards:
            controls.append(
                {
                    "text": "Previous",
                    "callback_data": encode_navigator_callback("P", cards[0].request_id),
                }
            )
        controls.append({"text": "Refresh", "callback_data": encode_navigator_callback("R")})
        if next_anchor is not None:
            controls.append(
                {"text": "Next", "callback_data": encode_navigator_callback("L", next_anchor)}
            )
        return {"inline_keyboard": rows + [controls]}

    def _card_markup(
        self, card_id: str, version: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        choices = list(payload.get("choices") or ["Approve", "Deny"])
        buttons = [
            {
                "text": str(choice)[:40],
                "callback_data": encode_navigator_callback("A", card_id, version, index),
            }
            for index, choice in enumerate(choices[:4])
        ]
        return {
            "inline_keyboard": [
                buttons,
                [
                    {
                        "text": "Write answer",
                        "callback_data": encode_navigator_callback("W", card_id, version),
                    }
                ],
                [{"text": "Back to Pending", "callback_data": encode_navigator_callback("B")}],
            ]
        }

    def _notice_markup(self, card_id: str) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "Review again",
                        "callback_data": encode_navigator_callback("O", card_id),
                    }
                ],
                [{"text": "Back to Pending", "callback_data": encode_navigator_callback("B")}],
            ]
        }

    def _back_off(self, now: float) -> None:
        with self._nav_state:
            self._nav_backoff = (
                1.0 if self._nav_backoff <= 0 else min(self._nav_backoff * 2.0, 60.0)
            )
            self._nav_next_attempt = now + self._nav_backoff

    def _edit_rejected(
        self,
        exc: NotifyRejected,
        state: dict[str, Any],
        text: str,
        markup: dict[str, Any],
        now: float,
    ) -> None:
        code = exc.code
        if code == 400 and exc.reason == "message_not_modified":
            # A successful no-op: the semantic view is stored, the time is not moved.
            with self._nav_state:
                state["published_text"] = text
                state["published_markup"] = markup
                snapshot = dict(state)
            self._inbox.save_navigator(snapshot)
            return
        if code == 429:
            wait = exc.retry_after
            delay = wait if isinstance(wait, int) and wait > 0 else 1
            with self._nav_state:
                self._nav_next_attempt = now + delay
            return
        if code is not None and 500 <= code < 600:
            self._back_off(now)
            return
        if code is not None and 400 <= code < 500:
            print(f"Telegram navigator suspended: code={code}", flush=True)
            with self._nav_state:
                state["suspended"] = True
                snapshot = dict(state)
            self._inbox.save_navigator(snapshot)
            return
        self._back_off(now)

    def _publish(self, now: float) -> None:
        """The one navigator edit path. It never sends, never pins."""
        expired: list[Any] = []
        try:
            with self._nav_edit:
                state = self._navigator_state()
                with self._nav_state:
                    if state.get("suspended") or now < self._nav_next_attempt:
                        return
                    message_id = state.get("message_id")
                    view = dict(state.get("view") or {"kind": "list", "anchor": None})
                    notice = state.get("notice")
                    generation = self._nav_generation
                    published = (
                        state.get("published_text"),
                        state.get("published_markup"),
                    )
                if message_id is None:
                    return
                text, markup, expired = self._compose(view, notice)
                with self._nav_state:
                    if (
                        self._nav_generation != generation
                        or state.get("message_id") != message_id
                    ):
                        return
                if (text, markup) == published:
                    return
                body = f"{text}\n\n{_updated_line(now)}"
                try:
                    self._api.edit_message_text(
                        self._owner_id, message_id, body, markup, parse_mode="HTML"
                    )
                except NotifyRejected as exc:
                    self._edit_rejected(exc, state, text, markup, now)
                except Exception as exc:
                    if type(exc).__name__ != "TelegramAPIError":
                        raise
                    self._back_off(now)
                else:
                    with self._nav_state:
                        state["published_text"] = text
                        state["published_markup"] = markup
                        state["published_at"] = now
                        self._nav_backoff = 0.0
                        self._nav_next_attempt = 0.0
                        # The latest intent and generation, never the snapshot's.
                        snapshot = dict(state)
                    self._inbox.save_navigator(snapshot)
        finally:
            self._inbox.strip_closed(expired)

    def _retire(self, previous_id: int | None, new_id: int) -> None:
        self._best_effort("pin", lambda: self._api.pin_chat_message(self._owner_id, new_id))
        if previous_id is None or previous_id == new_id:
            return
        self._best_effort(
            "strip", lambda: self.strip_message(self._owner_id, previous_id)
        )
        self._best_effort(
            "unpin", lambda: self._api.unpin_chat_message(self._owner_id, previous_id)
        )

    def _best_effort(self, action: str, call: Any) -> None:
        try:
            call()
        except Exception as exc:
            print(
                f"Telegram navigator {action} failed: {type(exc).__name__}", flush=True
            )

    def open_navigator(self) -> None:
        """A fresh navigator at the bottom of the chat, pinned, durably recorded."""
        if not self._commands_registered:
            self.register_commands()
        expired: list[Any] = []
        try:
            with self._nav_edit:
                state = self._navigator_state()
                with self._nav_state:
                    previous_id = state.get("message_id")
                cards, total, next_anchor, expired = self._inbox.pending_page(None)
                text = render_pending_list(cards, total, None)
                markup = self._list_markup(cards, next_anchor)
                now = time.time()
                sent = self._api.send_message(
                    self._owner_id,
                    f"{text}\n\n{_updated_line(now)}",
                    markup,
                    parse_mode="HTML",
                )
                message_id = _positive_int(
                    sent.get("message_id") if isinstance(sent, dict) else None
                )
                if message_id is None:
                    return
                new_state = {
                    "message_id": message_id,
                    "view": {"kind": "list", "anchor": None},
                    "notice": None,
                    "suspended": False,
                    "published_text": text,
                    "published_markup": markup,
                    "published_at": now,
                }
                try:
                    self._inbox.save_navigator(new_state)
                except Exception as exc:
                    print(
                        f"Telegram navigator activation failed: {type(exc).__name__}",
                        flush=True,
                    )
                    return
                with self._nav_state:
                    self._nav = new_state
                    self._nav_loaded = True
                    self._nav_generation += 1
                    self._nav_next_attempt = 0.0
                    self._nav_backoff = 0.0
            self._retire(previous_id, message_id)
        finally:
            self._inbox.strip_closed(expired)

    def _on_navigator(
        self,
        callback_id: str,
        chat_id: int,
        message_id: int | None,
        message: dict[str, Any],
        data: str,
    ) -> None:
        # Acknowledge first: a 15 s edit must never delay it.
        self._answer(callback_id)
        if message.get("forward_origin") is not None:
            return
        try:
            action = decode_navigator_callback(data)
        except ValueError:
            return
        state = self._navigator_state()
        with self._nav_state:
            active = state.get("message_id")
            suspended = bool(state.get("suspended"))
        if message_id is None or active != message_id or suspended:
            if message_id is not None:
                try:
                    self.strip_message(chat_id, message_id)
                except Exception:
                    pass
            return
        now = time.time()
        op = action["op"]
        if op == "A":
            self._navigator_answer(action, now)
        elif op == "W":
            self._navigator_write(action, now)
        elif op == "O":
            self._navigator_open(action, now)
        elif op == "B":
            self._navigator_back(now)
        elif op in {"L", "P"}:
            self._navigator_page(action["anchor"], op == "P", now)
        else:
            self._publish(now)

    def _navigator_answer(self, action: dict[str, Any], now: float) -> None:
        card_id = str(action["card_id"])
        choice = int(action["choice"])
        try:
            self._inbox.claim(
                card_id,
                version=int(action["version"]),
                from_id=self._owner_id,
                choice_index=choice,
            )
        except Exception as exc:
            if type(exc).__name__ != "ClaimRefused":
                raise
            reason = str(getattr(exc, "reason", "") or "")
            notice = (
                "Not recorded: "
                + REFUSAL_LINES.get(reason, "that card is not answerable")
                + "."
            )
        else:
            payload = self._inbox.navigation_payload(card_id) or {}
            choices = list(payload.get("choices") or ["Approve", "Deny"])
            label = choices[choice] if 0 <= choice < len(choices) else f"option {choice + 1}"
            notice = f"Answer recorded: {label}."
        self._navigator_intent({"kind": "list", "anchor": None}, notice)
        self._publish(now)

    def _navigator_write(self, action: dict[str, Any], now: float) -> None:
        card_id = str(action["card_id"])
        version = int(action["version"])
        payload = self._inbox.navigation_payload(card_id)
        if payload is None or payload.get("state") != "open" or payload.get("version") != version:
            # No prompt: the R8 notice, at the revision the owner tried to answer.
            self._navigator_intent(
                {
                    "kind": "card",
                    "request_id": card_id,
                    "version": version,
                    "anchor": self._current_anchor(),
                },
                None,
            )
            self._publish(now)
            return
        title = str(payload.get("title") or "Paraphe")[:120]
        sent = self._api.send_message(
            self._owner_id,
            f"Write your answer to: {_escape(title)}",
            {"force_reply": True, "input_field_placeholder": "Your answer"},
            parse_mode="HTML",
        )
        message_id = _positive_int(sent.get("message_id") if isinstance(sent, dict) else None)
        if message_id is not None:
            try:
                self._inbox.record_reply_prompt(
                    self._owner_id, message_id, card_id, version
                )
            except Exception:
                # An unbound prompt records nothing; its replies resolve to none.
                pass
        self._navigator_intent(
            {
                "kind": "card",
                "request_id": card_id,
                "version": version,
                "anchor": self._current_anchor(),
            },
            "Reply to the prompt above to answer in your own words.",
        )
        self._publish(now)

    def _navigator_open(self, action: dict[str, Any], now: float) -> None:
        card_id = str(action["card_id"])
        payload = self._inbox.navigation_payload(card_id)
        version = payload.get("version") if payload is not None else None
        self._navigator_intent(
            {
                "kind": "card",
                "request_id": card_id,
                "version": version if isinstance(version, int) else 0,
                "anchor": self._current_anchor(),
            },
            None,
        )
        self._publish(now)

    def _navigator_back(self, now: float) -> None:
        self._navigator_intent({"kind": "list", "anchor": self._current_anchor()}, None)
        self._publish(now)

    def _navigator_page(self, anchor: str | None, before: bool, now: float) -> None:
        if before:
            page, _total, _next, expired = self._inbox.pending_page(anchor, before=True)
            self._inbox.strip_closed(expired)
            anchor = page[0].request_id if page else None
        self._navigator_intent({"kind": "list", "anchor": anchor}, None)
        self._publish(now)

    def _current_anchor(self) -> str | None:
        with self._nav_state:
            view = self._nav.get("view") or {}
            anchor = view.get("anchor")
        return anchor if isinstance(anchor, str) else None
