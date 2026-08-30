"""Find the local control plane the public CLI talks to.

`PROJECT_SPEC.md` §14.2 gives the application, not the CLI, ownership of the
helper's lifecycle and credential. Whatever owns the helper — today the
developer launcher, later the signed application — publishes a handshake file
naming the loopback endpoint and its credential; the CLI only reads it.

The credential lives in a `0600` file rather than in a command line or an
environment variable so it never appears in process listings, shell history, or
agent-visible configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any


DEFAULT_STATE_ROOT = Path.home() / "Library" / "Application Support" / "VoxInk"
CONTROL_ENDPOINT_FILENAME = "control-endpoint.json"
ENDPOINT_ENV_VAR = "VOXINK_CONTROL_ENDPOINT"
ENDPOINT_SCHEMA_VERSION = 1


class EndpointUnavailable(RuntimeError):
    """No usable control endpoint was published."""


@dataclass(frozen=True, slots=True)
class ControlEndpoint:
    base_url: str
    auth_secret: str | None
    protocol_version: str | None = None
    service_version: str | None = None
    pid: int | None = None
    path: Path | None = None


def endpoint_path(state_root: Path | None = None) -> Path:
    override = os.environ.get(ENDPOINT_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return (state_root or DEFAULT_STATE_ROOT).expanduser() / CONTROL_ENDPOINT_FILENAME


def publish(
    path: Path,
    *,
    base_url: str,
    auth_secret: str | None,
    protocol_version: str | None = None,
    service_version: str | None = None,
    pid: int | None = None,
) -> Path:
    """Write the handshake file readable only by its owner.

    The file is written to a private temporary name and renamed into place so a
    CLI reading concurrently never sees a half-written credential.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "schema_version": ENDPOINT_SCHEMA_VERSION,
        "base_url": base_url,
        "auth_secret": auth_secret,
        "protocol_version": protocol_version,
        "service_version": service_version,
        "pid": pid if pid is not None else os.getpid(),
    }
    handle, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".control-", suffix=".json")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
        temporary.chmod(0o600)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def revoke(path: Path) -> None:
    """Remove a published endpoint, tolerating one already gone."""
    Path(path).expanduser().unlink(missing_ok=True)


def load(path: Path | None = None, *, state_root: Path | None = None) -> ControlEndpoint:
    resolved = Path(path).expanduser() if path is not None else endpoint_path(state_root)
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EndpointUnavailable(
            "No VoxInk helper is running. Start one with: ./scripts/voxink-dev"
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise EndpointUnavailable(f"The local control endpoint is unreadable: {exc}") from exc
    if document.get("schema_version") != ENDPOINT_SCHEMA_VERSION:
        raise EndpointUnavailable(
            "The local control endpoint uses an unsupported format; restart the helper."
        )
    base_url = document.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise EndpointUnavailable("The local control endpoint names no address.")
    if not _is_loopback(base_url):
        raise EndpointUnavailable(f"The local control endpoint is not loopback-only: {base_url}")
    secret = document.get("auth_secret")
    return ControlEndpoint(
        base_url=base_url.rstrip("/"),
        auth_secret=secret if isinstance(secret, str) and secret else None,
        protocol_version=_optional_str(document.get("protocol_version")),
        service_version=_optional_str(document.get("service_version")),
        pid=document.get("pid") if isinstance(document.get("pid"), int) else None,
        path=resolved,
    )


def _is_loopback(base_url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname or ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
