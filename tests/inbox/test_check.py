"""Owner-side configuration preflight tests."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_check():
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from paraphe import check as module

    return module


class FakeTelegramAPI:
    def __init__(self, token: str) -> None:
        self.token = token
        self.sent: list[tuple[int, str, object]] = []

    def get_me(self) -> dict[str, object]:
        return {"id": 42, "username": "paraphe_test_bot"}

    def send_message(self, chat_id: int, text: str, reply_markup: object) -> dict[str, object]:
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": 7}


class TestTelegramCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_check()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmpdir = Path(self._tmpdir.name)
        self.token = "123456:test-secret-token"
        self.token_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self.environ = {
            "PARAPHE_BOT_TOKEN": self.token,
            "PARAPHE_OWNER_TELEGRAM_ID": "999001",
            "PARAPHE_MCP_CREATE_BEARER": "test-create-bearer",
            "PARAPHE_STORE_PATH": "unused-check.sqlite",
        }

    def _run(self, api: object) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(self.module, "TelegramBotAPI", return_value=api):
            code = self.module.telegram(environ=self.environ, out=out, err=err)
        return code, out.getvalue() + err.getvalue()

    def test_the_pair_passes_only_after_identity_and_delivery(self) -> None:
        api = FakeTelegramAPI(self.token)
        code, output = self._run(api)
        self.assertEqual(code, 0)
        self.assertEqual(
            api.sent,
            [(999001, self.module.CHECK_MESSAGE, None)],
        )
        self.assertIn("@paraphe_test_bot", output)
        self.assertIn("passed for owner 999001", output)
        self.assertNotIn(self.token, output)
        self.assertNotIn(self.token_url, output)

    def test_a_refused_token_is_named_without_leaking_it(self) -> None:
        class RefusedToken(FakeTelegramAPI):
            def get_me(self) -> dict[str, object]:
                raise self_module.NotifyRejected(self_url)

        self_module = self.module
        self_url = self.token_url
        code, output = self._run(RefusedToken(self.token))
        self.assertEqual(code, 2)
        self.assertIn("refused the configured bot token", output)
        self.assertNotIn(self.token, output)
        self.assertNotIn(self.token_url, output)

    def test_a_token_transport_failure_does_not_claim_telegram_refused_it(self) -> None:
        class UnreachableToken(FakeTelegramAPI):
            def get_me(self) -> dict[str, object]:
                raise self_module.TelegramAPIError(self_url)

        self_module = self.module
        self_url = self.token_url
        code, output = self._run(UnreachableToken(self.token))
        self.assertEqual(code, 2)
        self.assertIn("could not reach the Telegram Bot API", output)
        self.assertNotIn("refused", output)
        self.assertNotIn(self.token, output)
        self.assertNotIn(self.token_url, output)

    def test_refused_delivery_names_the_owner_and_next_steps_without_leaking(self) -> None:
        class RefusedDelivery(FakeTelegramAPI):
            def send_message(
                self, chat_id: int, text: str, reply_markup: object
            ) -> dict[str, object]:
                raise self_module.NotifyRejected(self_url)

        self_module = self.module
        self_url = self.token_url
        code, output = self._run(RefusedDelivery(self.token))
        self.assertEqual(code, 2)
        self.assertIn("owner 999001", output)
        self.assertIn("check the id", output)
        self.assertIn("open a private chat", output)
        self.assertNotIn(self.token, output)
        self.assertNotIn(self.token_url, output)

    def test_a_delivery_transport_failure_does_not_blame_the_owner_id(self) -> None:
        class UnreachableDelivery(FakeTelegramAPI):
            def send_message(
                self, chat_id: int, text: str, reply_markup: object
            ) -> dict[str, object]:
                raise self_module.TelegramAPIError(self_url)

        self_module = self.module
        self_url = self.token_url
        code, output = self._run(UnreachableDelivery(self.token))
        self.assertEqual(code, 2)
        self.assertIn("could not reach the Telegram Bot API", output)
        self.assertNotIn("check the id", output)
        self.assertNotIn("open a private chat", output)
        self.assertNotIn(self.token, output)
        self.assertNotIn(self.token_url, output)

    def test_no_phone_configuration_is_named(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(self.module, "TelegramBotAPI") as api:
            code = self.module.telegram(
                environ={"PARAPHE_CONFIG_PATH": str(self.tmpdir / "absent.toml")},
                out=out,
                err=err,
            )
        self.assertEqual(code, 2)
        self.assertIn("no phone destination is configured", err.getvalue())
        api.assert_not_called()

    def test_entry_point_routes_the_telegram_check(self) -> None:
        from paraphe import __main__ as entry

        api = FakeTelegramAPI(self.token)
        out = io.StringIO()
        err = io.StringIO()
        with (
            mock.patch.dict("os.environ", self.environ, clear=True),
            mock.patch.object(self.module, "TelegramBotAPI", return_value=api),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", err),
        ):
            code = entry.main(["check", "telegram"])
        output = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn(self.token, output)
        self.assertNotIn(self.token_url, output)


if __name__ == "__main__":
    unittest.main()
