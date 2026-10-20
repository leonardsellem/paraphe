"""Private Telegram tap adapter."""

from __future__ import annotations

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


def render_card(payload: dict[str, Any]) -> str:
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
        pieces.append(_Piece(prefix=REPLY_HINT))
        expiry = _expiry_line(payload)
        if expiry:
            pieces.append(_Piece(prefix=expiry))
    return _fit(pieces)


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


class TelegramAdapter:
    def __init__(self, inbox: Any, api: Any, *, owner_id: int, bot_token: str) -> None:
        self._inbox = inbox
        self._api = api
        self._owner_id = owner_id
        self._bot_token = bot_token
        self._messages: dict[str, tuple[int, int, int]] = {}

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
            if reply_message_id is not None:
                self._on_reply(chat_id, reply_message_id, text)
            return []
        if text.startswith("/config"):
            return ["ttl and owner knobs only"]
        return []

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
