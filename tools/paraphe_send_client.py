"""The only transport of the drain tool: one shell-out to the Paraphe MCP CLI.

``paraphe-send`` resolves its own bearer and talks to the local MCP server; the
drain never sees a credential. Everything else in ``paraphe_drain.py`` is pure
logic, which is why the client is injected into every entry point and built only
by the CLI.

Usage: ``ParapheSendClient(send_bin).list_unprocessed()`` and friends. The
default binary is ``$PARAPHE_SEND_BIN`` or ``~/.local/bin/paraphe-send``.
"""

from __future__ import annotations

import json
import os
import subprocess

DEFAULT_SEND_BIN = os.environ.get("PARAPHE_SEND_BIN") or os.path.expanduser(
    "~/.local/bin/paraphe-send")


class ParapheSendClient:
    """A thin, stateless adapter over the MCP CLI; one call per invocation."""

    def __init__(self, send_bin: str | None = None, timeout: float = 120.0):
        self.send_bin = send_bin or DEFAULT_SEND_BIN
        self.timeout = timeout

    def _call(self, tool: str, arguments: dict) -> dict:
        proc = subprocess.run([self.send_bin, tool, json.dumps(arguments)],
                              capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"paraphe-send {tool} failed (rc={proc.returncode}): "
                               f"{(proc.stderr or '').strip()[:300]}")
        try:
            return json.loads(proc.stdout)
        except ValueError:
            raise RuntimeError(f"paraphe-send {tool} returned non-JSON") from None

    def list_unprocessed(self) -> dict:
        return self._call("list_unprocessed", {})

    def get_response(self, request_id: str) -> dict:
        return self._call("get_response", {"request_id": request_id})

    def mark_processed(self, request_id: str) -> dict:
        return self._call("mark_processed", {"request_id": request_id})
