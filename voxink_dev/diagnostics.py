"""Minimal private diagnostics for supervising local processes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any


_ALLOWED_FIELDS = {
    "code",
    "component",
    "elapsed_ms",
    "event",
    "level",
    "pid",
    "port",
    "protocol_version",
    "return_code",
    "status",
}


class DiagnosticLog:
    """Append allowlisted operational metadata to a mode-0600 JSONL file."""

    def __init__(self, log_root: Path) -> None:
        log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            log_root.chmod(0o700)
        except OSError:
            pass
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = log_root / f"developer-{stamp}-{os.getpid()}.jsonl"
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._handle = os.fdopen(descriptor, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def emit(self, event: str, *, level: str = "info", **fields: Any) -> None:
        record = {"event": event, "level": level, **fields}
        unexpected = set(record) - _ALLOWED_FIELDS
        if unexpected:
            raise ValueError(f"diagnostic fields are not allowlisted: {', '.join(sorted(unexpected))}")
        if level not in {"info", "warning", "error"}:
            raise ValueError(f"invalid diagnostic level: {level}")
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()

    def __enter__(self) -> DiagnosticLog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
