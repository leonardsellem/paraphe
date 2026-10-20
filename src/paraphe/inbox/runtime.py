"""One-process Paraphe runtime: MCP HTTP plus Telegram long polling."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from ..adapters.console import ConsoleDestination
from ..adapters.telegram import TelegramAdapter

from . import Inbox, NotifyRejected
from .claim import NullTelegramPort
from .config import SetupError, load_settings
from .http import is_loopback_host

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8787
DEFAULT_POLL_SECONDS = 25
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 15


class TelegramAPIError(Exception):
    """Telegram failed without exposing the token-bearing request URL."""


class TelegramBotAPI:
    def __init__(self, token: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/"

    def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        if not isinstance(result, list):
            raise TelegramAPIError("Telegram returned invalid updates")
        return result

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe", {})
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram returned an invalid bot identity")
        return result

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram returned an invalid message")
        return result

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict[str, Any]
    ) -> None:
        self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
            },
        )

    def answer_callback_query(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self._url + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        poll_seconds = int(payload.get("timeout", 0))
        timeout = (
            max(TELEGRAM_REQUEST_TIMEOUT_SECONDS, poll_seconds + 10)
            if poll_seconds > 0 else TELEGRAM_REQUEST_TIMEOUT_SECONDS
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
            except (OSError, ValueError):
                raise TelegramAPIError("Telegram Bot API request failed") from None
        except (OSError, ValueError, urllib.error.URLError):
            raise TelegramAPIError("Telegram Bot API request failed") from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise TelegramAPIError("Telegram Bot API request failed") from None
        if isinstance(body, dict) and body.get("ok") is False:
            raise NotifyRejected("Telegram Bot API rejected the request") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramAPIError("Telegram Bot API request failed")
        return body.get("result")


def _env_int(
    environ: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SetupError(f"{name} is invalid") from None
    if value < minimum or value > maximum:
        raise SetupError(f"{name} is invalid")
    return value


class Runtime:
    def __init__(
        self,
        *,
        inbox: Inbox,
        telegram: TelegramAdapter | None,
        api: TelegramBotAPI | None,
        server: Any,
        poll_seconds: int,
    ) -> None:
        self.inbox = inbox
        self.telegram = telegram
        self.api = api
        self.server = server
        self.poll_seconds = poll_seconds
        self.next_update_id = 0
        self._stopping = threading.Event()
        self._closed = False
        self._offset_ready = False
        self._reconciled = False

    @classmethod
    def start(cls, environ: Mapping[str, str] | None = None) -> "Runtime":
        environ = os.environ if environ is None else environ
        settings = load_settings(
            config_path=environ.get("PARAPHE_CONFIG_PATH") or None,
            environ=environ,
        )
        store_path = settings.store_path
        host = environ.get("PARAPHE_MCP_HOST") or DEFAULT_MCP_HOST
        port = _env_int(
            environ, "PARAPHE_MCP_PORT", DEFAULT_MCP_PORT, minimum=0, maximum=65535
        )
        poll_seconds = _env_int(
            environ,
            "PARAPHE_TELEGRAM_POLL_SECONDS",
            DEFAULT_POLL_SECONDS,
            minimum=1,
            maximum=50,
        )
        inbox = Inbox(
            owner_telegram_id=settings.owner_telegram_id,
            default_ttl_seconds=settings.default_ttl_seconds,
            floor_ttl_seconds=settings.floor_ttl_seconds,
            store_path=store_path,
            mcp_create_bearer=settings.mcp_create_bearer,
            owner_answer_token=settings.owner_answer_token,
            bot_token=settings.bot_token,
        )
        if settings.owner_answer_token and not is_loopback_host(host):
            raise SetupError("the answer path refuses a non-loopback bind")
        if settings.bot_token:
            api = TelegramBotAPI(settings.bot_token)
            telegram = TelegramAdapter(
                inbox,
                api,
                owner_id=settings.owner_telegram_id,
                bot_token=settings.bot_token,
            )
            destination: Any = telegram
        else:
            api = None
            telegram = None
            destination = ConsoleDestination(host=host, port=port)
        inbox._notifier = destination
        inbox._telegram = destination if telegram is not None else NullTelegramPort()
        inbox._notifications_ready = telegram is None
        try:
            with inbox._lock:
                store = inbox._ensure_store()
                saved_offset = store.load_telegram_next_offset() if telegram is not None else None
            server = inbox.serve(host=host, port=port)
        except Exception:
            inbox.close()
            raise
        runtime = cls(
            inbox=inbox,
            telegram=telegram,
            api=api,
            server=server,
            poll_seconds=poll_seconds,
        )
        if saved_offset is not None:
            runtime.next_update_id = saved_offset
            runtime._offset_ready = True
        return runtime

    def _store(self):
        store = self.inbox._store
        if store is None:
            raise RuntimeError("store is not open")
        return store

    def _persist_offset(self, offset: int) -> None:
        with self.inbox._lock:
            self._store().save_telegram_next_offset(offset)
            self.next_update_id = offset

    def _require_update_id(self, update: object) -> int:
        if not isinstance(update, Mapping):
            raise TelegramAPIError("Telegram returned invalid updates")
        update_id = update.get("update_id")
        if type(update_id) is not int or update_id < 0:
            raise TelegramAPIError("Telegram returned invalid updates")
        return update_id

    def _restore_telegram_offset(self) -> None:
        with self.inbox._lock:
            loaded = self._store().load_telegram_next_offset()
        if loaded is not None:
            self.next_update_id = loaded
            self._offset_ready = True
            return
        assert self.api is not None
        updates = self.api.get_updates(offset=-1, timeout=0)
        newest: int | None = None
        for update in updates:
            update_id = self._require_update_id(update)
            newest = update_id if newest is None else max(newest, update_id)
        self._persist_offset(0 if newest is None else newest + 1)
        self._offset_ready = True

    def poll_once(self) -> None:
        assert self.api is not None and self.telegram is not None
        if not self._offset_ready:
            raise TelegramAPIError("Telegram bootstrap is pending")
        updates = self.api.get_updates(
            offset=self.next_update_id, timeout=self.poll_seconds
        )
        for update in updates:
            update_id = self._require_update_id(update)
            self.telegram.handle_update(update)
            self._persist_offset(max(self.next_update_id, update_id + 1))

    def run(self) -> None:
        if self.api is None:
            # Local run: the HTTP surface serves MCP and the answer path; there
            # is no tap transport to poll.
            self._stopping.wait()
            return
        while not self._stopping.is_set() and not self._offset_ready:
            started = time.monotonic()
            print("Telegram bootstrap started", flush=True)
            try:
                self._restore_telegram_offset()
            except Exception as exc:
                print(
                    f"Telegram bootstrap failed: {type(exc).__name__} "
                    f"elapsed={time.monotonic() - started:.3f}s", flush=True,
                )
                self._stopping.wait(1)
            else:
                print(f"Telegram bootstrap completed elapsed={time.monotonic() - started:.3f}s", flush=True)
        if self._stopping.is_set():
            return
        if not self._reconciled:
            self._reconciled = True
            with self.inbox._lock:
                self.inbox._notifications_ready = True
            started = time.monotonic()
            print("Telegram reconcile started", flush=True)
            try:
                result = self.inbox.reconcile_notifications()
            except Exception as exc:
                print(
                    f"Telegram reconcile failed: {type(exc).__name__} "
                    f"elapsed={time.monotonic() - started:.3f}s", flush=True,
                )
            else:
                state = "degraded" if result is False else "completed"
                print(f"Telegram reconcile {state} elapsed={time.monotonic() - started:.3f}s", flush=True)
        while not self._stopping.is_set():
            try:
                self.poll_once()
            except (TelegramAPIError, NotifyRejected):
                self._stopping.wait(1)

    def stop(self) -> None:
        self._stopping.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        try:
            self.server.close()
        finally:
            self.inbox.close()


def main() -> int:
    runtime = Runtime.start()

    def stop(_signum: int, _frame: object) -> None:
        runtime.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    answer_state = "on" if runtime.inbox._owner_answer_token else "off"
    print(
        f"Paraphe ready: mcp http://{runtime.server.host}:{runtime.server.port}/mcp "
        f"answer-path {answer_state}",
        flush=True,
    )
    try:
        runtime.run()
    finally:
        runtime.close()
    return 0
