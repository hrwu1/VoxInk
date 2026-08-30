"""The versioned public result contract shared by every `voxink` command.

`PROJECT_SPEC.md` §14.4 makes these envelopes, error codes, and exit codes a
public API: consumers may depend on them instead of parsing prose. Additive
fields are allowed within a schema version; removals and renamed codes require
a new one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)


class ErrorCode:
    """The §14.4 error taxonomy."""

    INVALID_REQUEST = "invalid_request"
    NOT_READY = "not_ready"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    INVALID_TRANSITION = "invalid_transition"
    CONFLICT = "conflict"
    CAPTURE_UNAVAILABLE = "capture_unavailable"
    MODEL_BUSY = "model_busy"
    DISK_LOW = "disk_low"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_INVALID_REQUEST = 2
EXIT_NOT_READY = 3
EXIT_NOT_FOUND = 4
EXIT_NOT_PERMITTED = 5
EXIT_CONFLICT = 6
EXIT_UNAVAILABLE = 7
EXIT_TIMEOUT = 8

# Distinct failures share an exit code only when a caller would react to them
# the same way; the JSON error code always distinguishes them exactly.
EXIT_CODES: Mapping[str, int] = {
    ErrorCode.INVALID_REQUEST: EXIT_INVALID_REQUEST,
    ErrorCode.NOT_READY: EXIT_NOT_READY,
    ErrorCode.NOT_FOUND: EXIT_NOT_FOUND,
    ErrorCode.PERMISSION_DENIED: EXIT_NOT_PERMITTED,
    ErrorCode.APPROVAL_REQUIRED: EXIT_NOT_PERMITTED,
    ErrorCode.INVALID_TRANSITION: EXIT_CONFLICT,
    ErrorCode.CONFLICT: EXIT_CONFLICT,
    ErrorCode.CAPTURE_UNAVAILABLE: EXIT_UNAVAILABLE,
    ErrorCode.MODEL_BUSY: EXIT_UNAVAILABLE,
    ErrorCode.DISK_LOW: EXIT_UNAVAILABLE,
    ErrorCode.TIMEOUT: EXIT_TIMEOUT,
    ErrorCode.INTERNAL_ERROR: EXIT_INTERNAL_ERROR,
}

RETRYABLE_CODES = frozenset(
    {
        ErrorCode.NOT_READY,
        ErrorCode.APPROVAL_REQUIRED,
        ErrorCode.CONFLICT,
        ErrorCode.MODEL_BUSY,
        ErrorCode.TIMEOUT,
    }
)


def exit_code_for(code: str) -> int:
    return EXIT_CODES.get(code, EXIT_INTERNAL_ERROR)


@dataclass(frozen=True, slots=True)
class Failure(Exception):
    """A command failure that carries a public error code rather than prose."""

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None

    def __str__(self) -> str:
        return self.message

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.code)


def success(
    command: str,
    data: Any = None,
    *,
    warnings: Sequence[str] = (),
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "ok": True,
        "command": command,
        "data": {} if data is None else data,
        "warnings": list(warnings),
    }


def failure(
    command: str,
    error: Failure,
    *,
    request_id: str | None = None,
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "ok": False,
        "command": command,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "request_id": error.request_id or request_id,
            "details": dict(error.details),
        },
    }


def dumps(envelope: Mapping[str, Any]) -> str:
    """Serialize one envelope as a single line, so `jsonl` needs no other writer."""
    return json.dumps(envelope, ensure_ascii=False, sort_keys=False, default=str)
