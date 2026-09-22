#!/usr/bin/env python3
"""Small, fail-closed Codex capacity retry companion for Windows.

The module deliberately uses only the Python standard library. It never
constructs a natural-language follow-up message. Live CLI recovery is allowed
only through the official local app-server daemon WebSocket, after a read-only
thread status check. A standalone stdio app-server is never used to take over
an existing CLI session.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


CAPACITY_MESSAGE = "selected model is at capacity. please try a different model."
TOOL_VERSION = "0.2.0"
DEFAULT_RETRY_DELAYS = (0.0, 3.0, 5.0, 10.0, 15.0, 30.0, 60.0)
PID_FILENAMES = {"watcher": "watcher.pid", "app_server": "app-server.pid"}
GUID_RE = re.compile(
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def codex_home_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(profile) / ".codex"


def cli_daemon_socket_path() -> Path:
    """Return the official shared CLI app-server rendezvous path."""
    return codex_home_path() / "app-server-control" / "app-server-control.sock"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_hash(value: Optional[str]) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def normalize_error_info(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def as_nonnegative_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and number != float("inf") else None


def retry_after(error: Any, envelope: dict[str, Any]) -> Optional[float]:
    values: list[Any] = []
    if isinstance(error, dict):
        values.extend(error.get(key) for key in ("retryAfterSeconds", "retry_after_seconds", "retryAfter"))
    values.extend(envelope.get(key) for key in ("retryAfterSeconds", "retry_after_seconds", "retryAfter"))
    for value in values:
        parsed = as_nonnegative_float(value)
        if parsed is not None:
            return parsed
    return None


def thread_id_from_path(path: Path) -> Optional[str]:
    # Rollout names contain the thread id after the timestamp.  Prefer the
    # first GUID after "rollout-"; a resumed rollout may contain a second id.
    matches = list(GUID_RE.finditer(path.name))
    if not matches:
        return None
    return matches[0].group("id")


@dataclass(frozen=True)
class ErrorEvent:
    timestamp: str
    thread_id: Optional[str]
    turn_id: Optional[str]
    error_kind: str
    will_retry: Optional[bool]
    source: str
    terminal: bool = True
    retry_after_seconds: Optional[float] = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.thread_id if isinstance(self.thread_id, str) else "",
                self.turn_id if isinstance(self.turn_id, str) else "")

    @property
    def is_capacity(self) -> bool:
        return self.error_kind in {"server_overloaded", "capacity_message"}


@dataclass(frozen=True)
class ActivityEvent:
    timestamp: str
    thread_id: Optional[str]
    turn_id: Optional[str]
    kind: str


def classify(error: Any) -> str:
    """Return a small enum; never persist the original error text."""
    if not isinstance(error, dict):
        return "unknown"
    structured = [error[k] for k in ("codex_error_info", "codexErrorInfo", "error_type", "errorType")
                  if k in error and error[k] is not None]
    if structured:
        # An explicit, unfamiliar type is authoritative. Its message must not
        # override usage/policy/context errors or a new protocol shape.
        return ("server_overloaded" if all(normalize_error_info(value) == "serveroverloaded"
                                           for value in structured) else "unknown")

    message = error.get("message")
    if isinstance(message, str) and message.casefold().strip() == CAPACITY_MESSAGE:
        return "capacity_message"
    return "unknown"


def _params_from_record(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    params = record.get("params")
    if isinstance(params, dict):
        return params
    payload = record.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("params"), dict):
        return payload["params"]
    return None


def parse_record(line: str, path: Optional[Path] = None) -> ErrorEvent | ActivityEvent | None:
    """Parse only known protocol/event envelopes.

    Arbitrary transcript lines are ignored, even if their text contains the
    words "capacity" or "error".
    """
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None

    timestamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else utc_now()
    thread_from_file = thread_id_from_path(path) if path else None

    method = record.get("method")
    params = _params_from_record(record)
    if method == "turn/started" and params is not None:
        turn = params.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            return None
        return ActivityEvent(timestamp, params.get("threadId"), turn["id"], "turn_started")
    if isinstance(method, str) and method == "error" and params is not None:
        error = params.get("error")
        if not isinstance(error, dict):
            error = params
        return ErrorEvent(
            timestamp=timestamp,
            thread_id=params.get("threadId") or params.get("thread_id") or thread_from_file,
            turn_id=params.get("turnId") or params.get("turn_id"),
            error_kind=classify(error),
            will_retry=as_bool(params.get("willRetry", params.get("will_retry"))),
            source="app-server",
            retry_after_seconds=retry_after(error, params),
        )

    event_type = record.get("type")
    payload = record.get("payload")
    if event_type != "event_msg" or not isinstance(payload, dict):
        return None

    payload_type = payload.get("type")
    payload_thread = payload.get("thread_id") or payload.get("threadId") or thread_from_file
    payload_turn = payload.get("turn_id") or payload.get("turnId")
    if payload_type in {"task_started", "turn_started", "turn/started"}:
        return ActivityEvent(timestamp, payload_thread, payload_turn, "turn_started")
    if payload_type == "user_message":
        return ActivityEvent(timestamp, payload_thread, payload_turn, "user_message")

    if payload_type not in {"task_complete", "turn_complete", "turn/completed", "error"}:
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    error_kind = classify(error)
    explicit_will_retry = as_bool(payload.get("willRetry", payload.get("will_retry")))
    # The CLI rollout writer does not persist ErrorNotification.willRetry on
    # terminal task_complete records. A structured capacity error in that
    # terminal record is therefore an unambiguous terminal observation for the
    # CLI path; other missing values remain fail-closed.
    if (
        explicit_will_retry is None
        and payload_type == "task_complete"
        and error_kind in {"server_overloaded", "capacity_message"}
    ):
        explicit_will_retry = False
    return ErrorEvent(
        timestamp=timestamp,
        thread_id=payload_thread,
        turn_id=payload_turn,
        error_kind=error_kind,
        will_retry=explicit_will_retry,
        source="rollout",
        retry_after_seconds=retry_after(error, payload),
    )


@dataclass
class Episode:
    event: ErrorEvent
    generation: int
    attempt: int = 0
    next_due: float = 0.0
    cancelled: bool = False
    action_sent: bool = False
    state: str = "pending"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    event: Optional[ErrorEvent] = None


class RecoveryEngine:
    """Thread-safe state machine with deterministic polling for tests."""

    def __init__(
        self,
        *,
        retry_delays: Optional[list[float] | tuple[float, ...]] = None,
        initial_delay: Optional[float] = None,
        max_delay: float = 60.0,
        max_attempts: int = 0,
        jitter_ratio: float = 0.2,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.max_delay = max(0.0, max_delay)
        if retry_delays is None:
            # Keep callers that used the old initial_delay argument working,
            # while the normal/default path uses the explicit user-facing
            # schedule: immediate, 3, 5, 10, 15, 30, then 60 seconds.
            if initial_delay is None:
                retry_delays = list(DEFAULT_RETRY_DELAYS)
            else:
                legacy_initial = max(0.0, float(initial_delay))
                retry_delays = [min(self.max_delay, legacy_initial * (2**index))
                                for index in range(len(DEFAULT_RETRY_DELAYS))]
        normalized = [max(0.0, float(value)) for value in retry_delays]
        if not normalized:
            normalized = list(DEFAULT_RETRY_DELAYS)
        self.retry_delays = tuple(min(self.max_delay, value) for value in normalized)
        self.max_attempts = max(0, max_attempts)  # 0 is explicit unlimited mode.
        self.jitter_ratio = max(0.0, min(1.0, jitter_ratio))
        self.random_fn = random_fn
        self._episodes: dict[tuple[str, str], Episode] = {}
        self._active_turn: dict[str, str] = {}
        self._generation: dict[str, int] = {}
        self._continuations: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()

    def handle_error(self, event: ErrorEvent, now: Optional[float] = None) -> Decision:
        with self._lock:
            if not event.terminal:
                return Decision("ignore", "non_terminal_error", event)
            if not event.is_capacity:
                return Decision("ignore", "non_capacity_error", event)
            if event.will_retry is True:
                previous = self._episodes.get(event.key)
                if previous is not None:
                    previous.cancelled = True
                    previous.state = "cancelled"
                return Decision("ignore", "codex_will_retry", event)
            if event.will_retry is not False:
                return Decision("ignore", "missing_will_retry", event)
            if not isinstance(event.thread_id, str) or not event.thread_id or not isinstance(event.turn_id, str) or not event.turn_id:
                return Decision("ignore", "missing_thread_or_turn", event)
            active = self._active_turn.get(event.thread_id)
            if active and active != event.turn_id:
                return Decision("ignore", "thread_already_has_new_turn", event)
            if event.key in self._episodes:
                return Decision("duplicate", "episode_already_observed", event)
            # Retain tombstones for this process lifetime. If too many accumulate,
            # stop admitting new episodes rather than evicting dedupe protection.
            if len(self._episodes) >= 10000:
                return Decision("ignore", "episode_capacity_reached", event)
            current = time.monotonic() if now is None else now
            attempt = self._continuations.pop(event.key, 0)
            generation = self._generation.get(event.thread_id, 0) + 1
            self._generation[event.thread_id] = generation
            delay = self._delay(attempt)
            if isinstance(event.retry_after_seconds, (int, float)) and event.retry_after_seconds > delay:
                delay = min(self.max_delay, event.retry_after_seconds)
            episode = Episode(event=event, generation=generation, attempt=attempt, next_due=current + delay)
            self._episodes[event.key] = episode
            self._active_turn[event.thread_id] = event.turn_id
            if self.max_attempts and attempt >= self.max_attempts:
                episode.cancelled = True
                episode.state = "exhausted"
                return Decision("stop", "retry_budget_exhausted", event)
            return Decision("pending", "capacity_terminal_error", event)

    def handle_activity(self, event: ActivityEvent) -> Decision:
        with self._lock:
            if not isinstance(event.thread_id, str) or not event.thread_id:
                return Decision("ignore", "missing_thread", None)
            if event.kind not in {"turn_started", "user_message"}:
                return Decision("ignore", "unknown_activity", None)
            if event.kind == "turn_started" and isinstance(event.turn_id, str) and event.turn_id:
                self._active_turn[event.thread_id] = event.turn_id
            cancelled = 0
            for episode in self._episodes.values():
                if episode.event.thread_id == event.thread_id and not episode.cancelled:
                    episode.cancelled = True
                    episode.generation += 1
                    episode.state = "cancelled"
                    cancelled += 1
            return Decision("cancel", f"{event.kind}:{cancelled}", None)

    def cancel_thread(self, thread_id: str, reason: str = "manual_or_user_activity") -> Decision:
        with self._lock:
            count = 0
            for episode in self._episodes.values():
                if episode.event.thread_id == thread_id and not episode.cancelled:
                    episode.cancelled = True
                    episode.generation += 1
                    episode.state = "cancelled"
                    count += 1
            # Preserve the last observed turn so a late error cannot reopen it.
            return Decision("cancel", f"{reason}:{count}", None)

    def cancel_episode(self, episode: Episode, reason: str = "cancelled") -> Decision:
        with self._lock:
            if not episode.cancelled:
                episode.cancelled = True
                episode.action_sent = False
                episode.state = "cancelled"
                episode.generation += 1
            return Decision("cancel", reason, episode.event)

    def _delay(self, attempt: int) -> float:
        index = min(max(0, attempt), len(self.retry_delays) - 1)
        base = min(self.max_delay, self.retry_delays[index])
        if not self.jitter_ratio or base == 0:
            return base
        jitter = base * self.jitter_ratio
        return max(0.0, min(self.max_delay, base + ((self.random_fn() * 2.0) - 1.0) * jitter))

    def poll_due(self, now: Optional[float] = None) -> list[Episode]:
        current = time.monotonic() if now is None else now
        with self._lock:
            due: list[Episode] = []
            for episode in self._episodes.values():
                if episode.cancelled or episode.action_sent:
                    continue
                if self.max_attempts and episode.attempt >= self.max_attempts:
                    episode.cancelled = True
                    episode.state = "exhausted"
                    continue
                if episode.next_due <= current:
                    episode.attempt += 1
                    episode.action_sent = True
                    episode.state = "claimed"
                    due.append(episode)
            return due

    def native_result(self, episode: Episode, success: bool, now: Optional[float] = None,
                      next_turn_id: Optional[str] = None) -> Decision:
        with self._lock:
            if success and next_turn_id and episode.event.thread_id:
                # Only an acknowledged new turn may carry the attempt budget.
                # A manual Retry/new message has no such association.
                self._continuations[(episode.event.thread_id, next_turn_id)] = episode.attempt
            if episode.cancelled:
                return Decision("ignore", "episode_cancelled", episode.event)
            if success:
                # A later turn/started event will retire the episode.  Keep the
                # key dedupe guard until then so duplicate error lines cannot
                # create a second turn.
                episode.state = "sent"
                return Decision("sent", "native_retry_accepted", episode.event)
            # False means a known rejection. A transport timeout must call
            # native_uncertain instead; retransmitting an ambiguous request is
            # never allowed.
            if self.max_attempts and episode.attempt >= self.max_attempts:
                episode.cancelled = True
                episode.state = "exhausted"
                return Decision("stop", "retry_budget_exhausted", episode.event)
            current = time.monotonic() if now is None else now
            episode.action_sent = False
            episode.next_due = current + self._delay(episode.attempt)
            episode.state = "backoff"
            return Decision("pending", "native_retry_rejected_backoff", episode.event)

    def native_uncertain(self, episode: Episode) -> Decision:
        """Stop after a transport timeout whose acceptance is unknown."""
        with self._lock:
            episode.cancelled = True
            episode.action_sent = False
            episode.state = "uncertain"
            return Decision("stop", "native_result_uncertain", episode.event)

    def mark_dry_run(self, episode: Episode, now: Optional[float] = None) -> None:
        with self._lock:
            if not episode.cancelled:
                if self.max_attempts and episode.attempt >= self.max_attempts:
                    episode.cancelled = True
                    episode.state = "exhausted"
                    return
                episode.action_sent = False
                current = time.monotonic() if now is None else now
                episode.next_due = current + self._delay(episode.attempt)
                episode.state = "would_retry"

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "thread": safe_hash(item.event.thread_id),
                    "turn": safe_hash(item.event.turn_id),
                    "attempt": item.attempt,
                    "generation": item.generation,
                    "next_due": item.next_due,
                    "cancelled": item.cancelled,
                    "action_sent": item.action_sent,
                    "state": item.state,
                }
                for item in self._episodes.values()
            ]


class JsonRpcProcess:
    """Small JSON-RPC stdio client used only for handshake/explicit probes."""

    def __init__(self, executable: Path, extra_args: Optional[list[str]] = None) -> None:
        self.executable = executable
        self.extra_args = extra_args or []
        self.proc: Optional[subprocess.Popen[str]] = None
        self._next_id = 1

    def __enter__(self) -> "JsonRpcProcess":
        self.proc = subprocess.Popen(
            [str(self.executable), "app-server", "--listen", "stdio://", *self.extra_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _write(self, value: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("JSON-RPC process is not running")
        self.proc.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[dict[str, Any]], timeout: float = 5.0) -> dict[str, Any]:
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("JSON-RPC process is not running")
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            line_queue: queue.Queue[str] = queue.Queue(maxsize=1)

            def read_line() -> None:
                try:
                    line_queue.put(self.proc.stdout.readline())
                except Exception:
                    line_queue.put("")

            reader = threading.Thread(target=read_line, daemon=True)
            reader.start()
            try:
                line = line_queue.get(timeout=remaining)
            except queue.Empty:
                continue
            if not line:
                break
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if result.get("id") == request_id:
                return result
        raise TimeoutError(f"timed out waiting for {method}")

    def notify(self, method: str, params: Optional[dict[str, Any]]) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)


class CliDaemonWebSocket:
    """Minimal WebSocket client over Codex's official stdio proxy.

    Windows Python does not expose AF_UNIX on every supported build. Codex's
    own ``app-server proxy`` command performs the protected local socket
    connection, so this class only implements the WebSocket bytes on its
    stdin/stdout. It never falls back to a standalone app-server process.
    """

    _WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, executable: Path, socket_path: Optional[Path] = None) -> None:
        self.executable = executable
        self.socket_path = socket_path or cli_daemon_socket_path()
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self._next_id = 1
        self._frames: queue.Queue[tuple[int, bytes] | None] = queue.Queue()
        self._reader: Optional[threading.Thread] = None

    def __enter__(self) -> "CliDaemonWebSocket":
        if not self.socket_path.exists():
            raise FileNotFoundError("shared CLI app-server daemon socket is absent")
        args = [str(self.executable), "app-server", "proxy", "--sock", str(self.socket_path)]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self._handshake()
        except Exception:
            self._terminate()
            raise
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        try:
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self._send_frame(0x8, b"")
                except (BrokenPipeError, OSError):
                    pass
        finally:
            self._terminate()

    def _terminate(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    @staticmethod
    def _read_handshake(stream: Any) -> bytes:
        data = bytearray()
        while len(data) < 64 * 1024:
            byte = stream.read(1)
            if not byte:
                raise ConnectionError("app-server proxy closed during WebSocket handshake")
            data.extend(byte)
            if data.endswith(b"\r\n\r\n"):
                return bytes(data)
        raise ConnectionError("app-server WebSocket handshake headers are too large")

    def _handshake(self) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("app-server proxy did not start")
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET /rpc HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.proc.stdin.write(request)
        self.proc.stdin.flush()

        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                result.put((True, self._read_handshake(self.proc.stdout)))
            except Exception as exc:  # pragma: no cover - depends on process/socket
                result.put((False, exc))

        threading.Thread(target=read, daemon=True).start()
        try:
            ok, value = result.get(timeout=5.0)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for app-server WebSocket handshake") from exc
        if not ok:
            raise value
        header_blob = value
        header_lines = header_blob.decode("latin1").split("\r\n")
        if not header_lines or " 101 " not in f" {header_lines[0]} ":
            raise ConnectionError("app-server daemon did not accept WebSocket upgrade")
        headers: dict[str, str] = {}
        for line in header_lines[1:]:
            if ":" in line:
                name, header_value = line.split(":", 1)
                headers[name.strip().casefold()] = header_value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + self._WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("app-server daemon returned an invalid WebSocket handshake")

    @staticmethod
    def _read_exact(stream: Any, size: int) -> Optional[bytes]:
        result = bytearray()
        while len(result) < size:
            chunk = stream.read(size - len(result))
            if not chunk:
                return None
            result.extend(chunk)
        return bytes(result)

    def _reader_loop(self) -> None:
        if self.proc is None or self.proc.stdout is None:
            self._frames.put(None)
            return
        stream = self.proc.stdout
        try:
            while True:
                header = self._read_exact(stream, 2)
                if header is None:
                    break
                first, second = header
                opcode = first & 0x0F
                length = second & 0x7F
                if length == 126:
                    raw = self._read_exact(stream, 2)
                    if raw is None:
                        break
                    length = int.from_bytes(raw, "big")
                elif length == 127:
                    raw = self._read_exact(stream, 8)
                    if raw is None:
                        break
                    length = int.from_bytes(raw, "big")
                if length > 128 * 1024 * 1024:
                    break
                mask = self._read_exact(stream, 4) if (second & 0x80) else None
                payload = self._read_exact(stream, length)
                if payload is None:
                    break
                if mask is not None:
                    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                self._frames.put((opcode, payload))
                if opcode == 0x8:
                    break
        finally:
            self._frames.put(None)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("app-server proxy is not running")
        if len(payload) >= (1 << 63):
            raise ValueError("WebSocket payload is too large")
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        first = 0x80 | (opcode & 0x0F)
        if len(payload) < 126:
            header = bytes((first, 0x80 | len(payload)))
        elif len(payload) < (1 << 16):
            header = bytes((first, 0x80 | 126)) + len(payload).to_bytes(2, "big")
        else:
            header = bytes((first, 0x80 | 127)) + len(payload).to_bytes(8, "big")
        self.proc.stdin.write(header + mask + masked)
        self.proc.stdin.flush()

    def _next_json(self, deadline: float) -> dict[str, Any]:
        fragments: Optional[bytearray] = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for app-server response")
            try:
                frame = self._frames.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("timed out waiting for app-server response") from exc
            if frame is None:
                raise ConnectionError("app-server daemon connection closed")
            opcode, payload = frame
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode in {0x8, 0xA}:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                # The current app-server emits one final text frame per JSON
                # message. If a peer fragments it, continuation frames below
                # extend this buffer.
                try:
                    value = json.loads(bytes(fragments).decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                return value if isinstance(value, dict) else {}
            if opcode == 0x0 and fragments is not None:
                fragments.extend(payload)
                try:
                    value = json.loads(bytes(fragments).decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                return value if isinstance(value, dict) else {}

    def request(self, method: str, params: Optional[dict[str, Any]], timeout: float = 5.0) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send_frame(0x1, json.dumps(message, separators=(",", ":")).encode("utf-8"))
        deadline = time.monotonic() + timeout
        while True:
            response = self._next_json(deadline)
            if response.get("id") == request_id:
                return response

    def notify(self, method: str, params: Optional[dict[str, Any]]) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send_frame(0x1, json.dumps(message, separators=(",", ":")).encode("utf-8"))


def discover_runtime(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser()
        return path if path.is_file() else None
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    root = Path(local) / "OpenAI" / "Codex" / "bin"
    candidates = [p / "codex.exe" for p in root.glob("*") if (p / "codex.exe").is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)


def discover_desktop_package() -> dict[str, Optional[str]]:
    """Read the installed MSIX identity without touching package contents.

    The Desktop shell and the bundled CLI have independent version markers.
    Keeping this probe separate makes the diagnostic output honest and avoids
    treating a CLI version as proof that the Desktop protocol is compatible.
    """
    empty = {"name": None, "version": None, "package_family": None}
    if os.name != "nt":
        return empty
    command = (
        "$p=Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue; "
        "if ($p) { [pscustomobject]@{Name=$p.Name;Version=$p.Version.ToString();"
        "PackageFamilyName=$p.PackageFamilyName} | ConvertTo-Json -Compress }"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return empty
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            return empty
        return {
            "name": value.get("Name") if isinstance(value.get("Name"), str) else None,
            "version": value.get("Version") if isinstance(value.get("Version"), str) else None,
            "package_family": (
                value.get("PackageFamilyName")
                if isinstance(value.get("PackageFamilyName"), str)
                else None
            ),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        return empty


def _initialize_cli_daemon(rpc: CliDaemonWebSocket) -> Optional[dict[str, Any]]:
    initialized = rpc.request(
        "initialize",
        {
            "clientInfo": {"name": "codex-native-retry", "version": TOOL_VERSION},
            "capabilities": {"experimentalApi": True},
        },
    )
    if not isinstance(initialized.get("result"), dict):
        return None
    rpc.notify("initialized", {})
    return initialized


def probe_cli_daemon(runtime: Optional[Path]) -> dict[str, Any]:
    """Read-only capability probe for the shared CLI daemon."""
    result: dict[str, Any] = {
        "available": False,
        "protocol": "unverified",
        "socket": str(cli_daemon_socket_path()),
        "reason": None,
    }
    if os.name != "nt":
        result["reason"] = "windows_only"
        return result
    if runtime is None:
        result["reason"] = "Codex runtime not found"
        return result
    if not cli_daemon_socket_path().exists():
        result["reason"] = "shared CLI app-server daemon is not running"
        return result
    try:
        with CliDaemonWebSocket(runtime) as rpc:
            if _initialize_cli_daemon(rpc) is None:
                result["reason"] = "daemon initialize did not return a result"
                return result
            listed = rpc.request("thread/list", {"limit": 1})
            if not isinstance(listed.get("result"), dict):
                result["reason"] = "daemon thread/list is unavailable"
                return result
            result["available"] = True
            result["protocol"] = "local-websocket-jsonrpc"
            result["reason"] = "initialize and read-only thread/list verified"
            return result
    except (OSError, RuntimeError, TimeoutError, ValueError, ConnectionError, subprocess.SubprocessError) as exc:
        result["reason"] = f"daemon probe failed: {type(exc).__name__}"
        return result


def _discover_cli_executable() -> Optional[Path]:
    found = shutil.which("codex") or shutil.which("codex.exe")
    return Path(found) if found else None


def _background_creationflags() -> int:
    return (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
            getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _wait_for_cli_daemon(runtime: Path, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.5, timeout)
    latest: dict[str, Any] = {"available": False, "reason": "daemon_start_timeout"}
    while time.monotonic() < deadline:
        latest = probe_cli_daemon(runtime)
        if latest.get("available"):
            return latest
        time.sleep(0.5)
    return latest


def _start_managed_daemon(runtime: Path) -> bool:
    candidates: list[Path] = []
    command = _discover_cli_executable()
    if command is not None:
        candidates.append(command)
    if runtime not in candidates:
        candidates.append(runtime)
    for executable in candidates:
        try:
            completed = subprocess.run(
                [str(executable), "app-server", "daemon", "start"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return True
    return False


def _start_direct_daemon(runtime: Path) -> Optional[int]:
    existing_pid = _read_pid("app_server")
    if _process_alive(existing_pid):
        return existing_pid
    _clear_pid("app_server")
    socket_path = cli_daemon_socket_path()
    if socket_path.exists():
        # A socket that did not answer the read-only probe may belong to an
        # unknown process. Do not delete it or start a second server over it.
        return None
    directory = _appdata_dir()
    if directory is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        codex_home_path().mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [str(runtime), "app-server", "--listen", "unix://"],
            cwd=str(codex_home_path()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_background_creationflags(),
        )
        _write_pid("app_server", process.pid)
        return process.pid
    except OSError:
        return None


def ensure_cli_daemon(runtime: Optional[Path]) -> dict[str, Any]:
    """Ensure one shared local daemon exists without touching user sessions."""
    if runtime is None:
        return {"available": False, "reason": "Codex runtime not found"}
    existing = probe_cli_daemon(runtime)
    if existing.get("available"):
        existing["started"] = False
        existing["route"] = "existing_shared_daemon"
        return existing

    managed_started = _start_managed_daemon(runtime)
    after_managed = _wait_for_cli_daemon(runtime, timeout=8.0) if managed_started else existing
    if after_managed.get("available"):
        after_managed["started"] = managed_started
        after_managed["route"] = "managed_daemon"
        return after_managed

    direct_pid = _start_direct_daemon(runtime)
    if direct_pid is None:
        return {
            "available": False,
            "started": False,
            "route": "none",
            "reason": "shared daemon unavailable and direct socket is occupied or cannot start",
        }
    result = _wait_for_cli_daemon(runtime)
    result["started"] = bool(result.get("available"))
    result["route"] = "bundled_app_server"
    result["pid"] = direct_pid
    return result


def cli_native_retry(runtime: Path, thread_id: str, failed_turn_id: str) -> dict[str, Any]:
    """Send one minimal empty-input continuation through the shared daemon.

    The function returns metadata only. A transport failure after ``turn/start``
    was written is reported as ``uncertain`` so callers never retransmit an
    ambiguous request.
    """
    result: dict[str, Any] = {"status": "cancel", "reason": "not_attempted", "turn_id": None}
    try:
        with CliDaemonWebSocket(runtime) as rpc:
            if _initialize_cli_daemon(rpc) is None:
                result["reason"] = "daemon_initialize_rejected"
                return result
            read = rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            thread = read.get("result", {}).get("thread") if isinstance(read.get("result"), dict) else None
            if not isinstance(thread, dict):
                result["reason"] = "thread_read_unavailable"
                return result
            status = thread.get("status")
            status_type = status.get("type") if isinstance(status, dict) else None
            # A terminal server error is represented by systemError rather than
            # idle. It is safe to continue only after the latest persisted turn
            # proves that this exact turn failed with serverOverloaded.
            if status_type not in {"idle", "systemError"}:
                result["reason"] = "thread_not_idle"
                return result
            if thread.get("canAcceptDirectInput") is False:
                result["reason"] = "direct_input_not_allowed"
                return result

            turns_response = rpc.request(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                },
            )
            turns_result = turns_response.get("result")
            turns = turns_result.get("data") if isinstance(turns_result, dict) else None
            latest = turns[0] if isinstance(turns, list) and turns else None
            if not isinstance(latest, dict):
                result["reason"] = "latest_turn_unavailable"
                return result
            if latest.get("id") != failed_turn_id:
                result["reason"] = "latest_turn_mismatch"
                return result
            if latest.get("status") != "failed":
                result["reason"] = "latest_turn_not_failed"
                return result
            latest_error = latest.get("error")
            if classify(latest_error) not in {"server_overloaded", "capacity_message"}:
                result["reason"] = "latest_turn_not_capacity"
                return result

            try:
                response = rpc.request("turn/start", cli_native_request(thread_id))
            except (OSError, RuntimeError, TimeoutError, ConnectionError) as exc:
                result["status"] = "uncertain"
                result["reason"] = f"turn_start_transport_uncertain:{type(exc).__name__}"
                return result
            if isinstance(response.get("result"), dict):
                turn = response["result"].get("turn")
                if isinstance(turn, dict) and isinstance(turn.get("id"), str) and turn["id"]:
                    result.update(status="accepted", reason="turn_start_accepted", turn_id=turn["id"])
                    return result
                result["status"] = "uncertain"
                result["reason"] = "turn_start_response_shape_unknown"
                return result
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            lowered = message.casefold() if isinstance(message, str) else ""
            if any(token in lowered for token in ("active turn", "not loaded", "thread is closing", "cannot accept")):
                result["reason"] = "turn_start_rejected_by_thread_state"
            else:
                result["status"] = "rejected"
                result["reason"] = "turn_start_rejected"
            return result
    except (OSError, RuntimeError, TimeoutError, ValueError, ConnectionError, subprocess.SubprocessError) as exc:
        result["reason"] = f"daemon_unavailable:{type(exc).__name__}"
        return result


def probe_runtime(runtime: Optional[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "codex_detected": runtime is not None,
        "runtime": str(runtime) if runtime else None,
        "version": None,
        "desktop_package": discover_desktop_package(),
        "app_server_route": "unknown",
        "ipc_available": False,
        "named_pipe_observed": False,
        "cli_daemon_socket": str(cli_daemon_socket_path()),
        "cli_daemon_available": False,
        "cli_daemon_protocol": "unverified",
        "native_retry_enabled": False,
        "reason": None,
    }
    if runtime is None:
        result["reason"] = "Codex runtime not found"
        return result
    # The Desktop pipe is an internal follower router, not the CLI app-server.
    # Existence alone must never enable turn writes.
    result["named_pipe_observed"] = Path(r"\\.\pipe\codex-ipc").exists()
    try:
        completed = subprocess.run(
            [str(runtime), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result["version"] = (completed.stdout or completed.stderr).strip().splitlines()[0:1]
        result["version"] = result["version"][0] if result["version"] else None
    except (OSError, subprocess.SubprocessError) as exc:
        result["reason"] = f"version probe failed: {type(exc).__name__}"
        return result

    try:
        with JsonRpcProcess(runtime) as rpc:
            initialized = rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "codex-native-retry", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            rpc.notify("initialized", {})
            if "result" not in initialized:
                result["reason"] = "initialize did not return a result"
                return result
            # thread/list is a read-only capability probe.  It does not read
            # transcript items and its response is intentionally discarded.
            listed = rpc.request("thread/list", {"limit": 1})
            if "result" not in listed:
                result["reason"] = "thread/list unavailable"
                return result
            result["app_server_route"] = "bundled stdio app-server (probe only)"
    except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
        result["app_server_route"] = "bundled stdio app-server"
        result["reason"] = f"app-server probe failed: {type(exc).__name__}"

    daemon = probe_cli_daemon(runtime)
    result["cli_daemon_available"] = bool(daemon.get("available"))
    result["cli_daemon_protocol"] = daemon.get("protocol", "unverified")
    if result["cli_daemon_available"]:
        result["native_retry_enabled"] = True
        result["reason"] = "shared CLI app-server daemon protocol verified"
    elif result["reason"] is None:
        result["reason"] = daemon.get("reason", "shared CLI app-server daemon is not available")
    return result


def native_request(
    thread_id: str,
    *,
    turn_trigger: str = "capacity_retry_automatic",
    collaboration_mode: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return the observed Desktop empty-turn continuation shape.

    The function is pure and is used by tests/equivalence review.  The
    installed Desktop currently supplies one of the two capacity trigger
    values below and sends an empty input.  Other state overrides remain
    omitted unless a caller has observed the thread's collaboration mode.
    """
    if not thread_id:
        raise ValueError("thread_id is required")
    if turn_trigger not in {"capacity_retry_automatic", "capacity_retry_manual"}:
        raise ValueError("unsupported capacity retry trigger")
    payload: dict[str, Any] = {
        "threadId": thread_id,
        "turnTrigger": turn_trigger,
        "input": [],
    }
    if collaboration_mode is not None:
        payload["collaborationMode"] = collaboration_mode
    return payload


def cli_native_request(thread_id: str) -> dict[str, Any]:
    """Build the minimal CLI continuation request.

    The CLI has no Retry button or documented retry trigger. Omitting every
    optional override makes the daemon reuse the saved thread settings. The
    empty input is handled by app-server as continuation input and is not
    serialized as a synthetic user message.
    """
    if not thread_id:
        raise ValueError("thread_id is required")
    return {"threadId": thread_id, "input": []}


class RolloutWatcher:
    def __init__(self, sessions_dir: Path, engine: RecoveryEngine, logger: Callable[[dict[str, Any]], None]) -> None:
        self.sessions_dir = sessions_dir
        self.engine = engine
        self.logger = logger
        self.offsets: dict[Path, int] = {}

    def prime(self, recover_existing_capacity: bool = False) -> None:
        paths = list(self.sessions_dir.rglob("rollout-*.jsonl")) if self.sessions_dir.exists() else []
        if recover_existing_capacity:
            self._recover_existing_capacity(paths)
        for path in paths:
            try:
                self.offsets[path] = path.stat().st_size
            except OSError:
                continue

    def _recover_existing_capacity(self, paths: list[Path]) -> None:
        """Admit only the latest persisted event for each thread.

        This lets a watcher started after a terminal capacity error recover it,
        while a later user message/turn or a later non-capacity error blocks the
        stale episode. Only structured metadata is retained.
        """
        latest: dict[str, tuple[tuple[str, int, int], ErrorEvent | ActivityEvent]] = {}
        for path in paths:
            try:
                modified = path.stat().st_mtime_ns
                with path.open("rb") as stream:
                    for line_number, raw_line in enumerate(stream):
                        if not raw_line.endswith(b"\n"):
                            continue
                        parsed = parse_record(raw_line.decode("utf-8", "replace"), path)
                        if parsed is None or not parsed.thread_id:
                            continue
                        key = (parsed.timestamp, modified, line_number)
                        previous = latest.get(parsed.thread_id)
                        if previous is None or key >= previous[0]:
                            latest[parsed.thread_id] = (key, parsed)
            except (OSError, UnicodeError):
                continue
        for parsed in latest.values():
            event = parsed[1]
            if not isinstance(event, ErrorEvent) or not event.is_capacity or event.will_retry is not False:
                continue
            decision = self.engine.handle_error(event)
            if decision.action != "ignore":
                self.logger(
                    {
                        "event": "startup_error",
                        "thread": safe_hash(event.thread_id),
                        "turn": safe_hash(event.turn_id),
                        "error_type": event.error_kind,
                        "will_retry": event.will_retry,
                        "decision": decision.action,
                        "reason": "existing_terminal_capacity",
                    }
                )

    def scan_once(self) -> list[Decision]:
        decisions: list[Decision] = []
        paths = list(self.sessions_dir.rglob("rollout-*.jsonl")) if self.sessions_dir.exists() else []
        for path in paths:
            try:
                size = path.stat().st_size
                if size < self.offsets.get(path, 0):
                    # A truncated/replaced file may contain old transcript.
                    # Discard it through EOF rather than replaying history.
                    self.offsets[path] = size
                    continue
                if size == self.offsets.get(path, 0):
                    continue
                with path.open("rb") as stream:
                    stream.seek(self.offsets.get(path, 0))
                    while True:
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            break  # Keep the offset until the writer finishes.
                        self.offsets[path] = stream.tell()
                        parsed = parse_record(line.decode("utf-8", "replace"), path)
                        if parsed is None:
                            continue
                        if isinstance(parsed, ActivityEvent):
                            decision = self.engine.handle_activity(parsed)
                            if decision.action != "ignore":
                                decisions.append(decision)
                                self.logger({"event": "activity", "thread": safe_hash(parsed.thread_id),
                                             "turn": safe_hash(parsed.turn_id), "kind": parsed.kind, "decision": decision.reason})
                            continue
                        decision = self.engine.handle_error(parsed)
                        if parsed.is_capacity or decision.action != "ignore":
                            decisions.append(decision)
                            self.logger(
                                {
                                    "event": "error",
                                    "thread": safe_hash(parsed.thread_id),
                                    "turn": safe_hash(parsed.turn_id),
                                    "error_type": parsed.error_kind,
                                    "will_retry": parsed.will_retry,
                                    "decision": decision.action,
                                    "reason": decision.reason,
                                }
                            )
            except (OSError, UnicodeError):
                continue
        return decisions


def default_config() -> dict[str, Any]:
    user_profile = os.environ.get("USERPROFILE") or str(Path.home())
    return {
        "enabled": True,
        "dry_run": True,
        "retry_mode": "dry_run",
        "retry_delays_seconds": list(DEFAULT_RETRY_DELAYS),
        "max_delay_seconds": 60.0,
        "max_attempts": 0,
        "jitter_ratio": 0.2,
        "poll_seconds": 5.0,
        "sessions_dir": str(Path(user_profile) / ".codex" / "sessions"),
        "runtime_path": None,
    }


def load_config() -> dict[str, Any]:
    config = default_config()
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return config
    path = Path(appdata) / "CodexNativeRetry" / "config.json"
    try:
        user_config = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(user_config, dict):
            config.update({key: value for key, value in user_config.items() if key in config})
    except (OSError, ValueError):
        pass
    return config


def _appdata_dir() -> Optional[Path]:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "CodexNativeRetry" if appdata else None


def _pid_file(kind: str) -> Optional[Path]:
    directory = _appdata_dir()
    filename = PID_FILENAMES.get(kind)
    return directory / filename if directory is not None and filename else None


def _read_pid(kind: str) -> Optional[int]:
    path = _pid_file(kind)
    if path is None:
        return None
    try:
        value = int(path.read_text(encoding="ascii").strip())
        return value if value > 0 else None
    except (OSError, ValueError):
        return None


def _process_alive(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False
    return True


def _write_pid(kind: str, pid: int) -> None:
    path = _pid_file(kind)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid), encoding="ascii")
    except OSError:
        pass


def _clear_pid(kind: str, pid: Optional[int] = None) -> None:
    path = _pid_file(kind)
    if path is None:
        return
    try:
        if pid is None or path.read_text(encoding="ascii").strip() == str(pid):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def append_log(entry: dict[str, Any]) -> None:
    directory = _appdata_dir()
    if directory is None:
        return
    path = directory / "events.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        allowed = {"event", "thread", "turn", "kind", "error_type", "will_retry", "decision", "reason", "attempt", "mode"}
        safe_entry = {"timestamp": utc_now(), **{key: value for key, value in entry.items() if key in allowed}}
        if path.exists() and path.stat().st_size > 1_048_576:
            path.replace(path.with_suffix(".jsonl.1"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(safe_entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _status_file() -> Optional[Path]:
    directory = _appdata_dir()
    return directory / "observer-status.json" if directory is not None else None


def read_observer_status() -> dict[str, Any]:
    path = _status_file()
    if path is None:
        return {"running": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {"running": False}
    except (OSError, ValueError):
        return {"running": False}


def write_observer_status(
    engine: RecoveryEngine,
    watcher: RolloutWatcher,
    last_retry: Optional[dict[str, Any]],
    *,
    running: bool = True,
) -> None:
    path = _status_file()
    if path is None:
        return
    snapshot = engine.snapshot()
    payload = {
        "timestamp": utc_now(),
        "running": running,
        "watcher_pid": os.getpid() if running else None,
        "sessions_dir": str(watcher.sessions_dir),
        "current_threads_observed": len({item["thread"] for item in snapshot if item["thread"] != "-"}),
        "current_error_episodes": snapshot,
        "pending_retries": [item for item in snapshot if item["state"] in {"pending", "claimed", "backoff", "would_retry"}],
        "last_retry": last_retry,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


def probe_uia() -> dict[str, Any]:
    """Read-only UIA capability probe; never focuses or invokes a control."""
    if os.name != "nt":
        return {"available": False, "reason": "windows_only"}
    script = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$windows=@(Get-Process ChatGPT -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
$result=@()
foreach($p in $windows){
  try {
    $root=[Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
    $condition=New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::ControlTypeProperty,[Windows.Automation.ControlType]::Button)
    $buttons=$root.FindAll([Windows.Automation.TreeScope]::Descendants,$condition)
    $retry=0; $enabled_retry=0; $invoke=0
    foreach($b in $buttons){
      if($b.Current.Name -match '^(Retry(?: .*)?|重试.*|重試.*)$'){
        $retry++
        if($b.Current.IsEnabled){$enabled_retry++}
        $pattern=$null
        if($b.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)){$invoke++}
      }
    }
    $result += [pscustomobject]@{button_count=$buttons.Count;exact_retry_buttons=$retry;enabled_retry_buttons=$enabled_retry;retry_invoke_available=$invoke}
  } catch {}
}
[pscustomobject]@{available=$true;windows=$result} | ConvertTo-Json -Compress -Depth 4
'''
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = json.loads(completed.stdout) if completed.stdout.strip() else {}
        if isinstance(value, dict):
            value["invoke_enabled"] = False
            value["reason"] = "read-only probe; no correlated Retry action is enabled"
            return value
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"available": False, "reason": "UIAutomation probe failed"}


def invoke_uia_retry() -> dict[str, Any]:
    """Invoke one visible Codex Retry action without focus or input simulation."""
    if os.name != "nt":
        return {"invoked": False, "reason": "windows_only"}
    script = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$matches=@()
$windows=@(Get-Process ChatGPT -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
if($windows.Count -ne 1){
  [pscustomobject]@{invoked=$false;reason=if($windows.Count -eq 0){'no_codex_window'}else{'multiple_codex_windows'}} | ConvertTo-Json -Compress
  exit 0
}
foreach($p in $windows){
  try {
    $root=[Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
    $condition=New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::ControlTypeProperty,[Windows.Automation.ControlType]::Button)
    $buttons=$root.FindAll([Windows.Automation.TreeScope]::Descendants,$condition)
    foreach($b in $buttons){
      if($b.Current.IsEnabled -and $b.Current.Name -match '^(Retry(?: .*)?|重试.*|重試.*)$'){$matches += $b}
    }
  } catch {}
}
if($matches.Count -ne 1){
  [pscustomobject]@{invoked=$false;reason=if($matches.Count -eq 0){'no_unique_retry_button'}else{'multiple_retry_buttons'}} | ConvertTo-Json -Compress
  exit 0
}
$pattern=$null
if(-not $matches[0].TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)){
  [pscustomobject]@{invoked=$false;reason='invoke_pattern_unavailable'} | ConvertTo-Json -Compress
  exit 0
}
$pattern.Invoke()
[pscustomobject]@{invoked=$true;reason='uia_invoke'} | ConvertTo-Json -Compress
'''
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = json.loads(completed.stdout) if completed.stdout.strip() else {}
        if isinstance(value, dict):
            return {"invoked": bool(value.get("invoked")), "reason": value.get("reason", "uia_probe")}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"invoked": False, "reason": "UIAutomation invoke failed"}


def make_engine(config: dict[str, Any]) -> RecoveryEngine:
    configured_delays = config.get("retry_delays_seconds", DEFAULT_RETRY_DELAYS)
    if not isinstance(configured_delays, (list, tuple)):
        configured_delays = list(DEFAULT_RETRY_DELAYS)
    try:
        retry_delays = [float(value) for value in configured_delays]
    except (TypeError, ValueError):
        retry_delays = list(DEFAULT_RETRY_DELAYS)
    return RecoveryEngine(
        retry_delays=retry_delays,
        max_delay=float(config["max_delay_seconds"]),
        max_attempts=int(config["max_attempts"]),
        jitter_ratio=float(config["jitter_ratio"]),
    )


def cmd_diagnose(args: argparse.Namespace) -> int:
    config = load_config()
    runtime = discover_runtime(args.runtime or config.get("runtime_path"))
    report = probe_runtime(runtime)
    report["sessions_dir"] = str(Path(os.path.expandvars(config["sessions_dir"])).expanduser())
    report["dry_run_default"] = bool(config["dry_run"])
    report["configured_retry_mode"] = config.get("retry_mode", "dry_run")
    report["retry_delays_seconds"] = config.get("retry_delays_seconds", list(DEFAULT_RETRY_DELAYS))
    report["max_attempts"] = config.get("max_attempts", 0)
    report["jitter_ratio"] = config.get("jitter_ratio", 0.2)
    report["watcher_pid"] = _read_pid("watcher") if _process_alive(_read_pid("watcher")) else None
    report["app_server_pid"] = _read_pid("app_server") if _process_alive(_read_pid("app_server")) else None
    report["ui_automation"] = {"available": False, "reason": "ignored_for_cli_target"}
    report["target"] = "codex_cli"
    report["retry_mode"] = "native_cli" if report.get("cli_daemon_available") else "disabled_no_shared_daemon"
    observer = read_observer_status()
    observer_pid = observer.get("watcher_pid") if isinstance(observer, dict) else None
    if observer.get("running") and not _process_alive(observer_pid if isinstance(observer_pid, int) else _read_pid("watcher")):
        observer["running"] = False
    report["observer"] = observer
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["codex_detected"] else 1


def _spawn_watcher(runtime: Path, *, mode: str, sessions_dir: Optional[str]) -> dict[str, Any]:
    existing_pid = _read_pid("watcher")
    if _process_alive(existing_pid):
        return {"running": True, "started": False, "pid": existing_pid, "reason": "already_running"}
    _clear_pid("watcher")
    command = [sys.executable, str(Path(__file__).resolve()), "watch"]
    if mode == "dry_run":
        command.append("--dry-run")
    else:
        command.extend(["--live", "--retry-mode", "native_cli"])
    command.extend(["--runtime", str(runtime)])
    if sessions_dir:
        command.extend(["--sessions-dir", sessions_dir])
    directory = _appdata_dir()
    log_stream: Any = subprocess.DEVNULL
    log_path: Optional[Path] = None
    try:
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / "watcher.log"
            log_stream = log_path.open("ab")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            creationflags=_background_creationflags(),
        )
    except OSError as exc:
        if hasattr(log_stream, "close"):
            log_stream.close()
        return {"running": False, "started": False, "reason": f"watcher_start_failed:{type(exc).__name__}"}
    finally:
        if hasattr(log_stream, "close"):
            log_stream.close()

    deadline = time.monotonic() + 5.0
    child_pid: Optional[int] = None
    while time.monotonic() < deadline:
        child_pid = _read_pid("watcher")
        if _process_alive(child_pid):
            break
        if process.poll() is not None:
            break
        time.sleep(0.1)
    running = _process_alive(child_pid)
    return {
        "running": running,
        "started": running,
        "pid": child_pid if running else None,
        "reason": "started" if running else "watcher_exited_during_start",
        "log": str(log_path) if log_path else None,
    }


def cmd_start(args: argparse.Namespace) -> int:
    """Start the shared daemon and one watcher for every saved CLI rollout."""
    config = load_config()
    if not config.get("enabled", True):
        print(json.dumps({"event": "start", "enabled": False}, ensure_ascii=False))
        return 0
    mode = "dry_run" if args.dry_run else (args.retry_mode or "native_cli")
    if mode not in {"dry_run", "native_cli"}:
        print("start supports only dry_run or native_cli", file=sys.stderr)
        return 2
    runtime = discover_runtime(args.runtime or config.get("runtime_path"))
    if runtime is None:
        runtime = _discover_cli_executable()
    if runtime is None:
        print("Codex runtime was not found; pass --runtime C:\\path\\to\\codex.exe", file=sys.stderr)
        return 3

    daemon = ensure_cli_daemon(runtime)
    if not daemon.get("available"):
        append_log({"event": "service_start_failed", "mode": mode, "reason": daemon.get("reason", "daemon_unavailable")})
        print(json.dumps({"event": "start", "daemon": daemon, "watcher": {"running": False}}, ensure_ascii=False))
        return 3
    sessions_dir = args.sessions_dir or config.get("sessions_dir")
    watcher = _spawn_watcher(runtime, mode=mode, sessions_dir=sessions_dir)
    append_log({"event": "service_started", "mode": mode,
                "reason": daemon.get("route", "shared_daemon")})
    print(json.dumps({
        "event": "start",
        "mode": mode,
        "daemon": {
            "available": bool(daemon.get("available")),
            "route": daemon.get("route"),
            "started": bool(daemon.get("started")),
            "pid": daemon.get("pid"),
        },
        "watcher": watcher,
        "sessions_dir": str(Path(os.path.expandvars(sessions_dir)).expanduser()) if sessions_dir else None,
        "note": "Use codex --remote unix:// for CLI conversations that can receive native continuation.",
    }, ensure_ascii=False, indent=2))
    return 0 if watcher.get("running") else 3


def cmd_watch(args: argparse.Namespace) -> int:
    config = load_config()
    if args.dry_run:
        config["dry_run"] = True
    mode = args.retry_mode or config.get("retry_mode", "dry_run")
    if not args.live and config.get("dry_run", True) is True:
        mode = "dry_run"
    if args.dry_run:
        mode = "dry_run"
    if args.live:
        config["dry_run"] = False
        mode = args.retry_mode or ("native_cli" if mode == "dry_run" else mode)
    if mode not in {"dry_run", "native_cli", "native_ui"}:
        print(f"unsupported retry mode: {mode}", file=sys.stderr)
        return 2
    if mode == "dry_run" and config["dry_run"] is not True:
        append_log({"event": "live_disabled", "reason": "native_equivalence_unverified"})
        print("Live retry disabled: choose --retry-mode native_cli after inspecting diagnose.", file=sys.stderr)
        return 3
    runtime = discover_runtime(args.runtime or config.get("runtime_path"))
    if mode == "native_cli":
        daemon = probe_cli_daemon(runtime)
        if not daemon.get("available"):
            reason = daemon.get("reason", "shared CLI app-server daemon is unavailable")
            append_log({"event": "live_disabled", "mode": mode, "reason": reason})
            print(
                "Native CLI retry disabled: shared app-server daemon is unavailable. "
                "Start `codex app-server daemon start` (or bundled runtime `app-server --listen unix://`), "
                "then open/resume the CLI session again.",
                file=sys.stderr,
            )
            return 3
    if mode == "native_ui":
        capability = probe_runtime(runtime)
        if not capability.get("codex_detected") or not capability.get("desktop_package", {}).get("version"):
            append_log({"event": "live_disabled", "reason": "codex_version_probe_failed"})
            print("Native UI retry disabled: Codex version probe failed.", file=sys.stderr)
            return 3
        ui = probe_uia()
        if not ui.get("available"):
            append_log({"event": "live_disabled", "reason": "uia_unavailable"})
            print("Native UI retry disabled: UI Automation is unavailable.", file=sys.stderr)
            return 3
    if not config.get("enabled", True):
        print("disabled")
        return 0
    existing_pid = _read_pid("watcher")
    if existing_pid != os.getpid() and _process_alive(existing_pid):
        print(json.dumps({"event": "watcher_already_running", "pid": existing_pid}, ensure_ascii=False))
        return 0
    _clear_pid("watcher")
    _write_pid("watcher", os.getpid())
    sessions_dir = Path(os.path.expandvars(args.sessions_dir or config["sessions_dir"])).expanduser()
    engine = make_engine(config)
    def observe(entry: dict[str, Any]) -> None:
        append_log(entry)
        print(json.dumps(entry, ensure_ascii=False))

    watcher = RolloutWatcher(sessions_dir, engine, observe)
    watcher.prime(recover_existing_capacity=True)
    print(f"NativeRetryObserver: watching {sessions_dir} (mode={mode})")
    last_retry = None
    try:
        while True:
            watcher.scan_once()
            due = engine.poll_due()
            if mode == "native_ui" and len(due) > 1:
                # A single visible Retry button cannot be safely associated
                # with multiple background episodes.
                for episode in due:
                    append_log({"event": "live_disabled", "thread": safe_hash(episode.event.thread_id),
                                "turn": safe_hash(episode.event.turn_id), "reason": "multiple_due_episodes"})
                    engine.native_uncertain(episode)
                due = []
            for episode in due:
                entry = {
                    "event": "would_retry" if mode == "dry_run" else "retry_due",
                    "thread": safe_hash(episode.event.thread_id),
                    "turn": safe_hash(episode.event.turn_id),
                    "attempt": episode.attempt,
                    "mode": mode,
                }
                append_log(entry)
                print(json.dumps(entry, ensure_ascii=False))
                last_retry = {"timestamp": utc_now(), **entry}
                if mode == "dry_run":
                    engine.mark_dry_run(episode)
                    continue
                if mode == "native_cli":
                    action = cli_native_retry(
                        runtime,
                        episode.event.thread_id or "",
                        episode.event.turn_id or "",
                    )
                    safe_action = {
                        "event": "cli_turn_start",
                        "attempt": episode.attempt,
                        "mode": mode,
                        "status": action.get("status"),
                        "reason": action.get("reason", "unknown"),
                    }
                    append_log(safe_action)
                    print(json.dumps(safe_action, ensure_ascii=False))
                    if action.get("status") == "accepted":
                        engine.native_result(episode, True, next_turn_id=action.get("turn_id"))
                    elif action.get("status") == "uncertain":
                        engine.native_uncertain(episode)
                    elif action.get("status") == "rejected":
                        engine.native_result(episode, False)
                    else:
                        engine.cancel_episode(episode, str(action.get("reason", "cli_thread_state")))
                    continue

                action = invoke_uia_retry()
                append_log({"event": "uia_invoke", "attempt": episode.attempt, "mode": mode,
                            "reason": action.get("reason", "unknown")})
                print(json.dumps({"event": "uia_invoke", "attempt": episode.attempt,
                                  "invoked": bool(action.get("invoked")),
                                  "reason": action.get("reason", "unknown")}, ensure_ascii=False))
                if action.get("invoked"):
                    engine.native_result(episode, True)
                else:
                    engine.native_result(episode, False)
            write_observer_status(engine, watcher, last_retry)
            if args.once:
                return 0
            time.sleep(max(0.2, float(args.poll_seconds or config["poll_seconds"])))
    except KeyboardInterrupt:
        return 0
    finally:
        write_observer_status(engine, watcher, last_retry, running=False)
        _clear_pid("watcher", os.getpid())


def cmd_request(args: argparse.Namespace) -> int:
    if not args.thread_id:
        print("thread id is required", file=sys.stderr)
        return 2
    payload = cli_native_request(args.thread_id) if args.mode == "cli" else native_request(
        args.thread_id, turn_trigger=args.turn_trigger
    )
    print(json.dumps({"method": "turn/start", "params": payload}, ensure_ascii=False, indent=2))
    print("send disabled: request command is diagnostic-only", file=sys.stderr)
    return 3 if args.live else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed Codex native capacity retry observer")
    sub = parser.add_subparsers(dest="command", required=True)
    diagnose = sub.add_parser("diagnose")
    diagnose.add_argument("--runtime")
    diagnose.set_defaults(func=cmd_diagnose)

    start = sub.add_parser("start", help="start one shared app-server and the background watcher")
    start.add_argument("--dry-run", action="store_true", help="observe only; do not send native retry")
    start.add_argument("--sessions-dir")
    start.add_argument("--runtime")
    start.add_argument("--retry-mode", choices=("dry_run", "native_cli"))
    start.set_defaults(func=cmd_start)

    watch = sub.add_parser("watch")
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--live", action="store_true")
    watch.add_argument("--sessions-dir")
    watch.add_argument("--runtime")
    watch.add_argument("--poll-seconds", type=float)
    watch.add_argument("--once", action="store_true", help="one passive scan, then exit")
    watch.add_argument("--retry-mode", choices=("dry_run", "native_cli", "native_ui"))
    watch.set_defaults(func=cmd_watch)

    request = sub.add_parser("request", help="print the safe native continuation request")
    request.add_argument("--thread-id", required=True)
    request.add_argument(
        "--turn-trigger",
        choices=("capacity_retry_automatic", "capacity_retry_manual"),
        default="capacity_retry_automatic",
    )
    request.add_argument("--mode", choices=("cli", "desktop"), default="cli")
    request.add_argument("--live", action="store_true")
    request.set_defaults(func=cmd_request)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
