"""The copy-notes flag: junk headlines and empty bodies, never a block."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe.inbox import Inbox
from paraphe.inbox.copy_notes import copy_notes

BEARER = "test-mcp-bearer"


class Recorder:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def notify(self, payload: object) -> None:
        self.sent.append(payload)


PLAIN_FRENCH = {
    "question": "Garder la sauvegarde du soir ?",
    "context": "La sauvegarde de 22 h a échoué deux fois cette semaine.",
    "choices": ["Garder", "Changer"],
}

PLAIN_ENGLISH = {
    "question": "Keep the evening backup where it is?",
    "context": "The 10 pm backup failed twice this week; the family photos are on that drive.",
    "choices": ["Keep", "Change"],
}


class TestCopyNotes(unittest.TestCase):
    def test_tracker_key_in_headline_is_noted(self) -> None:
        notes = copy_notes(
            "question",
            {"question": "PROJ-123 : trancher exact canonical bridge", "context": "La situation."},
        )
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("tracker key", notes[0])
        self.assertIn("question", notes[0])

    def test_commit_hash_in_headline_is_noted(self) -> None:
        notes = copy_notes(
            "question",
            {"question": "Approuver la compilation 463641f3", "context": "La situation."},
        )
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("commit hash", notes[0])

    def test_uppercase_commit_hash_in_headline_is_noted(self) -> None:
        notes = copy_notes(
            "question",
            {"question": "Approuver la compilation 463641F3", "context": "La situation."},
        )
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("commit hash", notes[0])

    def test_code_in_choice_label_is_noted(self) -> None:
        notes = copy_notes(
            "question",
            {
                "question": "Choisir la suite ?",
                "context": "La situation.",
                "choices": ["Approuver UPX-1806", "Reporter"],
            },
        )
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("choices", notes[0])

    def test_build_jargon_token_in_headline_is_noted(self) -> None:
        notes = copy_notes(
            "question",
            {"question": "Fusionner la PR 825 ?", "context": "La situation."},
        )
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("jargon", notes[0])

    def test_missing_context_is_noted(self) -> None:
        question = copy_notes("question", {"question": "Choisir la suite ?", "context": ""})
        self.assertEqual(len(question), 1, question)
        self.assertIn("context is empty", question[0])
        approval = copy_notes("approval", {"title": "Publier maintenant ?", "details": ""})
        self.assertEqual(len(approval), 1, approval)
        self.assertIn("details is empty", approval[0])

    def test_plain_french_card_has_no_notes(self) -> None:
        self.assertEqual(copy_notes("question", PLAIN_FRENCH), [])

    def test_plain_english_card_has_no_notes(self) -> None:
        self.assertEqual(copy_notes("question", PLAIN_ENGLISH), [])

    def test_ordinary_words_are_not_flagged(self) -> None:
        for headline in (
            "Approuver le cycle de paie ?",
            "pr minuscule et date 2026-10-02",
            "Effacer le journal du soir ?",
            "Publier le site public ?",
            "deadbeef has no digit",
        ):
            self.assertEqual(
                copy_notes("question", {"question": headline, "context": "La situation."}),
                [],
                headline,
            )
        # COVID-19 matches the tracker-key shape; the matching is documented
        # as acceptable, and the note is the fix not a block.
        covid = copy_notes("question", {"question": "COVID-19 : rouvrir le bureau ?", "context": "La situation."})
        self.assertEqual(len(covid), 1, covid)

    def test_notes_never_block_the_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Recorder()
            inbox = Inbox(
                owner_telegram_id=999001,
                default_ttl_seconds=14400,
                floor_ttl_seconds=900,
                store_path=Path(tmp) / "inbox.sqlite",
                mcp_create_bearer=BEARER,
                notifier=recorder,
            )
            self.addCleanup(inbox.close)
            created = inbox.call_tool(
                "ask_question",
                {
                    "question": "PROJ-123 : trancher la sauvegarde",
                    "external_id": "junk-1",
                    "choices": ["Oui", "Non"],
                },
                bearer=BEARER,
            )
            self.assertGreaterEqual(len(created["copy_notes"]), 2, created["copy_notes"])
            self.assertEqual(created["status"], "pending")
            self.assertEqual(len(recorder.sent), 1)
            self.assertEqual(
                inbox._cards[created["request_id"]].question,
                "PROJ-123 : trancher la sauvegarde",
            )

    def test_duplicate_and_update_return_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Inbox(
                owner_telegram_id=999001,
                default_ttl_seconds=14400,
                floor_ttl_seconds=900,
                store_path=Path(tmp) / "inbox.sqlite",
                mcp_create_bearer=BEARER,
            )
            self.addCleanup(inbox.close)
            junk = {
                "title": "Approuver la fusion de la PR 825 (UPX-1806)",
                "external_id": "dup-update-1",
                "details": "La compilation 463641f3 est prête.",
            }
            first = inbox.call_tool("request_approval", junk, bearer=BEARER)
            self.assertGreaterEqual(len(first["copy_notes"]), 2, first["copy_notes"])
            duplicate = inbox.call_tool("request_approval", junk, bearer=BEARER)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["copy_notes"], first["copy_notes"])
            updated = inbox.call_tool(
                "update_request",
                {
                    "request_id": first["request_id"],
                    "expected_version": first["version"],
                    "title": "Publier la version maintenant ?",
                    "details": "La vérification est passée ; la sortie attend votre accord.",
                },
                bearer=BEARER,
            )
            self.assertEqual(updated["copy_notes"], [])


if __name__ == "__main__":
    unittest.main()
