"""The published contracts: the tool surface, the served text, the destination."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paraphe.adapters import console as console_module
from paraphe import inbox as inbox_module

Inbox = inbox_module.Inbox
TOOL_NAMES = inbox_module.TOOL_NAMES

CREATE = "test-mcp-bearer"


def _documented_tools() -> list[str]:
    text = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    section = text.split("## Tools", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE)


class TestPublishedContracts(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmpdir = Path(self._tmpdir.name)

    def _inbox(self, **kwargs: object) -> Inbox:
        inbox = Inbox(
            owner_telegram_id=999001,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=self.tmpdir / "inbox.sqlite",
            mcp_create_bearer=CREATE,
            **kwargs,
        )
        self.addCleanup(inbox.close)
        return inbox

    def test_a_card_stores_the_source_thread_it_was_given(self) -> None:
        # The origin is descriptive: nothing is woken from it, so the value
        # the caller sent is the value the card keeps.
        inbox = self._inbox()
        created = inbox.call_tool(
            "ask_question",
            {"question": "Ready?", "external_id": "origin", "source_thread": "desktop:abc"},
            bearer=CREATE,
        )
        stored = inbox._cards[created["request_id"]]
        self.assertEqual(stored.source_thread, "desktop:abc")

    def test_the_served_tools_are_exactly_the_documented_ones(self) -> None:
        documented = _documented_tools()
        self.assertEqual(sorted(TOOL_NAMES), sorted(documented))
        self.assertEqual(self._inbox().list_tools(), list(TOOL_NAMES))

    def test_every_served_tool_carries_a_description_not_just_its_name(self) -> None:
        descriptors = self._inbox().list_tool_descriptors()
        self.assertEqual([item["name"] for item in descriptors], list(TOOL_NAMES))
        for item in descriptors:
            description = item["description"]
            self.assertIsInstance(description, str)
            self.assertTrue(description.strip())
            self.assertNotEqual(description, item["name"])

    def test_the_served_text_carries_the_async_protocol(self) -> None:
        inbox = self._inbox()
        how = inbox.call_tool("how_to_use", {}, bearer=CREATE)
        self.assertIn("Record the request_id", how)
        self.assertIn("wait_seconds", how)
        self.assertIn("get_response", how)
        self.assertIn("list_unprocessed", how)
        self.assertIn("paraphe wait", how)
        descriptors = {item["name"]: item["description"] for item in inbox.list_tool_descriptors()}
        self.assertIn("request_id", descriptors["ask_question"])
        self.assertIn("get_response", descriptors["ask_question"])
        self.assertIn("drain", descriptors["get_response"].lower())
        self.assertIn("drain", descriptors["list_unprocessed"].lower())

    def test_the_served_text_teaches_provenance(self) -> None:
        inbox = self._inbox()
        how = inbox.call_tool("how_to_use", {}, bearer=CREATE)
        for token in ("runtime", "repo", "worktree", "ticket"):
            self.assertIn(token, how)
        descriptors = {item["name"]: item["description"] for item in inbox.list_tool_descriptors()}
        for tool in ("ask_question", "request_approval", "request_feedback", "update_request"):
            self.assertIn("runtime/repo/worktree/ticket", descriptors[tool])

    def test_a_created_card_renders_its_identity_line_from_the_payload(self) -> None:
        from paraphe.adapters.telegram import render_card

        class Recorder:
            def __init__(self) -> None:
                self.sent: list[dict] = []

            def notify(self, payload: dict) -> None:
                self.sent.append(payload)

        inbox = self._inbox()
        recorder = Recorder()
        inbox._notifier = recorder
        inbox.call_tool(
            "ask_question",
            {
                "question": "Ready to deploy?",
                "external_id": "render-identity-1",
                "agent_name": "Hermes",
                "runtime": "Claude Code",
                "repo": "paraphe",
                "worktree": "feature-better-tg-cards",
                "ticket": "card-3119",
                "expires_in_seconds": 900,
            },
            bearer=CREATE,
        )
        text = render_card(recorder.sent[-1])
        self.assertEqual(
            text.split("\n", 1)[0],
            "Hermes · Claude Code · paraphe · feature-better-tg-cards · card-3119",
        )

    def test_the_served_text_teaches_the_reply_rule(self) -> None:
        inbox = self._inbox()
        how = inbox.call_tool("how_to_use", {}, bearer=CREATE)
        self.assertIn("reply", how)
        self.assertIn("telegram-reply", how)
        self.assertIn("re-ask", how)
        descriptors = {item["name"]: item["description"] for item in inbox.list_tool_descriptors()}
        self.assertIn("telegram-reply", descriptors["get_response"])

    def test_no_tool_answers_or_claims_a_card(self) -> None:
        for name in TOOL_NAMES:
            self.assertNotIn("answer", name.split("_"))
            self.assertNotIn("claim", name.split("_"))

    def test_the_console_destination_implements_the_two_method_contract(self) -> None:
        printed: list[str] = []
        destination = console_module.ConsoleDestination(port=8792, write=printed.append)
        destination.notify(
            {"request_id": "rid", "version": 1, "kind": "question", "question": "Ready?"}
        )
        destination.edit_and_strip("rid", 1)
        self.assertIn("rid", printed[0])
        self.assertIn("rid", printed[1])


if __name__ == "__main__":
    unittest.main()
