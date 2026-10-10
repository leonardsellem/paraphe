"""The shipped protocol keeps an interactive ask listening after turn end."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TestReturnPathProtocol(unittest.TestCase):
    def test_interactive_return_and_safe_consumption_are_documented(self) -> None:
        text = (ROOT / "skills" / "paraphe-return-path" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        requirements = {
            "R3": (
                'terminal(background=true, notify=true, command="paraphe wait <request_id>")',
                "creating turn", "before ending the turn", "not asynchronous return paths",
            ),
            "R4": (
                "get_response", "request_id", "external_id", "kind", "version",
                "response.choice", "processed_at", "current mandate", "once",
                "foreign", "stale", "Deny", "expired", "cancelled", "unknown",
                "already processed", "superseded", "compressed successor", "dead origin",
            ),
            "R5": ("3", "4", "receipt", "no action", "new reason"),
            "R6": ("report_execution", "mark_processed", "read back", "effect", "ACK"),
            "R8": ("hermes chat -q", "request_id", "continuation", "interactive parent", "never promise"),
        }
        for label, clauses in requirements.items():
            with self.subTest(requirement=label):
                self.assertIn(f"**{label}**", text)
                section = text.split(f"**{label}**", 1)[1].split("\n\n", 1)[0]
                for clause in clauses:
                    self.assertIn(clause, section)
        for obsolete in ("notify_on_complete=true", "check the binary first"):
            self.assertNotIn(obsolete, text)


if __name__ == "__main__":
    unittest.main()
