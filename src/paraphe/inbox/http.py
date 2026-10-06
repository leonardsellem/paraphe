"""Authenticated Streamable HTTP + SSE for the Inbox MCP surface and the owner answer path."""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlparse

from .claim import ClaimRefused

ANSWER_PATH = "/answer"

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
MAX_BODY_BYTES = 65536


class BindError(Exception):
    """Listen address is not loopback or tailnet."""


class ServerHandle:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread
        self.host = server.server_address[0]
        self.port = int(server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


def bind_host_allowed(host: str) -> bool:
    raw = (host or "").strip().lower()
    if raw in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.version == 4 and ip in TAILNET_V4:
        return True
    if ip.version == 6 and ip in TAILNET_V6:
        return True
    return False


def is_loopback_host(host: str) -> bool:
    raw = (host or "").strip()
    if raw.lower() in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def bind_family(host: str) -> int:
    raw = (host or "").strip()
    if raw.lower() in {"localhost", "127.0.0.1"}:
        return socket.AF_INET
    if raw.lower() == "::1":
        return socket.AF_INET6
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return socket.AF_INET
    return socket.AF_INET6 if ip.version == 6 else socket.AF_INET


def serve_inbox(inbox: Any, host: str, port: int = 0) -> ServerHandle:
    if not bind_host_allowed(host):
        raise BindError("public bind refused")
    session = secrets.token_hex(16)
    handler = _make_handler(inbox, session)
    family = bind_family(host)

    class Server(ThreadingHTTPServer):
        address_family = family

    server = Server((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return ServerHandle(server, thread)


def _make_handler(inbox: Any, session_id: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _bearer(self) -> str | None:
            header = self.headers.get("Authorization") or ""
            if header.startswith("Bearer "):
                return header[7:]
            return None

        def do_GET(self) -> None:
            token = self._bearer()
            if not inbox.check_bearer(token):
                self._send(401, b"unauthorized", "text/plain")
                return
            if not self._is_mcp_path():
                self._send(404, b"not found", "text/plain")
                return
            self._send(405, b"method not allowed", "text/plain")

        def _read_body(self) -> bytes | None:
            raw_length = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._send(400, b"bad content-length", "text/plain", close=True)
                return None
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(413, b"payload too large", "text/plain", close=True)
                return None
            return self.rfile.read(length) if length else b"{}"

        def do_POST(self) -> None:
            if urlparse(self.path).path == ANSWER_PATH:
                self._answer_as_owner()
                return
            token = self._bearer()
            if not inbox.check_bearer(token):
                self._send(401, b"unauthorized", "text/plain", close=True)
                return
            if not self._is_mcp_path():
                self._send(404, b"not found", "text/plain", close=True)
                return
            raw = self._read_body()
            if raw is None:
                return
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._jsonrpc_error(None, -32700, "parse error")
                return
            response = _dispatch(inbox, payload, bearer=token)
            if response is None:
                self._send(202, b"", "application/json", extra={"Mcp-Session-Id": session_id})
                return
            accept = self.headers.get("Accept") or ""
            data = json.dumps(response).encode("utf-8")
            if "text/event-stream" in accept and "application/json" not in accept:
                framed = b"event: message\ndata: " + data + b"\n\n"
                self._send(200, framed, "text/event-stream", extra={"Mcp-Session-Id": session_id})
                return
            self._send(200, data, "application/json", extra={"Mcp-Session-Id": session_id})

        def _answer_as_owner(self) -> None:
            # The owner identity is the answer credential, never the create
            # bearer. No claim is recorded when it is missing or wrong.
            if not inbox.check_answer_token(self._bearer()):
                self._send(401, b"unauthorized", "text/plain", close=True)
                return
            raw = self._read_body()
            if raw is None:
                return
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, b"invalid request", "text/plain", close=True)
                return
            request_id = payload.get("request_id") if isinstance(payload, dict) else None
            version = payload.get("version") if isinstance(payload, dict) else None
            choice = payload.get("choice") if isinstance(payload, dict) else None
            if (
                not isinstance(request_id, str)
                or not request_id
                or isinstance(version, bool)
                or not isinstance(version, int)
            ):
                self._send(400, b"request_id and version are required", "text/plain", close=True)
                return
            if choice is not None and not isinstance(choice, str):
                self._send(400, b"invalid choice", "text/plain", close=True)
                return
            try:
                envelope = inbox.answer(request_id, version=version, choice=choice, responded_via="answer-path")
            except ClaimRefused as exc:
                self._send(409, str(exc.reason).encode("utf-8"), "text/plain", close=True)
                return
            except Exception:
                self._send(409, b"refused", "text/plain", close=True)
                return
            self._send(200, json.dumps(envelope).encode("utf-8"), "application/json", close=True)

        def _is_mcp_path(self) -> bool:
            path = urlparse(self.path).path
            return path in {"/mcp", "/", "/mcp/"}

        def _jsonrpc_error(self, rpc_id: object, code: int, message: str) -> None:
            data = json.dumps(
                {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
            ).encode("utf-8")
            self._send(200, data, "application/json")

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra: dict[str, str] | None = None,
            close: bool = False,
        ) -> None:
            if close:
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler


def _installed_version() -> str:
    """The installed distribution version, or ``0+unknown`` in a source checkout."""
    try:
        return version("paraphe")
    except PackageNotFoundError:
        return "0+unknown"


def _dispatch(inbox: Any, payload: dict[str, Any], *, bearer: str | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if method == "notifications/initialized" or (method and str(method).startswith("notifications/") and rpc_id is None):
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "paraphe", "version": _installed_version()},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
    if method == "tools/list":
        tools = inbox.list_tool_descriptors()
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            result = inbox.call_tool(name, arguments, bearer=bearer)
        except Exception as exc:
            text = str(exc) or "tool error"
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": True,
                },
            }
        if isinstance(result, str):
            text = result
        else:
            text = json.dumps(result)
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": "method not found"},
    }
