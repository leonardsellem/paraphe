"""Card held by the Inbox. Not a SQLite row type."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Card:
    request_id: str
    kind: str
    version: int = 1
    external_id: str | None = None
    state: str = "open"
    question: str | None = None
    context: str | None = None
    choices: list[str] = field(default_factory=list)
    choice_notes: list[str] = field(default_factory=list)
    title: str | None = None
    details: str | None = None
    allow_freeform: bool | None = None
    recommendation: str | None = None
    consequence: str | None = None
    prohibitions: list[str] = field(default_factory=list)
    risk: str = "medium"
    priority: str = "normal"
    project: str | None = None
    source_thread: str | None = None
    runtime: str | None = None
    repo: str | None = None
    worktree: str | None = None
    ticket: str | None = None
    links: list[str] = field(default_factory=list)
    agent_name: str | None = None
    expires_in_seconds: int = 14400
    expires_at: float | None = None
    created_at: float | None = None
    response_choice: str | None = None
    response_text: str | None = None
    responded_at: str | None = None
    responded_via: str | None = None
    processed_at: str | None = None
    execution_status: str | None = None
    execution_note: str | None = None
    execution_result: dict[str, Any] | None = None
    cancel_reason: str | None = None
    notified: bool = False
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    telegram_keyboard_attached: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
