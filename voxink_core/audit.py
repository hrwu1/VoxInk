"""Append-only audit trail of control-plane mutations (`PROJECT_SPEC.md` §14.5).

Every line says who did what, to which session, with what result and under
which scope. It never carries transcript text, audio, credentials, or the
arguments of a command beyond what identifies the operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any


AUDIT_FILENAME = "audit.jsonl"
#: When the log passes this size it is rotated once to `audit.jsonl.1`.
ROTATE_BYTES = 5 * 1024 * 1024


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def record(
        self,
        operation: str,
        *,
        client: str,
        result: str,
        request_id: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        entry = {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "operation": operation,
            "client": client,
            "result": result,
            "request_id": request_id,
            "session_id": session_id,
            "scope": scope,
            "details": _bounded(details),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > ROTATE_BYTES:
                    self.path.replace(self.path.with_suffix(".jsonl.1"))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                os.chmod(self.path, 0o600)
            except OSError:
                # The audit trail must never take the operation down with it.
                pass
        return entry

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            return []
        entries = []
        for line in lines[-max(1, limit):]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries


def _bounded(details: dict[str, Any]) -> dict[str, Any]:
    """Keep only short scalars and short lists: identifiers, never content."""
    bounded: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, (bool, int, float)) or value is None:
            bounded[key] = value
        elif isinstance(value, str):
            bounded[key] = value[:120]
        elif isinstance(value, (list, tuple)):
            bounded[key] = [str(item)[:80] for item in list(value)[:10]]
    return bounded
