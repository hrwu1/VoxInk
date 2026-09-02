"""Typed client for VoxInk's local control API.

`PROJECT_SPEC.md` §14.7 requires the CLI and any future MCP or SDK adapter to
share this client rather than shelling out to `voxink` and parsing its output.
Every operation the public commands expose is a method here, and every
transport or service failure becomes a `Failure` carrying a public error code.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import mimetypes
from pathlib import Path
import secrets
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .discovery import ControlEndpoint, EndpointUnavailable, load
from .envelope import ErrorCode, Failure


DEFAULT_TIMEOUT = 30.0

# The service reports HTTP status; the public contract reports stable codes.
# Anything unmapped is an internal error rather than a silently invented code.
_STATUS_CODES: Mapping[int, str] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.PERMISSION_DENIED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.INVALID_REQUEST,
    422: ErrorCode.INVALID_REQUEST,
    503: ErrorCode.NOT_READY,
    507: ErrorCode.DISK_LOW,
}


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Per-invocation controls shared by every command (§14.4)."""

    request_id: str
    timeout: float = DEFAULT_TIMEOUT
    idempotency_key: str | None = None

    @classmethod
    def create(
        cls,
        *,
        request_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        idempotency_key: str | None = None,
    ) -> "RequestContext":
        return cls(
            request_id=request_id or f"req_{secrets.token_hex(8)}",
            timeout=timeout,
            idempotency_key=idempotency_key,
        )


class ControlClient:
    """A thin transport over the local control plane; it owns no product logic."""

    def __init__(self, endpoint: ControlEndpoint, context: RequestContext) -> None:
        self.endpoint = endpoint
        self.context = context

    @classmethod
    def connect(
        cls, context: RequestContext, *, endpoint_path: Path | None = None
    ) -> "ControlClient":
        try:
            endpoint = load(endpoint_path)
        except EndpointUnavailable as exc:
            raise Failure(ErrorCode.NOT_READY, str(exc), request_id=context.request_id) from exc
        return cls(endpoint, context)

    # -- Readiness and capability discovery -------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def capture_capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/v1/ui/capture/capabilities")

    def providers(self) -> dict[str, Any]:
        return self._request("GET", "/v1/providers")

    # -- Capture input discovery ------------------------------------------

    def devices(self) -> dict[str, Any]:
        return self._request("GET", "/v1/devices")

    def applications(self) -> dict[str, Any]:
        return self._request("GET", "/v1/ui/capture/applications")

    # -- Capture lifecycle -------------------------------------------------

    def start_capture(
        self,
        *,
        sources: list[str],
        applications: list[str],
        retain_audio: bool,
        microphone_device: str | None = None,
        language: str | None = None,
        translate: bool = False,
        backend: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sources": sources,
            "applications": applications,
            "retain_audio": retain_audio,
            "translate": translate,
        }
        if microphone_device:
            body["microphone_device"] = microphone_device
        if language:
            body["language"] = language
        if backend:
            body["backend"] = backend
        return self._request("POST", "/v1/ui/capture/start", body=body)

    def capture_status(self, *, after: int = 0) -> dict[str, Any]:
        return self._request("GET", "/v1/ui/capture/status", query={"after": after})

    def control_capture(self, action: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/ui/capture/{action}")

    def mark_capture(self, label: str = "") -> dict[str, Any]:
        return self._request("POST", "/v1/ui/capture/mark", body={"label": label})

    def reveal(self, session_id: str | None = None) -> dict[str, Any]:
        body = {"session_id": session_id} if session_id else {}
        return self._request("POST", "/v1/ui/actions/reveal", body=body)

    def translation_capability(self) -> dict[str, Any]:
        return self._request("GET", "/v1/translation")

    # -- Sessions and artifacts -------------------------------------------

    def sessions(self) -> dict[str, Any]:
        return self._request("GET", "/v1/sessions")

    def session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{_segment(session_id)}")

    # -- Speaker diarization -----------------------------------------------

    def diarization_capability(self) -> dict[str, Any]:
        return self._request("GET", "/v1/diarization")

    def diarization(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/sessions/{_segment(session_id)}/diarization"
        )

    def start_diarization(
        self, session_id: str, *, regenerate: bool = False
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{_segment(session_id)}/diarization",
            body={"regenerate": regenerate},
        )

    def transcripts(self) -> dict[str, Any]:
        return self._request("GET", "/v1/transcripts")

    def transcript(self, session_id: str, *, part: int | None = None) -> dict[str, Any]:
        query = {"part": part} if part is not None else None
        return self._request("GET", f"/v1/transcripts/{_segment(session_id)}", query=query)

    # -- File transcription ------------------------------------------------

    def transcribe_file(
        self, path: Path, *, session_id: str | None, source: str, language: str | None
    ) -> dict[str, Any]:
        fields = {"source": source}
        if session_id:
            fields["session_id"] = session_id
        if language:
            fields["language"] = language
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise Failure(
                ErrorCode.NOT_FOUND,
                f"The audio file cannot be read: {exc}",
                request_id=self.context.request_id,
            ) from exc
        if not payload:
            raise Failure(
                ErrorCode.INVALID_REQUEST,
                f"The audio file is empty: {path}",
                request_id=self.context.request_id,
            )
        body, content_type = _multipart(fields, path.name, payload)
        return self._request(
            "POST", "/v1/transcriptions", raw_body=body, content_type=content_type
        )

    # -- Models and vocabulary --------------------------------------------

    def models(self) -> dict[str, Any]:
        return self._request("GET", "/v1/models")

    def select_model(self, model_id: str) -> dict[str, Any]:
        return self._request("POST", "/v1/models/active", body={"model": model_id})

    def vocabulary(self) -> dict[str, Any]:
        return self._request("GET", "/v1/vocabulary")

    def replace_vocabulary(self, content: str) -> dict[str, Any]:
        return self._request("PUT", "/v1/vocabulary", body={"content": content})

    # -- Transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.endpoint.base_url}{path}"
        if query:
            url = f"{url}?{urlencode({k: v for k, v in query.items() if v is not None})}"
        headers = {"Accept": "application/json", "X-VoxInk-Request-Id": self.context.request_id}
        if self.endpoint.auth_secret:
            headers["Authorization"] = f"Bearer {self.endpoint.auth_secret}"
        if self.context.idempotency_key and method in {"POST", "PUT", "DELETE"}:
            headers["X-VoxInk-Idempotency-Key"] = self.context.idempotency_key
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type is not None:
            headers["Content-Type"] = content_type
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.context.timeout) as response:
                return self._decode(response.read())
        except HTTPError as exc:
            raise self._service_failure(exc) from exc
        except TimeoutError as exc:
            raise Failure(
                ErrorCode.TIMEOUT,
                f"The local helper did not answer within {self.context.timeout:g}s.",
                request_id=self.context.request_id,
            ) from exc
        except (URLError, OSError) as exc:
            raise Failure(
                ErrorCode.NOT_READY,
                f"The local helper is unreachable at {self.endpoint.base_url}: {exc}",
                request_id=self.context.request_id,
            ) from exc

    def _decode(self, payload: bytes) -> dict[str, Any]:
        if not payload:
            return {}
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise Failure(
                ErrorCode.INTERNAL_ERROR,
                "The local helper returned an unreadable response.",
                request_id=self.context.request_id,
            ) from exc
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    def _service_failure(self, error: HTTPError) -> Failure:
        try:
            detail = json.loads(error.read()).get("detail")
        except (OSError, ValueError, AttributeError):
            detail = None
        message = detail if isinstance(detail, str) and detail else error.reason or "request failed"
        code = _STATUS_CODES.get(error.code, ErrorCode.INTERNAL_ERROR)
        # A 409 may be a lifecycle rejection or a busy model; the service's own
        # wording is the only signal available until sessions carry revisions.
        if code == ErrorCode.CONFLICT:
            lowered = message.lower()
            if "model" in lowered:
                code = ErrorCode.MODEL_BUSY
            elif "cannot " in lowered or "session" in lowered:
                code = ErrorCode.INVALID_TRANSITION
        return Failure(
            code,
            str(message),
            details={"http_status": error.code},
            request_id=self.context.request_id,
        )


def _segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _multipart(
    fields: Mapping[str, str], filename: str, payload: bytes
) -> tuple[bytes, str]:
    boundary = f"voxink{secrets.token_hex(16)}"
    marker = f"--{boundary}".encode()
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            marker
            + b"\r\n"
            + f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    chunks.append(
        marker
        + b"\r\n"
        + f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        + f"Content-Type: {media_type}\r\n\r\n".encode()
        + payload
        + b"\r\n"
    )
    chunks.append(marker + b"--\r\n")
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
