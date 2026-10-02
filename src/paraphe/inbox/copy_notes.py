"""Flag junk card copy back to the sender. Flag, never block."""

from __future__ import annotations

import re
from typing import Any, Mapping

# Tracker keys: PROJ-123, UPX-99 — case-sensitive, as the trackers are.
TRACKER_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
# Commit hashes: 7-40 hex characters, either case. The digit-plus-letter
# rule keeps words ("defaced") and pure numbers ("12345678") out.
_HASH = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
# Build mechanics: acronyms case-sensitive, the rest not.
_JARGON_ACRONYM = re.compile(r"\b(?:PR|CI|SHA|MCP)\b")
_JARGON_WORD = re.compile(r"\b(?:rebase|worktree|cherry-pick)\b", re.IGNORECASE)

# kind -> (headline field, body field). Notify is not here: it asks nothing.
_FIELDS = {
    "question": ("question", "context"),
    "approval": ("title", "details"),
    "feedback": ("title", "details"),
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _hash_match(text: str) -> bool:
    return any(
        any(char.isdigit() for char in token)
        and any(char in "abcdefABCDEF" for char in token)
        for token in _HASH.findall(text)
    )


def _jargon_match(text: str) -> bool:
    return bool(_JARGON_ACRONYM.search(text) or _JARGON_WORD.search(text))


def copy_notes(kind: str, fields: Mapping[str, Any]) -> list[str]:
    """Short English notes naming junk copy and the fix; [] when plain."""
    headline_key, body_key = _FIELDS.get(kind, ("title", "details"))
    headline = _text(fields.get(headline_key))
    body = _text(fields.get(body_key))
    notes: list[str] = []
    if TRACKER_KEY.search(headline):
        notes.append(
            f"{headline_key}: tracker key — replace it with one or two plain "
            "sentences on the situation; keep the key in ticket or links"
        )
    if _hash_match(headline):
        notes.append(
            f"{headline_key}: commit hash — say what changed in plain words; "
            "keep the hash in links"
        )
    if _jargon_match(headline):
        notes.append(
            f"{headline_key}: build jargon — translate it into what it means "
            "for the owner"
        )
    for label in fields.get("choices") or []:
        text = _text(label)
        if TRACKER_KEY.search(text) or _hash_match(text) or _jargon_match(text):
            notes.append(
                "choices: a label carries a tracker key, commit hash or build "
                "jargon — name the outcome in plain words"
            )
            break
    if not body.strip():
        notes.append(
            f"{body_key} is empty — open with one or two plain sentences on "
            "the situation and why the owner is asked"
        )
    return notes
