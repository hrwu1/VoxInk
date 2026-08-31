"""Canonical identifiers for user-owned VoxInk session artifacts."""

from __future__ import annotations

from datetime import datetime


SESSION_ID_FORMAT = "%Y-%m-%d_%H-%M-%S-%f"


def new_session_id(now: datetime | None = None) -> str:
    """Return a filesystem-safe session ID in local wall-clock time."""
    moment = now if now is not None else datetime.now().astimezone()
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("session ID timestamps must include a timezone")
    return moment.strftime(SESSION_ID_FORMAT)
