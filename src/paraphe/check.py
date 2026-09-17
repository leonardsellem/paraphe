"""Owner-side configuration preflights."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, TextIO

from .inbox import NotifyRejected
from .inbox.config import SetupError, load_settings
from .inbox.runtime import TelegramAPIError, TelegramBotAPI

CHECK_MESSAGE = "Paraphe Telegram check passed. This is a setup test, not a decision card."
NO_PHONE_DESTINATION = "paraphe: no phone destination is configured"
TELEGRAM_UNREACHABLE = "paraphe: could not reach the Telegram Bot API"


def telegram(
    *,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    config_path = Path(environ.get("PARAPHE_CONFIG_PATH") or "paraphe.toml")
    if not config_path.is_file() and not any(
        environ.get(name, "").strip()
        for name in ("PARAPHE_BOT_TOKEN", "PARAPHE_OWNER_TELEGRAM_ID")
    ):
        print(NO_PHONE_DESTINATION, file=err)
        return 2
    try:
        settings = load_settings(config_path=config_path, environ=environ)
    except SetupError as exc:
        print(f"paraphe: {exc}", file=err)
        return 2
    if not settings.bot_token or settings.owner_telegram_id is None:
        print(NO_PHONE_DESTINATION, file=err)
        return 2

    api = TelegramBotAPI(settings.bot_token)
    try:
        identity = api.get_me()
    except NotifyRejected:
        print("paraphe: Telegram refused the configured bot token", file=err)
        return 2
    except TelegramAPIError:
        print(TELEGRAM_UNREACHABLE, file=err)
        return 2
    username = identity.get("username")
    bot = f"@{username}" if isinstance(username, str) and username else "the configured bot"
    print(f"Telegram token identifies {bot}.", file=out)
    try:
        api.send_message(settings.owner_telegram_id, CHECK_MESSAGE, None)
    except NotifyRejected:
        print(
            f"paraphe: Telegram could not deliver to owner {settings.owner_telegram_id}; "
            "check the id and open a private chat with the bot first",
            file=err,
        )
        return 2
    except TelegramAPIError:
        print(TELEGRAM_UNREACHABLE, file=err)
        return 2
    print(f"Telegram check passed for owner {settings.owner_telegram_id}.", file=out)
    return 0


def main(argv: list[str]) -> int:
    if argv != ["telegram"]:
        print("paraphe: check needs exactly 'telegram'", file=sys.stderr)
        return 2
    return telegram()
