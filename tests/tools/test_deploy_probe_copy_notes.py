"""The deploy probe's own witness: it prints exactly the three G7 lines."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "deploy_probe_copy_notes.py"

EXPECTED = [
    "descriptions_with_context_first=4",
    "junk_notes=3",
    "plain_notes=0",
]


class TestDeployProbeCopyNotes(unittest.TestCase):
    def test_probe_prints_the_expected_lines(self) -> None:
        result = subprocess.run(
            [sys.executable, str(TOOL)],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), EXPECTED)


if __name__ == "__main__":
    unittest.main()
