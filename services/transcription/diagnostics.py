"""Private operational diagnostics for the local service.

Two kinds of file under the state root's ``logs/`` directory, both mode 0600:

* ``service-<stamp>-<pid>.jsonl``: one JSON line per stream or capture
  lifecycle event (a stream opened or released, a grace period expiring,
  live speakers stopping, the helper exiting). Identifiers and codes only.
* ``capture-<session>.log``: the capture helper's status lines for one
  session, timestamped, so a helper that reconnected or spooled audio can be
  explained after it has exited.

Neither carries transcript text, audio, or the launch credential, and a
failure to write must never take the operation down with it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, TextIO


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scalar(item) for item in list(value)[:20]]
    return str(value)[:300]


class CaptureLog:
    """Timestamped lines from one capture helper run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        with self._lock:
            try:
                if self._handle is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    self._handle = os.fdopen(descriptor, "a", encoding="utf-8")
                self._handle.write(f"{_now()} {line}\n")
                self._handle.flush()
            except OSError:
                return

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass


class ServiceDiagnostics:
    def __init__(self, log_root: Path) -> None:
        self.log_root = Path(log_root).expanduser()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.log_root / f"service-{stamp}-{os.getpid()}.jsonl"
        self._lock = threading.Lock()

    def event(self, name: str, *, level: str = "info", **fields: Any) -> None:
        record = {"time": _now(), "event": name, "level": level}
        record.update({key: _scalar(value) for key, value in fields.items()})
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            try:
                self.log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                return

    def capture_log(self, session_id: str) -> CaptureLog:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in session_id) or "session"
        return CaptureLog(self.log_root / f"capture-{safe}.log")
