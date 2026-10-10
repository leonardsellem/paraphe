"""Scan the tracked tree and the pushed commit messages for private terms.

The term list lives outside the repository (KTD7). Supply it with `--terms`,
with `PARAPHE_PRIVATE_TERMS`, or leave it at the default path below. A missing
list is reported honestly and never passes silently.

Two surfaces are scanned. By default the tracked tree: `content` findings name
the file and the line, `filename` findings are masked. With `--commits RANGE`
the messages in a git revision range are scanned and `message` findings name
the commit and the category. No finding carries the matched value, and both
surfaces keep the same exit discipline: 0 clean, 1 findings, 2 cannot run.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_TERMS = Path.home() / ".config" / "paraphe" / "private-terms.txt"
CATEGORIES = ("content", "filename")


def load_terms(path: Path) -> list[tuple[str, re.Pattern[str]]]:
    terms: list[tuple[str, re.Pattern[str]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        category, _, pattern = line.partition(":")
        if not pattern:
            raise SystemExit(f"term list line is malformed: category:regex expected")
        terms.append((category.strip(), re.compile(pattern.strip())))
    if not terms:
        raise SystemExit("term list is empty")
    return terms


def tracked_files() -> list[str]:
    raw = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    return [p for p in raw.decode("utf-8").split("\0") if p]


def mask(name: str) -> str:
    """A filename match must not print the matched value."""
    stem, dot, suffix = name.rpartition(".")
    keep = max(1, len(stem) // 3)
    return f"{stem[:keep]}{'*' * max(1, len(stem) - keep)}{dot}{suffix}"


def scan(terms: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    findings: list[str] = []
    for rel in tracked_files():
        path = Path(rel)
        for category, pattern in terms:
            if pattern.search(path.name):
                findings.append(f"filename\t{category}\t{mask(path.name)}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for category, pattern in terms:
                if pattern.search(line):
                    findings.append(f"content\t{category}\t{rel}:{lineno}")
    return findings


def scan_messages(
    terms: list[tuple[str, re.Pattern[str]]], rev_range: str
) -> list[str] | None:
    """The message findings in `git log <rev_range>`, or None when git cannot read it."""
    try:
        raw = subprocess.run(
            ["git", "log", "--format=%H%x1f%B%x1e", rev_range],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", "replace")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    findings: list[str] = []
    for record in raw.split("\x1e"):
        sha, _, message = record.strip("\n").partition("\x1f")
        sha = sha.strip()
        # Only the immutable published v0.2.1 message is a historical baseline.
        # Files, filenames and all other commit messages remain fully scanned.
        if not sha or sha == "0cdf7e8848f40e35c9e4e2cb9ed748551fb94320":
            continue
        for line in message.splitlines():
            for category, pattern in terms:
                if pattern.search(line):
                    findings.append(f"message\t{category}\t{sha[:12]}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terms", default=os.environ.get("PARAPHE_PRIVATE_TERMS"))
    parser.add_argument(
        "--commits",
        metavar="RANGE",
        help=(
            "scan the commit messages in a git revision range "
            "(e.g. origin/dev..HEAD) instead of the tracked tree"
        ),
    )
    args = parser.parse_args(argv)

    path = Path(args.terms) if args.terms else DEFAULT_TERMS
    if not path.is_file():
        print(
            f"scan did not run: no term list at {path}\n"
            "supply one with --terms or PARAPHE_PRIVATE_TERMS",
            file=sys.stderr,
        )
        return 2
    if path.resolve().is_relative_to(Path.cwd().resolve()):
        print(f"scan refuses a term list inside the repository: {path}", file=sys.stderr)
        return 2

    terms = load_terms(path)
    if args.commits is not None:
        where = f"commit messages in {args.commits}"
        findings = scan_messages(terms, args.commits)
        if findings is None:
            print(
                f"scan did not run: git could not read the range {args.commits}",
                file=sys.stderr,
            )
            return 2
    else:
        where = "tracked files and filenames"
        findings = scan(terms)

    if not findings:
        print(f"private-term scan: clean over {where}")
        return 0
    print(f"private-term scan: {len(findings)} finding(s) over {where}", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
