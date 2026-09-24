"""Entry point for the ``paraphe`` command and ``python3 -m paraphe``."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version
from pathlib import Path

DEFAULT_CONFIG_FILENAME = "paraphe.toml"
HELP_FLAGS = frozenset({"-h", "--help", "help"})
USAGE = """\
Paraphe - the self-hosted owner decision inbox an agent asks and you answer.

  paraphe [--config PATH]   start the server
  paraphe ask QUESTION ...  create a card from the shell; prints the request id
  paraphe wait REQUEST_ID   block until the card is answered; prints the answer
  paraphe --version         show the installed version
  paraphe check telegram    verify the configured phone destination
  paraphe store relocate    safely copy a legacy store to the current location
  paraphe --help            show this message

`paraphe ask` and `paraphe wait` complete the whole ask/answer loop from a
shell: ask prints the request id, wait exits when the owner taps (0 answered,
3 expired or not answerable, 4 unknown). Run `paraphe ask --help` for options.

Configuration comes from a TOML file named by --config (or PARAPHE_CONFIG_PATH,
or ./paraphe.toml), and from the environment. The environment wins.

  PARAPHE_BOT_TOKEN           Telegram bot token for the tap surface
  PARAPHE_OWNER_TELEGRAM_ID   the owner's Telegram user id
  PARAPHE_MCP_CREATE_BEARER   the bearer agent callers present to create cards
  PARAPHE_STORE_PATH          data location (default: your per-user data directory)
  PARAPHE_MCP_HOST            bind address (default 127.0.0.1)
  PARAPHE_MCP_PORT            bind port
  PARAPHE_MCP_URL             the ask/wait commands' endpoint override

Copy `config.example.toml` to `paraphe.toml` to start from a documented minimum.
The suite runs with: python3 -m unittest discover -s tests -p 'test_*.py'
"""


def _configured() -> bool:
    if any(name.startswith("PARAPHE_") for name in os.environ):
        return True
    return Path(os.environ.get("PARAPHE_CONFIG_PATH") or DEFAULT_CONFIG_FILENAME).is_file()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in HELP_FLAGS:
        print(USAGE, end="")
        return 0
    if args[:1] == ["--version"]:
        print(version("paraphe"))
        return 0
    if args and args[0] == "check":
        from .check import main as check_main

        return check_main(args[1:])
    if args and args[0] == "store":
        from .store_cli import main as store_main

        return store_main(args[1:])
    if args and args[0] in {"ask", "wait"}:
        from .cli import main as cli_main

        return cli_main(args)
    while args[:1] == ["--config"]:
        if len(args) < 2:
            print("paraphe: --config needs a path", file=sys.stderr)
            return 2
        os.environ["PARAPHE_CONFIG_PATH"] = args[1]
        args = args[2:]
    if not _configured():
        print(USAGE, end="")
        return 0

    from .inbox.config import SetupError
    from .inbox.runtime import main as run

    try:
        return run()
    except SetupError as exc:
        print(f"paraphe: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
