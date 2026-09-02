"""Scoped, time-boxed grants that let an agent use VoxInk under consent.

`PROJECT_SPEC.md` §14.5: sensitive operations need a grant the *user* issued
in the application, never one the CLI minted for itself. A grant names its
scopes, may be limited to certain applications, a maximum recording length,
whether audio may be kept, an expiry, and single use. The bearer token is
shown once at issue time and only its hash is stored.

An agent that lacks a grant asks for one: a pending request records what it
wants and hands back a claim secret. When the user approves it in the UI,
the token is parked on the request until the agent claims it with that
secret, so nothing but the requester can pick it up.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from typing import Any, Iterable


GRANT_SCOPES = (
    "capture.microphone",
    "capture.system",
    "capture.applications",
    "sessions.read",
    "transcripts.read",
    "audio.read",
    "audio.retain",
    "vocabulary.write",
    "models.switch",
    "models.import",
    "artifacts.delete",
)

TOKEN_PREFIX = "vxg_"
GRANTS_FILENAME = "grants.json"
SCHEMA_VERSION = 1
#: Unapproved requests older than this are dropped when the store is read.
PENDING_TTL = timedelta(hours=24)
MAX_PENDING = 20


class GrantError(ValueError):
    """A grant or request could not be created or used."""


@dataclass(frozen=True, slots=True)
class Grant:
    id: str
    label: str
    token_hash: str
    scopes: tuple[str, ...]
    #: Bundle identifiers this grant may capture; empty means any.
    applications: tuple[str, ...] = ()
    #: Longest recording it may start, in seconds; None means unlimited.
    max_duration_s: int | None = None
    retain_audio: bool = False
    expires_at: str | None = None
    single_use: bool = False
    used_at: str | None = None
    revoked_at: str | None = None
    created_at: str = ""
    request_id: str | None = None

    def is_valid(self, now: datetime | None = None) -> bool:
        if self.revoked_at:
            return False
        if self.single_use and self.used_at:
            return False
        if self.expires_at and _parse(self.expires_at) <= (now or _now()):
            return False
        return True

    def allows(self, scope: str) -> bool:
        return scope in self.scopes

    def to_dict(self, *, include_hash: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["scopes"] = list(self.scopes)
        data["applications"] = list(self.applications)
        data["valid"] = self.is_valid()
        if not include_hash:
            data.pop("token_hash")
        return data


@dataclass(frozen=True, slots=True)
class PendingRequest:
    id: str
    client: str
    reason: str
    scopes: tuple[str, ...]
    applications: tuple[str, ...]
    max_duration_s: int | None
    retain_audio: bool
    claim_hash: str
    created_at: str
    state: str = "pending"
    grant_id: str | None = None
    #: The issued token, parked here until the requester claims it.
    parked_token: str | None = None
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("claim_hash")
        data.pop("parked_token")
        data["scopes"] = list(self.scopes)
        data["applications"] = list(self.applications)
        data["claimable"] = self.state == "approved" and self.parked_token is not None
        return data


@dataclass
class _Document:
    grants: list[Grant] = field(default_factory=list)
    requests: list[PendingRequest] = field(default_factory=list)


class GrantStore:
    """Owner-only JSON store of grants and pending requests."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    # -- grants -----------------------------------------------------------------

    def issue(
        self,
        *,
        label: str,
        scopes: Iterable[str],
        applications: Iterable[str] = (),
        max_duration_s: int | None = None,
        retain_audio: bool = False,
        expires_in_s: int | None = None,
        single_use: bool = False,
        request_id: str | None = None,
    ) -> tuple[Grant, str]:
        """Create a grant and return it with its one-time bearer token."""
        scope_tuple = tuple(dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip()))
        unknown = [scope for scope in scope_tuple if scope not in GRANT_SCOPES]
        if unknown:
            raise GrantError(f"unknown scope(s): {', '.join(unknown)}")
        if not scope_tuple:
            raise GrantError("a grant needs at least one scope")
        if max_duration_s is not None and max_duration_s <= 0:
            raise GrantError("max_duration_s must be positive")
        if expires_in_s is not None and expires_in_s <= 0:
            raise GrantError("expires_in_s must be positive")
        if retain_audio and "audio.retain" not in scope_tuple:
            scope_tuple = (*scope_tuple, "audio.retain")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = _now()
        grant = Grant(
            id=f"grant_{secrets.token_hex(6)}",
            label=" ".join(str(label).split()) or "agent",
            token_hash=_hash(token),
            scopes=scope_tuple,
            applications=tuple(dict.fromkeys(str(app).strip() for app in applications if str(app).strip())),
            max_duration_s=max_duration_s,
            retain_audio=retain_audio,
            expires_at=_stamp(now + timedelta(seconds=expires_in_s)) if expires_in_s else None,
            single_use=single_use,
            created_at=_stamp(now),
            request_id=request_id,
        )
        with self._lock:
            document = self._read()
            document.grants.append(grant)
            self._write(document)
        return grant, token

    def authenticate(self, token: str | None) -> Grant | None:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        digest = _hash(token)
        with self._lock:
            for grant in self._read().grants:
                if secrets.compare_digest(grant.token_hash, digest):
                    return grant if grant.is_valid() else None
        return None

    def consume(self, grant_id: str) -> Grant | None:
        """Mark a single-use grant as used; a no-op for reusable grants."""
        with self._lock:
            document = self._read()
            for index, grant in enumerate(document.grants):
                if grant.id == grant_id:
                    if grant.single_use and not grant.used_at:
                        updated = replace(grant, used_at=_stamp(_now()))
                        document.grants[index] = updated
                        self._write(document)
                        return updated
                    return grant
        return None

    def revoke(self, grant_id: str) -> Grant | None:
        with self._lock:
            document = self._read()
            for index, grant in enumerate(document.grants):
                if grant.id == grant_id:
                    if grant.revoked_at:
                        return grant
                    updated = replace(grant, revoked_at=_stamp(_now()))
                    document.grants[index] = updated
                    self._write(document)
                    return updated
        return None

    def list(self) -> list[Grant]:
        with self._lock:
            return list(self._read().grants)

    def get(self, grant_id: str) -> Grant | None:
        return next((grant for grant in self.list() if grant.id == grant_id), None)

    # -- requests ---------------------------------------------------------------

    def request(
        self,
        *,
        client: str,
        reason: str,
        scopes: Iterable[str],
        applications: Iterable[str] = (),
        max_duration_s: int | None = None,
        retain_audio: bool = False,
    ) -> tuple[PendingRequest, str]:
        """Record what an agent wants and return the request with its claim secret."""
        scope_tuple = tuple(dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip()))
        unknown = [scope for scope in scope_tuple if scope not in GRANT_SCOPES]
        if unknown:
            raise GrantError(f"unknown scope(s): {', '.join(unknown)}")
        if not scope_tuple:
            raise GrantError("a request needs at least one scope")
        claim_secret = secrets.token_urlsafe(24)
        pending = PendingRequest(
            id=f"req_{secrets.token_hex(6)}",
            client=" ".join(str(client).split())[:80] or "agent",
            reason=" ".join(str(reason).split())[:300],
            scopes=scope_tuple,
            applications=tuple(dict.fromkeys(str(app).strip() for app in applications if str(app).strip())),
            max_duration_s=max_duration_s if max_duration_s and max_duration_s > 0 else None,
            retain_audio=bool(retain_audio),
            claim_hash=_hash(claim_secret),
            created_at=_stamp(_now()),
        )
        with self._lock:
            document = self._read()
            open_requests = [item for item in document.requests if item.state == "pending"]
            if len(open_requests) >= MAX_PENDING:
                raise GrantError("too many unanswered requests; approve or deny some first")
            document.requests.append(pending)
            self._write(document)
        return pending, claim_secret

    def pending(self) -> list[PendingRequest]:
        with self._lock:
            return [item for item in self._read().requests if item.state == "pending"]

    def get_request(self, request_id: str) -> PendingRequest | None:
        with self._lock:
            return next((item for item in self._read().requests if item.id == request_id), None)

    def approve(
        self,
        request_id: str,
        *,
        label: str | None = None,
        expires_in_s: int | None = None,
        single_use: bool = False,
        max_duration_s: int | None = None,
    ) -> Grant:
        """Issue the grant a request asked for and park its token for the claimer.

        The user may tighten what was asked for (a shorter maximum duration),
        never widen it: scopes and applications come from the request.
        """
        with self._lock:
            document = self._read()
            pending = next((item for item in document.requests if item.id == request_id), None)
            if pending is None:
                raise GrantError("unknown request")
            if pending.state != "pending":
                raise GrantError(f"request already {pending.state}")
            duration = pending.max_duration_s
            if max_duration_s is not None:
                duration = min(duration, max_duration_s) if duration else max_duration_s
            grant, token = self.issue(
                label=label or pending.client,
                scopes=pending.scopes,
                applications=pending.applications,
                max_duration_s=duration,
                retain_audio=pending.retain_audio,
                expires_in_s=expires_in_s,
                single_use=single_use,
                request_id=pending.id,
            )
            document = self._read()
            for index, item in enumerate(document.requests):
                if item.id == request_id:
                    document.requests[index] = replace(
                        item, state="approved", grant_id=grant.id, parked_token=token, decided_at=_stamp(_now())
                    )
            self._write(document)
            return grant

    def deny(self, request_id: str) -> PendingRequest:
        with self._lock:
            document = self._read()
            for index, item in enumerate(document.requests):
                if item.id == request_id:
                    if item.state != "pending":
                        raise GrantError(f"request already {item.state}")
                    updated = replace(item, state="denied", decided_at=_stamp(_now()))
                    document.requests[index] = updated
                    self._write(document)
                    return updated
        raise GrantError("unknown request")

    def claim(self, request_id: str, claim_secret: str) -> tuple[PendingRequest, str | None]:
        """Hand the parked token to whoever holds the claim secret, once.

        Returns the request and the token, or the request and None while it
        is still pending. A denied request raises.
        """
        with self._lock:
            document = self._read()
            for index, item in enumerate(document.requests):
                if item.id != request_id:
                    continue
                if not secrets.compare_digest(item.claim_hash, _hash(claim_secret)):
                    raise GrantError("claim secret does not match")
                if item.state == "denied":
                    raise GrantError("the request was denied")
                if item.state == "pending":
                    return item, None
                token = item.parked_token
                if token is None:
                    raise GrantError("the token was already claimed")
                updated = replace(item, parked_token=None, state="claimed")
                document.requests[index] = updated
                self._write(document)
                return updated, token
        raise GrantError("unknown request")

    # -- storage ----------------------------------------------------------------

    def _read(self) -> _Document:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _Document()
        except (OSError, json.JSONDecodeError) as exc:
            raise GrantError(f"grant store is unreadable: {exc}") from exc
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise GrantError("grant store uses an unsupported format")
        grants = [Grant(**{**item, "scopes": tuple(item["scopes"]), "applications": tuple(item.get("applications", ()))}) for item in raw.get("grants", [])]
        horizon = _now() - PENDING_TTL
        requests = [
            PendingRequest(**{**item, "scopes": tuple(item["scopes"]), "applications": tuple(item.get("applications", ()))})
            for item in raw.get("requests", [])
        ]
        requests = [
            item for item in requests
            if not (item.state == "pending" and _parse(item.created_at) < horizon)
        ]
        return _Document(grants, requests)

    def _write(self, document: _Document) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "grants": [asdict(grant) for grant in document.grants],
            "requests": [asdict(item) for item in document.requests],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(dir=self.path.parent, prefix=".grants-", suffix=".json")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
