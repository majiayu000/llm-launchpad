#!/usr/bin/env python3
"""Live generation meter published by the Claude compatibility proxy.

The proxy relays the upstream /v1/messages SSE stream verbatim; TurnMeter
observes the same bytes on the way through and publishes a small JSON
snapshot so scripts/meter.sh can render a live tok/s readout. Ollama emits
one content_block_delta per generated token and only carries authoritative
usage totals on the final message_delta, so tokens are counted from deltas
and reconciled at finish. Set QWEN38_METER_FILE="" to disable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Callable

# User-private state dir (matches install.sh / scripts STATE_DIR), not /tmp.
DEFAULT_METER_FILE = os.path.join("~", ".local", "share", "qwen38-ollama", "meter.json")

logger = logging.getLogger(__name__)


def meter_path() -> str:
    """Resolve the snapshot path; empty value disables metering."""
    raw = os.environ.get("QWEN38_METER_FILE", DEFAULT_METER_FILE).strip()
    return os.path.expanduser(raw) if raw else ""


class TurnMeter:
    """Track one streamed /v1/messages response and publish live stats."""

    def __init__(self, path: str | None = None, clock: Callable[[], float] = time.time):
        self.path = meter_path() if path is None else path
        self.enabled = bool(self.path)
        self._clock = clock
        self._buffer = b""
        self._phase = "prefill"
        self._input_tokens = 0
        self._output_tokens = 0
        self._t_request = clock()
        self._t_first_token: float | None = None
        self._last_completed: dict[str, Any] | None = None
        self._finished = False
        self._published = False

    def feed(self, data: bytes) -> None:
        """Consume the next raw bytes relayed from the upstream response."""
        if not self.enabled or self._finished:
            return
        if not self._published:
            self._publish(self._clock(), self._phase)
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._handle_line(line.strip())

    def finish(self) -> None:
        """Freeze the turn, reconcile totals, and rotate in the summary."""
        if not self.enabled or self._finished:
            return
        self._finished = True
        now = self._clock()
        generation_s = max(now - (self._t_first_token or now), 0.0)
        if self._output_tokens > 0:
            self._last_completed = {
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "duration_s": round(now - self._t_request, 2),
                "generation_s": round(generation_s, 2),
                "tok_s": round(self._output_tokens / generation_s, 1) if generation_s > 0 else None,
                "ended_at": round(now, 3),
            }
        self._publish(now, "done")

    def _handle_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start":
            usage = event.get("message", {}).get("usage") or {}
            self._input_tokens = max(self._input_tokens, usage.get("input_tokens") or 0)
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            self._phase = "thinking" if block.get("type") == "thinking" else "text"
        elif kind == "content_block_delta":
            self._output_tokens += 1
            if self._t_first_token is None:
                self._t_first_token = self._clock()
        elif kind == "message_delta":
            usage = event.get("usage") or {}
            if isinstance(usage.get("output_tokens"), int):
                self._output_tokens = max(self._output_tokens, usage["output_tokens"])
            if isinstance(usage.get("input_tokens"), int):
                self._input_tokens = max(self._input_tokens, usage["input_tokens"])
        self._publish(self._clock(), self._phase)

    def _publish(self, now: float, phase: str) -> None:
        self._published = True
        snapshot: dict[str, Any] = {
            "turn_id": f"{self._t_request:.6f}",
            "phase": phase,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "t_request": round(self._t_request, 3),
            "t_first_token": round(self._t_first_token, 3) if self._t_first_token is not None else None,
            "t_update": round(now, 3),
        }
        if self._last_completed is not None:
            snapshot["last_completed"] = self._last_completed
        self._write(snapshot)

    def _write(self, snapshot: dict[str, Any]) -> None:
        directory = os.path.dirname(self.path) or "."
        tmp_path = ""
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".meter-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False)
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600)
        except OSError as error:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            logger.warning("meter snapshot write failed: %s", error)
