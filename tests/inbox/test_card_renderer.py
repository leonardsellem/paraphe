"""Card renderer tests: ordered rich sections, escaping, the budget, status."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe.adapters import console as console_module
from paraphe.adapters import telegram as tg

MESSAGE_BUDGET = tg.MESSAGE_BUDGET
TRIM_MARKER = tg.TRIM_MARKER
render_card = tg.render_card
message_units = tg.message_units

# 1_700_000_900 epoch seconds -> "2023-11-14 22:28:20 UTC"
EXPIRES_AT = 1_700_000_900
EXPIRY_LINE = "Expires: 2023-11-14 22:28:20 UTC"

HINT = "Reply to this message to answer in your own words or ask a question."

# The R5 detection patterns, repeated here so the render test fails
# independently of the module that flags copy.
TRACKER_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
COMMIT_HASH = re.compile(r"\b[0-9a-fA-F]{7,40}\b")


def _codes_in(line: str) -> list[str]:
    found = TRACKER_KEY.findall(line)
    for token in COMMIT_HASH.findall(line):
        if any(char.isdigit() for char in token) and any(
            char in "abcdefABCDEF" for char in token
        ):
            found.append(token)
    return found


def full_card() -> dict:
    return {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "version": 3,
        "kind": "approval",
        "title": "Cut over the service to the new revision?",
        "details": "The live state changed; the safe window is now.",
        "choices": ["Approve", "Deny"],
        "choice_notes": ["do it now, off-hours", "keep the old revision"],
        "recommendation": "Approve",
        "consequence": "The service moves to the new revision and the old is stopped.",
        "prohibitions": ["No infra changes.", "No data migration."],
        "links": ["https://github.com/example/paraphe/pull/18"],
        "risk": "high",
        "priority": "high",
        "agent_name": "Hermes",
        "runtime": "Hermes",
        "repo": "paraphe",
        "worktree": "feature-better-tg-cards",
        "ticket": "card-3118",
        "expires_at": EXPIRES_AT,
    }


FULL_EXPECTED = (
    "Hermes · Hermes · paraphe · feature-better-tg-cards · card-3118\n"
    "\n"
    "Approval · risk high\n"
    "\n"
    "<b>Cut over the service to the new revision?</b>\n"
    "\n"
    "The live state changed; the safe window is now.\n"
    "\n"
    "1. Approve (recommended) — do it now, off-hours\n"
    "2. Deny — keep the old revision\n"
    "\n"
    "<b>Recommended:</b> Approve\n"
    "\n"
    "<b>If approved:</b> The service moves to the new revision and the old is stopped.\n"
    "\n"
    "<b>Limits:</b>\n"
    "- No infra changes.\n"
    "- No data migration.\n"
    "\n"
    '<b>Links:</b>\n<a href="https://github.com/example/paraphe/pull/18">'
    "https://github.com/example/paraphe/pull/18</a>\n"
    "\n"
    + HINT
    + "\n"
    "\n"
    + EXPIRY_LINE
)


class TestCardRenderer(unittest.TestCase):
    def test_full_card_renders_the_pinned_fixture(self) -> None:
        self.assertEqual(render_card(full_card()), FULL_EXPECTED)
        # deterministic: a second render is identical
        self.assertEqual(render_card(full_card()), render_card(full_card()))

    def test_identity_line_renders_present_fields_in_order_and_invents_nothing(self) -> None:
        base = {"kind": "question", "title": "Ready?", "expires_at": EXPIRES_AT}

        def first_line(extra: dict) -> str:
            return render_card({**base, **extra}).split("\n", 1)[0]

        self.assertEqual(first_line({}), "Question")
        self.assertEqual(first_line({"agent_name": "Hermes"}), "Hermes")
        self.assertEqual(
            first_line({"agent_name": "Hermes", "runtime": "Claude Code"}),
            "Hermes · Claude Code",
        )
        self.assertEqual(
            first_line({"agent_name": "Hermes", "repo": "paraphe"}),
            "Hermes · paraphe",
        )
        self.assertEqual(
            first_line(
                {
                    "agent_name": "Hermes",
                    "runtime": "Claude Code",
                    "repo": "paraphe",
                    "worktree": "feature-better-tg-cards",
                    "ticket": "card-3118",
                }
            ),
            "Hermes · Claude Code · paraphe · feature-better-tg-cards · card-3118",
        )

    def test_url_ticket_renders_as_a_link(self) -> None:
        text = render_card(
            {
                "kind": "approval",
                "title": "Ready?",
                "agent_name": "Hermes",
                "ticket": "https://tracker.example/card-3118",
                "expires_at": EXPIRES_AT,
            }
        )
        self.assertTrue(
            text.startswith(
                'Hermes · <a href="https://tracker.example/card-3118">'
                "https://tracker.example/card-3118</a>"
            ),
            text.split("\n", 1)[0],
        )

    def test_markup_metacharacters_are_escaped_and_never_injected(self) -> None:
        text = render_card(
            {
                "kind": "approval",
                "title": '<b>x</b> & "quotes" <script>go()</script>',
                "details": "5 < 6 & 7 > 4 <i>ital</i>",
                "choices": ["<Approve>", "Deny & co"],
                "choice_notes": ["note <b>one</b>", "note & two"],
                "recommendation": "<Approve>",
                "consequence": "a < b & c > d",
                "prohibitions": ["no <script> tags", "no & ampersands"],
                "links": ["https://example.test/a?b=1&c=2"],
                "agent_name": "<Hermes>",
                "runtime": "r&d",
                "repo": "par/pa&phe",
                "worktree": "w<1>",
                "ticket": "LS<3118>",
                "risk": "high",
                "expires_at": EXPIRES_AT,
            }
        )
        # Only the renderer's own markup survives: title + four bold labels.
        self.assertEqual(text.count("<b>"), 5)
        self.assertEqual(text.count("</b>"), 5)
        self.assertNotIn("<script", text)
        self.assertNotIn("<i>", text)
        self.assertIn("&lt;script&gt;go()&lt;/script&gt;", text)
        self.assertIn("5 &lt; 6 &amp; 7 &gt; 4 &lt;i&gt;ital&lt;/i&gt;", text)
        self.assertIn("1. &lt;Approve&gt; (recommended) — note &lt;b&gt;one&lt;/b&gt;", text)
        self.assertIn("&lt;Hermes&gt; · r&amp;d · par/pa&amp;phe · w&lt;1&gt; · LS&lt;3118&gt;", text)
        self.assertIn(
            '<a href="https://example.test/a?b=1&amp;c=2">'
            "https://example.test/a?b=1&amp;c=2</a>",
            text,
        )

    def test_oversized_content_trims_with_a_visible_marker_under_budget(self) -> None:
        payload = full_card()
        payload["details"] = "x" * 4000
        text = render_card(payload)
        self.assertIn(TRIM_MARKER, text)
        self.assertLessEqual(message_units(text), MESSAGE_BUDGET)
        self.assertEqual(render_card(payload), text)

    def test_trim_order_is_longest_free_text_first(self) -> None:
        payload = {
            "kind": "approval",
            "title": "Trim",
            "details": "d" * 2000,
            "links": [f"https://example.test/{index:03d}/" + "x" * 470 for index in range(8)],
            "expires_at": EXPIRES_AT,
        }
        text = render_card(payload)
        self.assertEqual(text.count(TRIM_MARKER), 1)
        self.assertLessEqual(message_units(text), MESSAGE_BUDGET)
        # The longest free text (the links block) shrank; the shorter one
        # stayed whole, and the cut keeps the head of the block only.
        self.assertIn("d" * 2000, text)
        self.assertIn(payload["links"][0], text)
        self.assertNotIn(payload["links"][-1], text)
        self.assertIn("<b>Links:</b>", text)
        self.assertLess(text.index("<b>Links:</b>"), text.index(TRIM_MARKER))

    def test_emoji_content_is_budgeted_in_utf16_units(self) -> None:
        payload = {
            "kind": "approval",
            "title": "Emoji",
            "details": "🧵" * 2100,  # 4200 UTF-16 units
            "expires_at": EXPIRES_AT,
        }
        text = render_card(payload)
        self.assertLessEqual(message_units(text), MESSAGE_BUDGET)
        self.assertIn(TRIM_MARKER, text)

    def test_lone_surrogates_cannot_fail_the_render(self) -> None:
        text = render_card(
            {"kind": "question", "title": "ok\ud800end", "expires_at": EXPIRES_AT}
        )
        self.assertNotIn("\ud800", text)
        self.assertIn("ok", text)
        self.assertIn("end", text)

    def test_plain_french_card_renders_without_codes(self) -> None:
        # R6: a card written to the standard renders with no tracker key or
        # commit hash in its headline or choices, and its context opens with
        # the situation.
        situation = "La sauvegarde de 22 h a échoué deux fois cette semaine."
        question = "Garder la sauvegarde du soir, ou changer d'horaire ?"
        payload = {
            "request_id": "11111111-1111-4111-8111-111111111111",
            "version": 1,
            "kind": "question",
            "title": question,
            "details": situation,
            "choices": ["Garder", "Changer d'horaire", "Déplacer maintenant", "Ne rien changer"],
            "choice_notes": [
                "Rien ne change ce soir.",
                "La sauvegarde part plus tôt.",
                "Le fichier reste sur place.",
                "La question revient demain.",
            ],
            "recommendation": "Changer d'horaire",
            "expires_at": EXPIRES_AT,
        }

        telegram_lines = render_card(payload).splitlines()
        telegram_title = next(line for line in telegram_lines if question in line)
        telegram_choices = [line for line in telegram_lines if line.split(".", 1)[0].isdigit()]
        self.assertTrue(telegram_choices)
        self.assertEqual(_codes_in(telegram_title), [], telegram_title)
        for line in telegram_choices:
            self.assertEqual(_codes_in(line), [], line)

        printed: list[str] = []
        console_module.ConsoleDestination(port=8792, write=printed.append).notify(payload)
        console_lines = "\n".join(printed).splitlines()
        console_title = next(line for line in console_lines if question in line)
        console_choices = next(line for line in console_lines if "choices:" in line)
        console_context = next(line for line in console_lines if situation in line)
        self.assertEqual(_codes_in(console_title), [], console_title)
        self.assertEqual(_codes_in(console_choices), [], console_choices)
        self.assertTrue(
            console_context.split(": ", 1)[1].startswith(situation), console_context
        )

    def test_status_message_renders_identity_and_no_decision_sections(self) -> None:
        text = render_card(
            {
                "kind": "notify",
                "title": "Nightly sync finished.",
                "message": "Two warnings; nothing failed.",
                "agent_name": "Hermes",
            }
        )
        self.assertEqual(
            text,
            "Hermes\n\nStatus\n\n<b>Nightly sync finished.</b>\n\nTwo warnings; nothing failed.",
        )
        self.assertNotIn("Recommended", text)
        self.assertNotIn(HINT, text)
        self.assertNotIn("Expires", text)
        self.assertNotIn("1. ", text)

    def test_status_without_provenance_has_no_identity_line(self) -> None:
        text = render_card({"kind": "notify", "title": "Sync.", "message": "Done."})
        self.assertEqual(text, "Status\n\n<b>Sync.</b>\n\nDone.")


if __name__ == "__main__":
    unittest.main()
