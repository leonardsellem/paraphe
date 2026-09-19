"""Owner-side commands for relocating the Inbox store."""

from __future__ import annotations

import sys

from .inbox.config import SetupError
from .inbox import store as _store

USAGE = """\
Usage: paraphe store relocate [--move]

Copy the store from the location used by earlier releases to the current
per-user data location. The source is kept unless --move is supplied.
"""


def main(argv: list[str]) -> int:
    if argv in (["-h"], ["--help"], ["help"]):
        print(USAGE, end="")
        return 0
    if argv not in (["relocate"], ["relocate", "--move"]):
        print(USAGE, end="", file=sys.stderr)
        return 2

    move = argv == ["relocate", "--move"]
    source = _store.LEGACY_STORE_PATH
    target = _store.default_store_path()
    try:
        _store.relocate_store(source, target, move=move)
    except (SetupError, OSError) as exc:
        print(f"paraphe: {exc}", file=sys.stderr)
        return 2

    action = "Moved" if move else "Copied"
    print(f"{action} store from {source} to {target}.")
    return 0
