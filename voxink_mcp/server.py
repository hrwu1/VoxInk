"""JSON-RPC dispatch for the VoxInk MCP server."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, TextIO

from voxink_cli.client import GRANT_TOKEN_FILE_ENV, ControlClient, RequestContext
from voxink_cli.envelope import ErrorCode, Failure


PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "voxink", "version": "0.1.0"}
MAX_WAIT_SECONDS = 30.0
POLL_SECONDS = 0.5


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool("voxink_status", "Readiness of the local VoxInk helper and the state of live capture.", {}),
    _tool("voxink_capabilities", "What this VoxInk can do and what the current grant permits.", {}),
    _tool(
        "voxink_capture_start",
        "Start a live capture session. Needs a grant with the matching capture.* scope; "
        "without one the result names the pending approval request.",
        {
            "sources": {"type": "array", "items": {"type": "string", "enum": ["microphone", "system", "applications"]}},
            "applications": {"type": "array", "items": {"type": "string"}, "description": "bundle identifiers for the applications source"},
            "retain_audio": {"type": "boolean", "default": False},
            "language": {"type": "string", "enum": ["en", "zh"]},
            "translate": {"type": "boolean", "default": False},
        },
        ["sources"],
    ),
    _tool("voxink_capture_pause", "Pause the running capture.", {}),
    _tool("voxink_capture_resume", "Resume a paused capture.", {}),
    _tool("voxink_capture_stop", "Stop the running capture and finalize the transcript.", {}),
    _tool("voxink_capture_mark", "Drop a timestamped marker into the transcript.", {"label": {"type": "string"}}),
    _tool(
        "voxink_capture_events",
        "Read transcript events after a cursor. Waits up to wait_seconds for new events so an "
        "agent can follow a meeting as it happens; returns next_cursor to pass back.",
        {
            "after": {"type": "integer", "minimum": 0, "default": 0},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 10},
        },
    ),
    _tool("voxink_transcripts_list", "List saved transcript sessions.", {}),
    _tool(
        "voxink_transcript_show",
        "Read one session's transcript Markdown.",
        {"session_id": {"type": "string"}, "part": {"type": "integer", "minimum": 1}},
        ["session_id"],
    ),
    _tool(
        "voxink_permissions_request",
        "Ask the owner for a scoped grant. Returns a request id and claim secret; the owner approves "
        "in VoxInk, then voxink_permissions_claim picks up the token.",
        {
            "scopes": {"type": "array", "items": {"type": "string"}},
            "applications": {"type": "array", "items": {"type": "string"}},
            "max_minutes": {"type": "integer", "minimum": 1},
            "retain_audio": {"type": "boolean", "default": False},
            "reason": {"type": "string"},
        },
        ["scopes"],
    ),
    _tool(
        "voxink_permissions_claim",
        "Claim an approved request's token and use it for later calls in this session.",
        {"request_id": {"type": "string"}, "claim_secret": {"type": "string"}, "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 0}},
        ["request_id", "claim_secret"],
    ),
]


# The scopes each tool needs, so a refusal can name them and ask for exactly
# those. The consent tools are absent on purpose: they are how an agent holding
# nothing gets a credential, so they must never trigger a request of their own.
TOOL_SCOPES: dict[str, tuple[str, ...]] = {
    "voxink_status": ("sessions.read",),
    "voxink_capture_start": ("capture.microphone", "capture.system", "capture.applications"),
    "voxink_capture_pause": ("capture.microphone", "capture.system", "capture.applications"),
    "voxink_capture_resume": ("capture.microphone", "capture.system", "capture.applications"),
    "voxink_capture_stop": ("capture.microphone", "capture.system", "capture.applications"),
    "voxink_capture_mark": ("capture.microphone", "capture.system", "capture.applications"),
    "voxink_capture_events": ("sessions.read",),
    "voxink_transcripts_list": ("transcripts.read",),
    "voxink_transcript_show": ("transcripts.read",),
}

# Starting a capture should ask only for the sources it was actually given.
_SOURCE_SCOPES = {
    "microphone": "capture.microphone",
    "system": "capture.system",
    "applications": "capture.applications",
}

_PERMISSION_CODES = {ErrorCode.PERMISSION_DENIED, ErrorCode.APPROVAL_REQUIRED}


class Session:
    """One MCP session: a client, the grant it acts under, and its tools."""

    def __init__(
        self,
        *,
        token: str | None,
        owner: bool,
        client_name: str,
        client_factory: Callable[[RequestContext, str | None], ControlClient] | None = None,
    ) -> None:
        self.token = token
        self.owner = owner
        self.client_name = client_name
        self._client_factory = client_factory or _connect

    def client(self) -> ControlClient:
        context = RequestContext.create()
        client = self._client_factory(context, self.token)
        if not self.owner and self.token is None:
            # No grant: only the consent flow and public discovery may pass.
            client = replace_credential(client)
        return client

    # -- tools ----------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"tool_{name.removeprefix('voxink_')}", None)
        if handler is None:
            raise Failure(ErrorCode.INVALID_REQUEST, f"unknown tool: {name}")
        try:
            return handler(**arguments)
        except Failure as failure:
            raise self._as_consent_request(name, arguments, failure) from None

    def _as_consent_request(self, name: str, arguments: dict[str, Any], failure: Failure) -> Failure:
        """Turn a refusal this session cannot act on into one it can.

        The service files a pending request for a caller that holds a grant but
        lacks a scope. A caller holding nothing never reaches that code — it is
        refused while being authenticated — so without this it would receive a
        dead end where the instructions promise `approval_required`. Filing the
        request here keeps that promise, and the claim secret goes only to this
        caller.
        """
        if failure.code not in _PERMISSION_CODES or failure.details.get("request_id"):
            return failure
        scopes = self._scopes_for(name, arguments)
        if not scopes:
            return failure
        try:
            response = self.client().request_grant(
                client=self.client_name,
                reason=f"needed by {name}",
                scopes=list(scopes),
                applications=list(arguments.get("applications") or []),
                max_duration_s=None,
                retain_audio=bool(arguments.get("retain_audio")),
            )
        except Failure:
            # The consent flow itself is unreachable; the original refusal is
            # still the truest thing we can say.
            return failure
        request = response.get("request", {})
        return Failure(
            ErrorCode.APPROVAL_REQUIRED,
            f"{name} needs the owner's approval: {', '.join(scopes)}",
            details={
                "missing_scopes": list(scopes),
                "request_id": request.get("id"),
                "claim_secret": response.get("claim_secret"),
                "next": (
                    "Ask the user to approve this request in VoxInk's Agent access tab, "
                    "then call voxink_permissions_claim with the request_id and claim_secret."
                ),
            },
        )

    def _scopes_for(self, name: str, arguments: dict[str, Any]) -> tuple[str, ...]:
        if name == "voxink_capture_start":
            sources = arguments.get("sources") or []
            asked = tuple(dict.fromkeys(_SOURCE_SCOPES[s] for s in sources if s in _SOURCE_SCOPES))
            if asked:
                return asked
        return TOOL_SCOPES.get(name, ())

    def tool_status(self) -> dict[str, Any]:
        client = self.client()
        health = client.health()
        capture = client.capture_status()
        return {
            "ready": health.get("status") == "ok",
            "model": health.get("model"),
            "capture_state": capture.get("state"),
            "session_id": capture.get("session_id"),
            "credential": "owner" if self.owner else ("grant" if self.token else "none"),
        }

    def tool_capabilities(self) -> dict[str, Any]:
        client = self.client()
        health = client.health()
        capture = client.capture_capabilities()
        # What this session may do comes from the service, not from the catalog
        # of what a grant could carry: reporting the catalog beside a credential
        # of "none" reads as permission the agent does not have.
        standing = client.grant_self()
        return {
            "protocol_version": health.get("protocol_version"),
            "capture_modes": capture.get("modes", []),
            "partial_captions": capture.get("partial_captions"),
            "translation_available": capture.get("translation_available"),
            "credential": standing.get("credential", "none"),
            "permitted_scopes": standing.get("scopes", []),
            "requestable_scopes": standing.get("requestable_scopes", []),
            "grant_expires_at": standing.get("expires_at"),
        }

    def tool_capture_start(
        self,
        sources: list[str],
        applications: list[str] | None = None,
        retain_audio: bool = False,
        language: str | None = None,
        translate: bool = False,
    ) -> dict[str, Any]:
        applications = list(applications or [])
        if "applications" in sources and not applications:
            raise Failure(ErrorCode.INVALID_REQUEST, "the applications source needs bundle identifiers")
        started = self.client().start_capture(
            sources=["system" if source == "applications" else source for source in dict.fromkeys(sources)],
            applications=applications,
            retain_audio=retain_audio,
            language=language,
            translate=translate,
        )
        return {
            "session_id": started.get("session_id"),
            "state": started.get("state"),
            "next_cursor": started.get("next_cursor", 0),
        }

    def tool_capture_pause(self) -> dict[str, Any]:
        return _capture_view(self.client().control_capture("pause"))

    def tool_capture_resume(self) -> dict[str, Any]:
        return _capture_view(self.client().control_capture("resume"))

    def tool_capture_stop(self) -> dict[str, Any]:
        return _capture_view(self.client().control_capture("stop"))

    def tool_capture_mark(self, label: str = "") -> dict[str, Any]:
        return self.client().mark_capture(label)

    def tool_capture_events(self, after: int = 0, wait_seconds: float = 10.0) -> dict[str, Any]:
        client = self.client()
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
        while True:
            view = client.capture_status(after=after)
            events = view.get("events", [])
            state = view.get("state")
            if events or state in {"ready", "failed"} or time.monotonic() >= deadline:
                return {
                    "state": state,
                    "session_id": view.get("session_id"),
                    "events": events,
                    "next_cursor": view.get("next_cursor", after),
                    "error": view.get("error"),
                }
            time.sleep(POLL_SECONDS)

    def tool_transcripts_list(self) -> dict[str, Any]:
        response = self.client().transcripts()
        return {
            "output_root": response.get("output_root"),
            "transcripts": [
                {
                    "session_id": item.get("session_id"),
                    "parts": item.get("part_count"),
                    "size_bytes": item.get("size_bytes"),
                    "interrupted": item.get("interrupted"),
                }
                for item in response.get("transcripts", [])
            ],
        }

    def tool_transcript_show(self, session_id: str, part: int | None = None) -> dict[str, Any]:
        transcript = self.client().transcript(session_id, part=part)
        return {"session_id": session_id, "part": part, "text": transcript.get("text", "")}

    def tool_permissions_request(
        self,
        scopes: list[str],
        applications: list[str] | None = None,
        max_minutes: int | None = None,
        retain_audio: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        response = self.client().request_grant(
            client=self.client_name,
            reason=reason,
            scopes=list(dict.fromkeys(scopes)),
            applications=list(applications or []),
            max_duration_s=max_minutes * 60 if max_minutes else None,
            retain_audio=retain_audio,
        )
        request = response.get("request", {})
        return {
            "request_id": request.get("id"),
            "claim_secret": response.get("claim_secret"),
            "state": request.get("state"),
            "next": "Ask the user to approve this request in VoxInk, then call voxink_permissions_claim.",
        }

    def tool_permissions_claim(self, request_id: str, claim_secret: str, wait_seconds: float = 0.0) -> dict[str, Any]:
        client = self.client()
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
        while True:
            response = client.claim_grant(request_id, claim_secret)
            token = response.get("token")
            if token:
                self.token = token
                self.owner = False
                _persist_token(token)
                return {"claimed": True, "grant_id": response.get("request", {}).get("grant_id")}
            if time.monotonic() >= deadline:
                return {"claimed": False, "state": response.get("request", {}).get("state")}
            time.sleep(1.0)


def replace_credential(client: ControlClient) -> ControlClient:
    """Strip the owner credential so an unauthorized agent cannot borrow it."""
    return ControlClient(replace(client.endpoint, auth_secret=None), client.context, token=None)


def _persist_token(token: str) -> None:
    """Keep a claimed token for the next run when a token file is configured."""
    path = os.environ.get(GRANT_TOKEN_FILE_ENV)
    if not path:
        return
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
        target.write_text(token + "\n", encoding="utf-8")
    except OSError:
        pass


def _connect(context: RequestContext, token: str | None) -> ControlClient:
    client = ControlClient.connect(context)
    if token is not None:
        client = ControlClient(client.endpoint, context, token=token)
    return client


def _capture_view(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": view.get("state"),
        "session_id": view.get("session_id"),
        "next_cursor": view.get("next_cursor", 0),
        "error": view.get("error"),
    }


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------


def serve(
    stdin: TextIO,
    stdout: TextIO,
    *,
    token_file: Path | None = None,
    owner: bool = False,
    client_name: str = "mcp-agent",
    client_factory: Callable[[RequestContext, str | None], ControlClient] | None = None,
) -> int:
    token = _read_token(token_file)
    session = Session(token=token, owner=owner, client_name=client_name, client_factory=client_factory)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        response = handle(session, message)
        if response is not None:
            _write(stdout, response)
    return 0


def handle(session: Session, message: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; notifications get no answer."""
    method = message.get("method")
    identifier = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return _result(identifier, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "VoxInk transcribes locally. Every tool except voxink_capabilities and the "
                "consent tools needs a grant the user approves in VoxInk's Agent access tab. "
                "A refused call returns approval_required together with the request_id and "
                "claim_secret of a request already filed for you: ask the user to approve it, "
                "then call voxink_permissions_claim with those two values. Call "
                "voxink_capabilities first to see permitted_scopes for this session."
            ),
        })
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return _result(identifier, {})
    if method == "tools/list":
        return _result(identifier, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            payload = session.call(name, arguments)
        except Failure as failure:
            body = {
                "ok": False,
                "error": {
                    "code": failure.code,
                    "message": failure.message,
                    "retryable": failure.retryable,
                    "details": dict(failure.details),
                },
            }
            return _result(identifier, {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": True})
        except TypeError as exc:
            return _error(identifier, -32602, f"invalid arguments: {exc}")
        return _result(identifier, {"content": [{"type": "text", "text": json.dumps({"ok": True, "data": payload}, ensure_ascii=False)}]})
    if identifier is None:
        return None
    return _error(identifier, -32601, f"method not found: {method}")


def _read_token(token_file: Path | None) -> str | None:
    source = token_file or (Path(os.environ[GRANT_TOKEN_FILE_ENV]).expanduser() if os.environ.get(GRANT_TOKEN_FILE_ENV) else None)
    if source is None:
        return None
    try:
        return source.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _result(identifier: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _write(stdout: TextIO, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    stdout.flush()
