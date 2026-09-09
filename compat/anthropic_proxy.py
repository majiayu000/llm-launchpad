#!/usr/bin/env python3
"""Loopback proxy that normalizes Claude Code requests for Ollama."""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlsplit

from meter import TurnMeter


LISTEN_HOST = os.environ.get("QWEN38_COMPAT_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("QWEN38_COMPAT_PORT", "11440"))
UPSTREAM_HOST = os.environ.get("QWEN38_OLLAMA_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("QWEN38_OLLAMA_PORT", "11439"))
# Shared secret for Anthropic-style clients (x-api-key / Authorization: Bearer).
# Default matches scripts/claude-code.sh so local tooling works out of the box.
COMPAT_TOKEN = os.environ.get("QWEN38_COMPAT_TOKEN", "ollama")
MAX_BODY_BYTES = 64 * 1024 * 1024
TOOL_RESULT_BLOCK_CHARS = int(os.environ.get("QWEN38_TOOL_RESULT_BLOCK_CHARS", "2000"))
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
CLIENT_AUTH_HEADERS = {"authorization", "x-api-key"}


def is_loopback_host(host: str) -> bool:
    """Return True when the listen address is loopback-only."""
    normalized = host.strip().lower()
    if normalized in {"localhost", "::1"}:
        return True
    # Strip IPv6 brackets used in some bind strings.
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def extract_request_credential(headers: Mapping[str, str]) -> str | None:
    """Pull the shared secret from Anthropic-style auth headers."""
    by_name = {name.lower(): value for name, value in headers.items()}
    api_key = by_name.get("x-api-key")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()

    authorization = by_name.get("authorization")
    if isinstance(authorization, str):
        scheme, _, remainder = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and remainder.strip():
            return remainder.strip()
    return None


def credential_matches(provided: str | None, expected: str) -> bool:
    """Constant-time compare when auth is configured; empty expected disables auth."""
    if not expected:
        return True
    if not isinstance(provided, str) or not provided:
        return False
    return secrets.compare_digest(provided, expected)


def assert_listen_host_allowed(host: str, token: str) -> None:
    """Refuse non-loopback binds unless a non-empty shared secret is configured."""
    if is_loopback_host(host):
        return
    if token:
        return
    raise SystemExit(
        f"Refusing to bind QWEN38_COMPAT_HOST={host!r} without QWEN38_COMPAT_TOKEN. "
        "Set a non-empty shared secret before exposing the compat proxy off loopback."
    )

TOOL_RESULT_OMISSION = (
    "\n\n[Local Ollama context guard: {omitted} characters omitted. "
    "Re-read only the needed range with Read offset/limit or a targeted Bash command.]\n\n"
)
SYSTEM_REMINDER_OPEN = "\n\n<system-reminder>\n"
SYSTEM_REMINDER_CLOSE = "\n</system-reminder>"


def _text_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("late system message content must be text or text blocks")

    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise ValueError("late system message must contain only text blocks")
        blocks.append(dict(block))
    return blocks


def normalize_anthropic_request(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Merge late system-role messages into their adjacent user messages.

    Ollama rejects system messages after the conversation starts. Moving every
    new reminder into the leading system field fixes validation but mutates the
    beginning of the rendered prompt on every turn, defeating prefix caching.
    Merging each reminder into its preceding user message keeps all completed
    turns byte-stable while retaining the reminder next to its original input.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")

    normalized_messages: list[dict[str, Any]] = []
    merged_count = 0
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")
        if str(message.get("role", "")).lower() != "system":
            normalized_messages.append(message)
            continue

        reminder = [
            {"type": "text", "text": SYSTEM_REMINDER_OPEN},
            *_text_blocks(message.get("content")),
            {"type": "text", "text": SYSTEM_REMINDER_CLOSE},
        ]
        merged_count += 1

        if normalized_messages and str(normalized_messages[-1].get("role", "")).lower() == "user":
            previous = normalized_messages[-1]
            previous_content = previous.get("content")
            if isinstance(previous_content, str):
                content_blocks = [{"type": "text", "text": previous_content}]
            elif isinstance(previous_content, list) and all(isinstance(block, dict) for block in previous_content):
                content_blocks = [dict(block) for block in previous_content]
            else:
                raise ValueError("user message content must be text or content blocks")
            merged_message = dict(previous)
            merged_message["content"] = [*content_blocks, *reminder]
            normalized_messages[-1] = merged_message
        else:
            normalized_messages.append({"role": "user", "content": reminder})

    if not merged_count:
        return payload, 0

    normalized = dict(payload)
    normalized["messages"] = normalized_messages
    return normalized, merged_count


def _shorten_text(text: str, allowance: int) -> tuple[str, int]:
    """Keep the useful edges of one tool result within its character allowance."""
    if len(text) <= allowance:
        return text, 0

    omitted = len(text) - max(allowance, 0)
    marker = TOOL_RESULT_OMISSION.format(omitted=omitted)
    if allowance <= len(marker) + 80:
        return marker, len(text)

    retained = allowance - len(marker)
    head_chars = retained * 2 // 3
    tail_chars = retained - head_chars
    shortened = f"{text[:head_chars]}{marker}{text[-tail_chars:]}"
    return shortened, len(text) - len(shortened)


def compact_anthropic_tool_results(payload: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    """Deterministically bound each historical text tool output.

    Claude Code resends every prior Read/Bash result on every turn. Qwen3.8's
    hybrid runner can reuse a checkpoint only when earlier prompt bytes remain
    stable. A per-result limit gives identical input the same representation on
    every turn. Tool-use/result IDs and non-text blocks are preserved; the model
    can fetch omitted source ranges again when needed.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")

    changed_messages = list(messages)
    shortened_blocks = 0
    removed_chars = 0

    for message_index, message in enumerate(messages):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue

        content = message["content"]
        changed_content = list(content)
        message_changed = False
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue

            result_content = block.get("content")
            if isinstance(result_content, str):
                shortened, removed = _shorten_text(result_content, TOOL_RESULT_BLOCK_CHARS)
                if removed:
                    changed_block = dict(block)
                    changed_block["content"] = shortened
                    changed_content[block_index] = changed_block
                    message_changed = True
                    shortened_blocks += 1
                    removed_chars += removed
                continue

            if not isinstance(result_content, list):
                continue

            changed_result = list(result_content)
            result_changed = False
            for result_index, result_block in enumerate(result_content):
                if not isinstance(result_block, dict) or result_block.get("type") != "text":
                    continue
                text = result_block.get("text")
                if not isinstance(text, str):
                    continue
                shortened, removed = _shorten_text(text, TOOL_RESULT_BLOCK_CHARS)
                if removed:
                    changed_text_block = dict(result_block)
                    changed_text_block["text"] = shortened
                    changed_result[result_index] = changed_text_block
                    result_changed = True
                    shortened_blocks += 1
                    removed_chars += removed
            if result_changed:
                changed_block = dict(block)
                changed_block["content"] = changed_result
                changed_content[block_index] = changed_block
                message_changed = True

        if message_changed:
            changed_message = dict(message)
            changed_message["content"] = changed_content
            changed_messages[message_index] = changed_message

    if not shortened_blocks:
        return payload, 0, 0

    compacted = dict(payload)
    compacted["messages"] = changed_messages
    return compacted, shortened_blocks, removed_chars


class CompatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Qwen38OllamaCompat/1.0"

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("client=%s %s", self.client_address[0], format % args)

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        return self.rfile.read(length) if length else b""

    def _send_anthropic_error(self, status: int, error_type: str, message: str) -> None:
        body = json.dumps(
            {"type": "error", "error": {"type": error_type, "message": message}},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _proxy(self) -> None:
        self.close_connection = True
        try:
            credential = extract_request_credential(self.headers)
            if not credential_matches(credential, COMPAT_TOKEN):
                self._send_anthropic_error(
                    401,
                    "authentication_error",
                    "Invalid or missing API key. Provide a matching x-api-key or Authorization: Bearer token.",
                )
                return

            body = self._read_body()
            rewritten = 0
            shortened = 0
            removed_chars = 0
            if self.command == "POST" and urlsplit(self.path).path == "/v1/messages":
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as error:
                    raise ValueError(f"invalid JSON: {error.msg}") from error
                if not isinstance(payload, dict):
                    raise ValueError("request body must be a JSON object")
                payload, rewritten = normalize_anthropic_request(payload)
                payload, shortened, removed_chars = compact_anthropic_tool_results(payload)
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP | CLIENT_AUTH_HEADERS | {"host", "content-length"}
            }
            if body:
                headers["Content-Length"] = str(len(body))

            upstream = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=3600)
            upstream.request(self.command, self.path, body=body or None, headers=headers)
            response = upstream.getresponse()
            meter = (
                TurnMeter()
                if self.command == "POST"
                and urlsplit(self.path).path == "/v1/messages"
                and response.status == 200
                else None
            )

            self.send_response(response.status, response.reason)
            has_content_length = False
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in HOP_BY_HOP:
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(name, value)

            chunked = not has_content_length and self.command != "HEAD" and response.status not in {204, 304}
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()

            if self.command != "HEAD":
                while data := response.read1(64 * 1024):
                    if meter is not None:
                        meter.feed(data)
                    if chunked:
                        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                    else:
                        self.wfile.write(data)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                if meter is not None:
                    meter.finish()

            logging.info(
                "method=%s path=%s status=%d merged_system_messages=%d "
                "shortened_tool_results=%d removed_tool_result_chars=%d request_bytes=%d",
                self.command,
                self.path,
                response.status,
                rewritten,
                shortened,
                removed_chars,
                len(body),
            )
            upstream.close()
        except ValueError as error:
            self._send_anthropic_error(400, "invalid_request_error", str(error))
        except (BrokenPipeError, ConnectionResetError):
            logging.info("client disconnected method=%s path=%s", self.command, self.path)
        except (OSError, http.client.HTTPException) as error:
            logging.exception("upstream request failed")
            self._send_anthropic_error(502, "api_error", f"Ollama upstream unavailable: {error}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        assert_listen_host_allowed(LISTEN_HOST, COMPAT_TOKEN)
    except SystemExit as error:
        logging.error("%s", error)
        raise SystemExit(1) from error

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), CompatHandler)
    auth_mode = "shared-secret" if COMPAT_TOKEN else "disabled"
    logging.info(
        "Claude compatibility API listening on http://%s:%d -> http://%s:%d (auth=%s)",
        LISTEN_HOST,
        LISTEN_PORT,
        UPSTREAM_HOST,
        UPSTREAM_PORT,
        auth_mode,
    )
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
