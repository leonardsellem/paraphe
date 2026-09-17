"""Inbox-seam tests for fail-closed setup."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

_OMIT = object()


def _load_inbox():
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from paraphe import inbox as module

    return module


_inbox = _load_inbox()
DEFAULT_TTL_SECONDS = _inbox.DEFAULT_TTL_SECONDS
FLOOR_TTL_SECONDS = _inbox.FLOOR_TTL_SECONDS
Inbox = _inbox.Inbox
SetupError = _inbox.SetupError
default_store_path = _inbox.default_store_path
_store_module = _inbox.store
from paraphe import __main__ as _entry
from paraphe.adapters import console as console_module
from paraphe.inbox import runtime as runtime_module

import contextlib
import io
import os


class TestSetup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmpdir = Path(self._tmpdir.name)
        # The location an earlier release used is a real path on an upgraded
        # host. Point it somewhere absent so the suite is host-independent.
        self._legacy = mock.patch.object(
            _store_module, "LEGACY_STORE_PATH", self.tmpdir / "no-legacy-store.sqlite"
        )
        self._legacy.start()
        self.addCleanup(self._legacy.stop)
        self._credentials = {
            "bot_token": "test-bot-token",
            "owner_telegram_id": 999001,
            "mcp_create_bearer": "test-mcp-bearer",
        }

    def _write_config(
        self,
        *,
        bot_token: object = _OMIT,
        owner_telegram_id: object = _OMIT,
        default_ttl_seconds: object = _OMIT,
        floor_ttl_seconds: object = _OMIT,
        mcp_create_bearer: object = _OMIT,
        owner_answer_token: object = _OMIT,
        store_path: object = _OMIT,
    ) -> Path:
        lines: list[str] = []
        if bot_token is not _OMIT:
            lines.append(f'bot_token = "{bot_token}"')
        if owner_telegram_id is not _OMIT:
            lines.append(f"owner_telegram_id = {owner_telegram_id}")
        if default_ttl_seconds is not _OMIT:
            lines.append(f"default_ttl_seconds = {default_ttl_seconds}")
        if floor_ttl_seconds is not _OMIT:
            lines.append(f"floor_ttl_seconds = {floor_ttl_seconds}")
        if mcp_create_bearer is not _OMIT:
            lines.append(f'mcp_create_bearer = "{mcp_create_bearer}"')
        if owner_answer_token is not _OMIT:
            lines.append(f'owner_answer_token = "{owner_answer_token}"')
        if store_path is not _OMIT:
            lines.append(f'store_path = "{store_path}"')
        path = self.tmpdir / "config.toml"
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path

    def _start(self, config_path: Path, environ: dict[str, str] | None = None):
        inbox = Inbox.start(config_path=config_path, environ=environ or {})
        self.addCleanup(inbox.close)
        return inbox

    def test_console_mode_starts_without_a_phone_destination(self) -> None:
        path = self._write_config(
            mcp_create_bearer="test-mcp-bearer",
            owner_answer_token="test-answer-token",
        )
        inbox = self._start(path)
        self.assertIsNone(inbox.owner_telegram_id)
        self.assertTrue(inbox.check_answer_token("test-answer-token"))
        self.assertFalse(inbox.check_answer_token("test-mcp-bearer"))

    def test_phone_destination_without_an_owner_identity_refuses(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            mcp_create_bearer="test-mcp-bearer",
        )
        with self.assertRaises(SetupError):
            self._start(path)

    def test_answer_credential_must_differ_from_the_create_credential(self) -> None:
        path = self._write_config(
            mcp_create_bearer="same-token",
            owner_answer_token="same-token",
        )
        with self.assertRaises(SetupError):
            self._start(path)

    def test_no_phone_destination_and_no_answer_credential_refuses(self) -> None:
        path = self._write_config(mcp_create_bearer="test-mcp-bearer")
        with self.assertRaises(SetupError):
            self._start(path)

    def test_the_default_config_filename_is_ignored(self) -> None:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "paraphe.toml"], cwd=Path(__file__).resolve().parents[2]
        )
        self.assertEqual(result.returncode, 0)

    def test_non_loopback_bind_with_the_answer_path_refuses(self) -> None:
        path = self._write_config(
            mcp_create_bearer="test-mcp-bearer",
            owner_answer_token="test-answer-token",
        )
        inbox = self._start(path)
        for host in ("0.0.0.0", "100.64.1.2", "8.8.8.8"):
            with self.assertRaises(SetupError):
                runtime_module.Runtime.start(
                    environ={
                        "PARAPHE_BOT_TOKEN": "test-bot-token",
                        "PARAPHE_OWNER_TELEGRAM_ID": "999001",
                        "PARAPHE_MCP_CREATE_BEARER": "test-mcp-bearer",
                        "PARAPHE_OWNER_ANSWER_TOKEN": "test-answer-token",
                        "PARAPHE_STORE_PATH": str(self.tmpdir / "bind.sqlite"),
                        "PARAPHE_MCP_HOST": host,
                    }
                )
        self.assertIsNotNone(inbox)

    def _write_config_with_answer(self, **overrides: object) -> Path:
        values = dict(self._credentials)
        values.pop("bot_token", None)
        values.pop("owner_telegram_id", None)
        values.update(owner_answer_token="test-answer-token", **overrides)
        return self._write_config(**values)

    def test_missing_owner_id_refuses_start(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            mcp_create_bearer="test-mcp-bearer",
        )
        with self.assertRaises(SetupError):
            self._start(path)

    def test_missing_mcp_bearer_refuses_start(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=999001,
        )
        with self.assertRaises(SetupError):
            self._start(path)

    def test_env_ttl_overrides_file_ttl(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=999001,
            mcp_create_bearer="test-mcp-bearer",
            default_ttl_seconds=1800,
            floor_ttl_seconds=900,
        )
        inbox = self._start(
            path,
            environ={"PARAPHE_DEFAULT_TTL_SECONDS": "3600"},
        )
        self.assertEqual(inbox.default_ttl_seconds, 3600)

    def test_file_ttl_below_floor_is_rejected(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=999001,
            mcp_create_bearer="test-mcp-bearer",
            default_ttl_seconds=60,
            floor_ttl_seconds=900,
        )
        with self.assertRaises(SetupError):
            self._start(path)

    def test_default_ttl_is_14400(self) -> None:
        self.assertEqual(DEFAULT_TTL_SECONDS, 14400)
        self.assertEqual(FLOOR_TTL_SECONDS, 900)
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=999001,
            mcp_create_bearer="test-mcp-bearer",
        )
        inbox = self._start(path)
        self.assertEqual(inbox.default_ttl_seconds, 14400)
        self.assertEqual(inbox.floor_ttl_seconds, 900)

    def test_store_path_is_ready_without_sqlite_rows(self) -> None:
        inbox = self._start(self._write_config(**self._credentials))
        ready = self.tmpdir / "inbox.sqlite"
        prepared = inbox.prepare_store(ready)
        self.assertTrue(Path(prepared).is_file())
        self.assertEqual(Path(prepared).stat().st_mode & 0o777, 0o600)

    def test_default_store_path_is_per_user(self) -> None:
        inbox = self._start(self._write_config(**self._credentials))
        resolved = Path(inbox.store_path)
        self.assertEqual(resolved, default_store_path())
        self.assertEqual(resolved.name, "inbox.sqlite")
        self.assertEqual(resolved.parent.name, "paraphe")
        self.assertFalse(resolved.is_relative_to("/var/lib"))

    def test_store_path_from_the_config_file_is_honoured(self) -> None:
        configured = self.tmpdir / "configured" / "inbox.sqlite"
        inbox = self._start(self._write_config(**self._credentials, store_path=configured))
        self.assertEqual(Path(inbox.store_path), configured)

    def test_store_path_from_the_environment_wins_over_the_config_file(self) -> None:
        configured = self.tmpdir / "configured" / "inbox.sqlite"
        from_env = self.tmpdir / "from-env" / "inbox.sqlite"
        inbox = self._start(
            self._write_config(**self._credentials, store_path=configured),
            environ={"PARAPHE_STORE_PATH": str(from_env)},
        )
        self.assertEqual(Path(inbox.store_path), from_env)

    def test_relocation_away_from_the_previous_location_refuses(self) -> None:
        legacy = self.tmpdir / "legacy" / "inbox.sqlite"
        legacy.parent.mkdir(parents=True)
        legacy.touch()
        _store_module.LEGACY_STORE_PATH = legacy
        with self.assertRaises(SetupError) as caught:
            self._start(self._write_config(**self._credentials))
        message = str(caught.exception)
        self.assertIn(str(default_store_path()), message)
        self.assertIn(str(legacy), message)
        self.assertFalse(default_store_path().exists())

    def test_an_explicit_location_is_honoured_while_a_store_exists_elsewhere(self) -> None:
        legacy = self.tmpdir / "legacy" / "inbox.sqlite"
        legacy.parent.mkdir(parents=True)
        legacy.touch()
        _store_module.LEGACY_STORE_PATH = legacy
        target = self.tmpdir / "explicit" / "inbox.sqlite"
        inbox = self._start(
            self._write_config(**self._credentials), environ={"PARAPHE_STORE_PATH": str(target)}
        )
        self.assertEqual(Path(inbox.store_path), target)

    def test_created_data_directory_is_owner_only(self) -> None:
        target = self.tmpdir / "data" / "inbox.sqlite"
        prepared = _store_module.Store.prepare(target)
        self.assertEqual(Path(prepared).stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_a_card_survives_a_restart_against_the_same_location(self) -> None:
        store_path = self.tmpdir / "persist" / "inbox.sqlite"
        env = {"PARAPHE_STORE_PATH": str(store_path)}
        first = self._start(self._write_config(**self._credentials), environ=env)
        created = first.call_tool(
            "ask_question", {"question": "Ready?", "external_id": "persist-1"}, bearer="test-mcp-bearer"
        )
        second = self._start(self._write_config(**self._credentials), environ=env)
        again = second.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(again["status"], "pending")

    def test_empty_env_does_not_discard_file_values(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=999001,
            mcp_create_bearer="test-mcp-bearer",
            default_ttl_seconds=1800,
            floor_ttl_seconds=900,
        )
        inbox = self._start(
            path,
            environ={
                "PARAPHE_DEFAULT_TTL_SECONDS": "",
                "PARAPHE_BOT_TOKEN": "  ",
            },
        )
        self.assertEqual(inbox.default_ttl_seconds, 1800)

    def test_env_ttl_below_floor_is_rejected(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=999001,
            mcp_create_bearer="test-mcp-bearer",
            default_ttl_seconds=1800,
        )
        with self.assertRaises(SetupError):
            self._start(path, environ={"PARAPHE_DEFAULT_TTL_SECONDS": "60"})

    def test_malformed_toml_is_setup_error(self) -> None:
        path = self.tmpdir / "bad.toml"
        path.write_text("owner_telegram_id = [\n", encoding="utf-8")
        with self.assertRaises(SetupError):
            self._start(path)

    def test_non_positive_owner_id_is_rejected(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=0,
            mcp_create_bearer="test-mcp-bearer",
        )
        with self.assertRaises(SetupError):
            self._start(path)

    def test_console_command_prints_usage_when_nothing_is_configured(self) -> None:
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out):
            code = _entry.main([])
        self.assertEqual(code, 0)
        self.assertIn("Paraphe", out.getvalue())

    def test_console_command_prints_installed_version_without_configuration(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(_entry, "version", return_value="1.2.3") as distribution_version,
            contextlib.redirect_stdout(out),
        ):
            code = _entry.main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "1.2.3\n")
        distribution_version.assert_called_once_with("paraphe")

    def test_console_command_fails_closed_on_incomplete_configuration(self) -> None:
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"PARAPHE_OWNER_TELEGRAM_ID": "999001"}, clear=True), contextlib.redirect_stderr(err):
            code = _entry.main([])
        self.assertEqual(code, 2)
        self.assertIn("paraphe:", err.getvalue())

    def test_bearer_is_available_and_not_logged(self) -> None:
        path = self._write_config(
            bot_token="test-bot-token",
            owner_telegram_id=999001,
            mcp_create_bearer="test-mcp-bearer",
        )
        inbox = self._start(path)
        inbox.store_path = self.tmpdir / "inbox.sqlite"
        created = inbox.call_tool(
            "ask_question",
            {"question": "Ready?", "external_id": "from-start"},
            bearer="test-mcp-bearer",
        )
        self.assertTrue(created["request_id"])
        self.assertNotIn("test-mcp-bearer", repr(inbox))
        self.assertNotIn("test-bot-token", repr(inbox))


CREATE = "test-mcp-bearer"
ANSWER = "test-answer-token"


class TestAnswerPath(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmpdir = Path(self._tmpdir.name)
        self._legacy = mock.patch.object(
            _store_module, "LEGACY_STORE_PATH", self.tmpdir / "absent.sqlite"
        )
        self._legacy.start()
        self.addCleanup(self._legacy.stop)
        self.inbox = Inbox(
            owner_telegram_id=None,
            default_ttl_seconds=14400,
            floor_ttl_seconds=900,
            store_path=self.tmpdir / "inbox.sqlite",
            mcp_create_bearer=CREATE,
            owner_answer_token=ANSWER,
        )
        self.addCleanup(self.inbox.close)
        self.handle = self.inbox.serve(host="127.0.0.1", port=0)
        self.addCleanup(self.handle.close)
        self.base = f"http://127.0.0.1:{self.handle.port}"

    def _post(self, token: str, body: dict) -> tuple[int, str]:
        request = urllib.request.Request(
            self.base + "/answer",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def _card(self, external_id: str = "answer-1") -> dict:
        return self.inbox.call_tool(
            "ask_question",
            {"question": "Ready?", "external_id": external_id, "choices": ["Yes", "No"]},
            bearer=CREATE,
        )

    def test_the_create_credential_cannot_answer(self) -> None:
        created = self._card()
        status, _ = self._post(
            CREATE,
            {"request_id": created["request_id"], "version": created["version"], "choice": "Yes"},
        )
        self.assertEqual(status, 401)
        envelope = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertIsNone(envelope["response"])
        self.assertEqual(envelope["status"], "pending")

    def test_a_missing_or_wrong_credential_is_refused(self) -> None:
        created = self._card()
        status, _ = self._post(
            "wrong",
            {"request_id": created["request_id"], "version": created["version"], "choice": "Yes"},
        )
        self.assertEqual(status, 401)

    def test_the_owner_credential_answers_and_records_how_it_arrived(self) -> None:
        created = self._card()
        status, body = self._post(
            ANSWER,
            {"request_id": created["request_id"], "version": created["version"], "choice": "Yes"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "answered")
        envelope = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertEqual(envelope["response"]["choice"], "Yes")
        self.assertEqual(envelope["response"]["responded_via"], "answer-path")

    def test_a_superseded_version_is_refused_and_the_card_is_unchanged(self) -> None:
        created = self._card()
        self.inbox.call_tool(
            "update_request",
            {
                "request_id": created["request_id"],
                "expected_version": created["version"],
                "choices": ["Yes", "No", "Later"],
            },
            bearer=CREATE,
        )
        status, body = self._post(
            ANSWER,
            {"request_id": created["request_id"], "version": created["version"], "choice": "Yes"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body.strip(), "stale_version")
        envelope = self.inbox.call_tool("get_response", {"request_id": created["request_id"]})
        self.assertIsNone(envelope["response"])

    def test_a_second_answer_is_refused(self) -> None:
        created = self._card()
        payload = {"request_id": created["request_id"], "version": created["version"], "choice": "Yes"}
        self.assertEqual(self._post(ANSWER, payload)[0], 200)
        status, body = self._post(ANSWER, payload)
        self.assertEqual(status, 409)
        self.assertEqual(body.strip(), "already_tapped")

    def test_the_console_destination_prints_the_card_and_the_answer_command(self) -> None:
        printed: list[str] = []
        destination = console_module.ConsoleDestination(port=8792, write=printed.append)
        created = self._card()
        destination.notify(
            {
                "request_id": created["request_id"],
                "version": created["version"],
                "kind": "question",
                "question": "Ready?",
                "choices": ["Yes", "No"],
                "risk": "medium",
                "priority": "normal",
            }
        )
        text = "\n".join(printed)
        self.assertIn(created["request_id"], text)
        self.assertIn("/answer", text)
        self.assertIn("PARAPHE_OWNER_ANSWER_TOKEN", text)


if __name__ == "__main__":
    unittest.main()
