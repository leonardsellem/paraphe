"""Read-only probe: the installed code teaches and flags card copy.

Prints the three post-deploy lines; exits non-zero if any differs from the
expected value. Creates no card, reads no store, needs no credential.

Run it the way the deploy gate does:

    PYTHONPATH=/usr/local/lib/paraphe/src python3 tools/deploy_probe_copy_notes.py
"""

from __future__ import annotations

import sys

ASKING_TOOLS = ("ask_question", "request_approval", "request_feedback", "update_request")
JUNK = {
    "question": "PROJ-123 : probe 1a2b3c4d",
    "context": "",
    "choices": ["Oui", "Non"],
}
PLAIN = {
    "question": "Garder la sauvegarde du soir ?",
    "context": "La sauvegarde de 22 h a échoué deux fois cette semaine.",
    "choices": ["Garder", "Changer"],
}


def main() -> int:
    from paraphe import inbox
    from paraphe.inbox.copy_notes import copy_notes

    values = {
        "descriptions_with_context_first": sum(
            "context first" in inbox.TOOL_DESCRIPTIONS[tool].lower() for tool in ASKING_TOOLS
        ),
        "junk_notes": len(copy_notes("question", JUNK)),
        "plain_notes": len(copy_notes("question", PLAIN)),
    }
    expected = {
        "descriptions_with_context_first": 4,
        "junk_notes": 3,
        "plain_notes": 0,
    }
    failed = False
    for key, wanted in expected.items():
        print(f"{key}={values[key]}")
        failed = failed or values[key] != wanted
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
