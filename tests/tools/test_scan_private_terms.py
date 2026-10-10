"""The private-term gate's own witness: content, filenames and commit messages."""

from __future__ import annotations

import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "scan_private_terms.py"

# Synthetic terms only. The real list lives outside this repository and must
# never appear in it, so the tests bring their own list.
VALUE = "zq-suite-term"
LIST = "suite_term:zq-suite-term\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=suite",
            "-c",
            "user.email=suite@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class ScanToolCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "--quiet")
        self.terms = self.root / "terms.txt"  # a term list lives outside the repository
        self.terms.write_text(LIST, encoding="utf-8")

    def _run(self, *args: str, terms: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), "--terms", str(terms or self.terms), *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )

    def _commit(self, message: str) -> None:
        _git(self.repo, "commit", "--allow-empty", "--quiet", "-m", message)


class TestTreeScan(ScanToolCase):
    def test_a_clean_tree_passes(self) -> None:
        (self.repo / "notes.md").write_text("nothing to see\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertIn("clean", result.stdout)

    def test_a_seeded_tracked_file_fires_with_the_category_and_not_the_value(self) -> None:
        (self.repo / "notes.md").write_text(f"a line with {VALUE} in it\n", encoding="utf-8")
        _git(self.repo, "add", "notes.md")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("content", result.stderr)
        self.assertIn("suite_term", result.stderr)
        self.assertNotIn(VALUE, result.stderr)
        self.assertNotIn(VALUE, result.stdout)

    def test_a_seeded_filename_fires_masked(self) -> None:
        (self.repo / f"{VALUE}-2031.md").write_text("clean\n", encoding="utf-8")
        _git(self.repo, "add", ".")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("filename", result.stderr)
        self.assertIn("suite_term", result.stderr)
        self.assertNotIn(VALUE, result.stderr)


class TestCommitsMode(ScanToolCase):
    def test_only_the_published_release_message_is_baselined(self) -> None:
        tool = runpy.run_path(str(TOOL))
        published = "0cdf7e8848f40e35c9e4e2cb9ed748551fb94320"
        new = published[:-1] + "1"  # the same short hash is not the same commit
        records = "".join(
            f"{sha}\x1fsubject carrying {VALUE}\x1e\n" for sha in (published, new)
        )
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, records.encode()),
        ):
            findings = tool["scan_messages"](tool["load_terms"](self.terms), "HEAD")
        self.assertEqual(findings, [f"message\tsuite_term\t{new[:12]}"])

        (self.repo / "notes.md").write_text(f"{VALUE}\n", encoding="utf-8")
        _git(self.repo, "add", "notes.md")
        tree = self._run()
        self.assertEqual(tree.returncode, 1)
        self.assertIn("content\tsuite_term\tnotes.md:1", tree.stderr)
        self.assertNotIn(VALUE, tree.stdout + tree.stderr)

    def test_a_clean_message_passes(self) -> None:
        self._commit("a clean subject")
        result = self._run("--commits", "HEAD")
        self.assertEqual(result.returncode, 0)
        self.assertIn("clean", result.stdout)

    def test_a_seeded_message_fires_with_the_commit_and_the_category(self) -> None:
        self._commit(f"subject carrying {VALUE}")
        result = self._run("--commits", "HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn("message", result.stderr)
        self.assertIn("suite_term", result.stderr)
        self.assertNotIn(VALUE, result.stderr)
        sha = _git(self.repo, "rev-parse", "--short=12", "HEAD").strip()
        self.assertIn(sha, result.stderr)

    def test_the_range_limits_the_scan(self) -> None:
        self._commit(f"the older subject carries {VALUE}")
        self._commit("the newer subject")
        limited = self._run("--commits", "HEAD^..HEAD")
        self.assertEqual(limited.returncode, 0)
        full = self._run("--commits", "HEAD")
        self.assertEqual(full.returncode, 1)

    def test_an_empty_range_passes(self) -> None:
        self._commit("a clean subject")
        result = self._run("--commits", "HEAD..HEAD")
        self.assertEqual(result.returncode, 0)
        self.assertIn("clean", result.stdout)


class TestCannotRun(ScanToolCase):
    def test_a_missing_term_list_reports_it_cannot_run(self) -> None:
        result = self._run(terms=self.root / "absent.txt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("did not run", result.stderr)

    def test_a_term_list_inside_the_repository_is_refused(self) -> None:
        inside = self.repo / "terms.txt"
        inside.write_text(LIST, encoding="utf-8")
        result = self._run(terms=inside)
        self.assertEqual(result.returncode, 2)
        self.assertIn("refuses", result.stderr)

    def test_an_unreadable_range_reports_it_cannot_run(self) -> None:
        self._commit("a clean subject")
        result = self._run("--commits", "no-such-ref..HEAD")
        self.assertEqual(result.returncode, 2)
        self.assertIn("did not run", result.stderr)


if __name__ == "__main__":
    unittest.main()
