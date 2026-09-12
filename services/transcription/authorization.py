"""Authentication and operation permissions for the local control API.

The policy owns credential interpretation and consent refusals. Routes declare
required scopes or owner-only operations; capture responses redact transcript
content separately when their caller only holds a control permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import secrets

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from voxink_core.grants import Grant, GrantError, GrantStore, TOKEN_PREFIX


def authorized(header: str | None, secret: str | None) -> bool:
    """Compare bearer credentials without leaking timing or accepting empty secrets."""
    if secret is None:
        # No credential configured (direct debugging): everything is the owner,
        # except a caller presenting a grant token, who is held to its scopes.
        supplied = _bearer(header)
        return not (supplied and supplied.startswith(TOKEN_PREFIX))
    supplied = _bearer(header)
    return bool(supplied) and secrets.compare_digest(supplied, secret)


def _bearer(header: str | None) -> str | None:
    prefix = "Bearer "
    if not header or not header.startswith(prefix):
        return None
    return header[len(prefix) :] or None


# Paths anyone on loopback may call: readiness, the UI bootstrap, and the
# consent flow an agent uses before it has any credential at all.
_PUBLIC_PATHS = frozenset(
    {"/health", "/v1/ui/bootstrap", "/v1/ui/capture/capabilities", "/v1/grants/scopes", "/v1/grants/self"}
)
CAPTURE_SCOPES = ("capture.microphone", "capture.system", "capture.applications")


def _is_consent_path(method: str, path: str) -> bool:
    if method != "POST":
        return False
    if path == "/v1/grants/requests":
        return True
    return path.startswith("/v1/grants/requests/") and path.endswith("/claim")


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling: the owner (UI or developer credential), a grant, or nobody."""

    kind: str
    grant: Grant | None = None

    @classmethod
    def owner(cls) -> "Principal":
        return cls("owner")

    @classmethod
    def anonymous(cls) -> "Principal":
        return cls("anonymous")

    @classmethod
    def for_grant(cls, grant: Grant) -> "Principal":
        return cls("grant", grant)

    @property
    def name(self) -> str:
        if self.grant is not None:
            return f"grant:{self.grant.id}"
        return self.kind


def principal_for(request: Request) -> Principal:
    return getattr(request.state, "principal", None) or Principal.anonymous()


class AccessPolicy:
    def __init__(self, grants: GrantStore, auth_secret: str | None) -> None:
        self.grants = grants
        self.auth_secret = auth_secret

    async def authenticate(self, request: Request, call_next):
        path = request.url.path
        public_ui = path == "/ui" or path.startswith("/ui/")
        # Identify the caller before deciding whether this path needs one, so a
        # public route such as `/v1/grants/self` can still describe a credential
        # that was presented instead of reporting every caller as anonymous.
        header = request.headers.get("authorization")
        if authorized(header, self.auth_secret):
            request.state.principal = Principal.owner()
        else:
            grant = self.grants.authenticate(_bearer(header)) if _bearer(header) else None
            request.state.principal = (
                Principal.for_grant(grant) if grant is not None else Principal.anonymous()
            )
        if public_ui or path in _PUBLIC_PATHS or _is_consent_path(request.method, path):
            response = await call_next(request)
            if public_ui and (path in {"/ui", "/ui/"} or path.endswith(".html")):
                # The entry point must discover the current hashed assets after
                # an upgrade; the assets themselves can retain normal caching.
                response.headers["Cache-Control"] = "no-store"
            return response
        if request.state.principal.kind != "anonymous":
            return await call_next(request)
        # An agent that holds nothing is not stuck: the consent flow is open to
        # it, so name the way out rather than refusing in prose (§14.5).
        return JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "code": "permission_denied",
                    "message": (
                        "no valid local credential: this call needs the owner's approval"
                        if header
                        else "no local credential was presented"
                    ),
                    "remediation": (
                        "Ask the owner for a grant: POST /v1/grants/requests with the scopes you "
                        "need, have the owner approve it in VoxInk, then POST "
                        "/v1/grants/requests/{request_id}/claim. Both are open to callers holding "
                        "no credential."
                    ),
                    "consent": {
                        "request": "POST /v1/grants/requests",
                        "claim": "POST /v1/grants/requests/{request_id}/claim",
                        "scopes": "GET /v1/grants/scopes",
                        "self": "GET /v1/grants/self",
                    },
                }
            },
        )

    @staticmethod
    def may_read_transcripts(request: Request) -> bool:
        principal = principal_for(request)
        return principal.kind == "owner" or bool(
            principal.grant and principal.grant.allows("transcripts.read")
        )
    def require_owner(self, request: Request) -> Principal:
        principal = principal_for(request)
        if principal.kind != "owner":
            raise HTTPException(
                status_code=403,
                detail={"code": "permission_denied", "message": "only the application owner may do this"},
            )
        return principal

    def require(self, request: Request, scope: str) -> Principal:
        """Let the owner through; hold a grant to its scopes."""
        principal = principal_for(request)
        if principal.kind == "owner":
            return principal
        if principal.grant is not None and principal.grant.allows(scope):
            return principal
        raise self.approval_required(principal, [scope])

    def require_any(self, request: Request, scopes: tuple[str, ...]) -> Principal:
        principal = principal_for(request)
        if principal.kind == "owner":
            return principal
        if principal.grant is not None and any(principal.grant.allows(scope) for scope in scopes):
            return principal
        raise self.approval_required(principal, list(scopes))

    def require_capture(
        self, request: Request, sources: tuple[str, ...],
        applications: tuple[str, ...], *, retain_audio: bool = False,
    ) -> Principal:
        principal = principal_for(request)
        if principal.kind == "owner":
            return principal
        needed = capture_scopes(sources, applications, retain_audio=retain_audio)
        grant = principal.grant
        missing = [scope for scope in needed if grant is None or not grant.allows(scope)]
        if missing:
            raise self.approval_required(principal, missing, applications, retain_audio)
        if grant is not None and grant.applications and (
            not applications or any(app not in grant.applications for app in applications)
        ):
            raise HTTPException(status_code=403, detail={
                "code": "permission_denied",
                "message": "this grant may only capture: " + ", ".join(grant.applications),
            })
        return principal

    def approval_required(
        self,
        principal: Principal,
        missing: list[str],
        applications: tuple[str, ...] = (),
        retain_audio: bool = False,
    ) -> HTTPException:
        """Build the §14.5 refusal: a request the owner can approve, never a bypass.

        The pending request is created on the caller's behalf so the UI can show
        it right away; the claim secret goes back only to this caller.
        """
        client = principal.grant.label if principal.grant is not None else "agent"
        detail: dict[str, Any] = {
            "code": "approval_required",
            "message": "this needs the owner's approval: " + ", ".join(missing),
            "missing_scopes": missing,
        }
        try:
            pending, claim_secret = self.grants.request(
                client=client,
                reason="requested automatically by a call that lacked the scope",
                scopes=missing,
                applications=applications,
                max_duration_s=None,
                retain_audio=retain_audio,
            )
        except GrantError as exc:
            detail["message"] += f" (no request was filed: {exc})"
        else:
            detail["request_id"] = pending.id
            detail["claim_secret"] = claim_secret
        return HTTPException(status_code=403, detail=detail)



def capture_scopes(
    sources: tuple[str, ...], applications: tuple[str, ...], *, retain_audio: bool = False,
) -> list[str]:
    needed = []
    if "microphone" in sources:
        needed.append("capture.microphone")
    if "system" in sources:
        needed.append("capture.applications" if applications else "capture.system")
    if retain_audio:
        needed.append("audio.retain")
    return needed
