"""The public `voxink` command (`PROJECT_SPEC.md` §14).

Every command produces the same versioned envelope, so a script or an agent can
depend on `ok`, `command`, `data`, and `error.code` instead of parsing prose.
The human `text` format is a rendering of that same data and is never the only
way to read a result.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, TextIO

from .client import DEFAULT_TIMEOUT, GRANT_TOKEN_FILE_ENV, ControlClient, RequestContext
from .shell import StartDefaults, run_shell
from .envelope import (
    EXIT_OK,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    ErrorCode,
    Failure,
    dumps,
    failure,
    success,
)

# Scopes the application issues grants for (§14.5). The CLI never mints a
# grant: it asks, the owner approves in the UI, and the CLI claims the token.
from voxink_core.grants import GRANT_SCOPES  # noqa: E402

# Command groups §14.3 defines but the first slice does not implement.
DEFERRED_COMMAND_GROUPS = ("audio", "config", "diagnostics")

WATCH_INTERVAL_SECONDS = 0.5
CAPTURE_SETTLED_STATES = frozenset({"ready", "failed"})


@dataclass(frozen=True, slots=True)
class Result:
    data: Any
    warnings: tuple[str, ...] = ()
    text: str = ""


Handler = Callable[[argparse.Namespace, ControlClient], Result]


# ---------------------------------------------------------------------------
# Readiness and capability discovery
# ---------------------------------------------------------------------------


def _capabilities(args: argparse.Namespace, client: ControlClient) -> Result:
    health = client.health()
    capture = client.capture_capabilities()
    native = bool(capture.get("native_capture_available"))
    diarization = client.diarization_capability()
    translation = bool(capture.get("translation_available"))
    data = {
        "protocol_version": health.get("protocol_version"),
        "schema_version": SCHEMA_VERSION,
        "supported_schema_versions": list(SUPPORTED_SCHEMA_VERSIONS),
        "service_version": health.get("service_version"),
        "privacy_mode": health.get("privacy_mode"),
        "bind_policy": health.get("bind_policy"),
        "provider": health.get("provider"),
        "model": health.get("model"),
        "capture_modes": capture.get("modes", []),
        "features": {
            "capture.microphone": {"supported": True, "available": native, "permitted": native},
            "capture.system": {"supported": True, "available": native, "permitted": native},
            "capture.applications": {"supported": True, "available": native, "permitted": native},
            "sessions.read": {"supported": True, "available": True, "permitted": True},
            "transcripts.read": {"supported": True, "available": True, "permitted": True},
            "models.switch": {"supported": True, "available": True, "permitted": True},
            "vocabulary.write": {"supported": True, "available": True, "permitted": True},
            # Supported by this build; available only once the local weights
            # are installed, which is a separate, licence-gated download.
            "diarization.speakers": {
                "supported": True,
                "available": bool(diarization.get("available")),
                "permitted": bool(diarization.get("enabled")),
            },
            "captions.partial": {
                "supported": True,
                "available": bool(capture.get("partial_captions", False)),
                "permitted": True,
            },
            "captions.translation": {
                "supported": True,
                "available": translation,
                "permitted": translation,
            },
            "capture.channels": {"supported": True, "available": native, "permitted": native},
            "capture.echo_suppression": {
                "supported": True,
                "available": bool(capture.get("echo_suppression", False)),
                "permitted": True,
            },
            "durable_session_registry": {
                "supported": False,
                "available": False,
                "permitted": False,
            },
        },
        # `grant_scopes` is the §14.4 name for the catalog of scopes a grant may
        # carry. It says nothing about this caller, which read as permission the
        # caller did not hold, so `permitted_scopes` below now answers that. The
        # old name stays: schema 1 may gain fields but not lose them.
        "grant_scopes": list(GRANT_SCOPES),
        "requestable_scopes": list(GRANT_SCOPES),
        "grant_enforcement": "grant-token" if client.uses_grant else "developer-credential",
        "deferred_command_groups": list(DEFERRED_COMMAND_GROUPS),
    }
    standing = client.grant_self()
    data["credential"] = standing.get("credential", "none")
    data["permitted_scopes"] = list(standing.get("scopes", []))
    if data["credential"] != "owner":
        # Only the owner credential authorizes every scope. For anyone else —
        # a grant, or a caller holding nothing at all — "permitted" means what
        # the credential says, not what the helper could do for its owner.
        granted = set(data["permitted_scopes"])
        for feature in data["features"].values():
            feature["permitted"] = feature["permitted"] and bool(granted)
        for scope in GRANT_SCOPES:
            if scope in data["features"]:
                data["features"][scope]["permitted"] = scope in granted
    lines = [
        f"protocol {data['protocol_version']}, schema {SCHEMA_VERSION}, "
        f"service {data['service_version']}",
        f"provider {data['provider']} ({data['model']}), {data['privacy_mode']}, "
        f"{data['bind_policy']}",
        "capture modes: " + (", ".join(data["capture_modes"]) or "none"),
        f"grant enforcement: {data['grant_enforcement']}",
        f"credential: {data['credential']}; permitted scopes: "
        + (", ".join(data["permitted_scopes"]) or "none"),
    ]
    warnings = []
    if data["credential"] == "owner":
        warnings.append(
            "This command holds the owner credential, which authorizes every scope. "
            f"Agents should run under a grant token ({GRANT_TOKEN_FILE_ENV})."
        )
    elif data["credential"] == "none":
        warnings.append(
            "This command holds no credential, so no scope is permitted. Ask for a grant "
            "with: voxink permissions request --scope SCOPE --client NAME"
        )
    if not native:
        warnings.append(
            "Native capture is unavailable, so no capture mode can start. "
            "Build it with: ./scripts/build-macos-capture"
        )
    if diarization.get("enabled") and not diarization.get("available"):
        warnings.append(
            "Speaker diarization is supported but its local weights are not "
            "installed. See: voxink diarization capability"
        )
    return Result(data, tuple(warnings), "\n".join(lines))


def _status(args: argparse.Namespace, client: ControlClient) -> Result:
    health = client.health()
    capture = client.capture_status()
    data = {
        "ready": health.get("status") == "ok",
        "provider": health.get("provider"),
        "model": health.get("model"),
        "model_loaded": health.get("model_loaded"),
        "capture_state": capture.get("state"),
        "capture_session_id": capture.get("session_id"),
        "endpoint": client.endpoint.base_url,
        "protocol_version": health.get("protocol_version"),
    }
    loaded = "loaded" if data["model_loaded"] else "not loaded"
    return Result(
        data,
        text=(
            f"{'ready' if data['ready'] else 'not ready'} at {data['endpoint']}\n"
            f"model {data['model']} ({loaded}) via {data['provider']}\n"
            f"capture {data['capture_state']}"
            + (f" (session {data['capture_session_id']})" if data["capture_session_id"] else "")
        ),
    )


def _doctor(args: argparse.Namespace, client: ControlClient) -> Result:
    """Report local failures a person can act on, rather than only readiness."""
    checks: list[dict[str, Any]] = []
    health = client.health()
    checks.append(
        _check(
            "helper",
            health.get("status") == "ok",
            f"Helper answers at {client.endpoint.base_url}.",
            "The helper is reachable but unhealthy. Restart it: ./scripts/voxink-dev",
        )
    )
    capture = client.capture_capabilities()
    checks.append(
        _check(
            "native_capture",
            bool(capture.get("native_capture_available")),
            "Native capture is available.",
            "Native capture is unavailable, so only microphone capture can run. "
            "Build it with: ./scripts/build-macos-capture",
        )
    )
    models = client.models()
    checks.append(
        _check(
            "models",
            bool(models.get("models")),
            f"{len(models.get('models', []))} local model package(s) installed.",
            "No verified local model package was found under the models root.",
        )
    )
    checks.append(
        _check(
            "vocabulary",
            True,
            f"Vocabulary file: {client.vocabulary().get('path') or 'not configured'}",
            "",
        )
    )
    checks.append(
        _check(
            "session_registry",
            False,
            "",
            "The durable session registry is not built, so completed and "
            "interrupted sessions are discoverable only as transcript artifacts.",
        )
    )
    text = "\n".join(
        f"{'ok  ' if check['ok'] else 'warn'}  {check['name']}: {check['detail']}"
        for check in checks
    )
    attention = sum(1 for check in checks if not check["ok"])
    return Result({"checks": checks, "needs_attention": attention}, text=text)


def _check(name: str, ok: bool, detail: str, remedy: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail if ok else remedy}


# ---------------------------------------------------------------------------
# Capture input discovery
# ---------------------------------------------------------------------------


def _devices_list(args: argparse.Namespace, client: ControlClient) -> Result:
    response = client.devices()
    devices = response.get("devices", [])
    warnings = (
        ()
        if devices
        else ("No input devices were reported; the local audio backend may be unavailable.",)
    )
    text = "\n".join(
        f"{device.get('uid') or device.get('index', ''):<28}  {device['name']}"
        f"{'  (default)' if device.get('default') else ''}"
        for device in devices
    )
    return Result({"devices": devices, "source": response.get("source")}, warnings, text)


def _applications_list(args: argparse.Namespace, client: ControlClient) -> Result:
    applications = client.applications().get("applications", [])
    text = "\n".join(
        f"{application['bundle_id']}\t{application['name']}" for application in applications
    )
    return Result({"applications": applications}, text=text)


# ---------------------------------------------------------------------------
# Capture lifecycle
# ---------------------------------------------------------------------------


def _capture_start(args: argparse.Namespace, client: ControlClient) -> Result:
    sources = list(dict.fromkeys(args.sources or ["system"]))
    applications = list(dict.fromkeys(args.applications or []))
    if "applications" in sources and not applications:
        raise Failure(
            ErrorCode.INVALID_REQUEST,
            "--source applications requires at least one --app BUNDLE_ID",
        )
    if applications and "applications" not in sources:
        raise Failure(ErrorCode.INVALID_REQUEST, "--app requires --source applications")
    if "system" in sources and "applications" in sources:
        raise Failure(
            ErrorCode.INVALID_REQUEST,
            "choose all system audio or selected applications, not both",
        )
    started = client.start_capture(
        sources=["system" if source == "applications" else source for source in sources],
        applications=applications,
        retain_audio=args.retain_audio,
        microphone_device=getattr(args, "device", None),
        language=getattr(args, "language", None),
        translate=bool(getattr(args, "translate", False)),
        backend=getattr(args, "backend", None),
    )
    data = {
        "session_id": started.get("session_id"),
        "state": started.get("state"),
        "sources": sources,
        "applications": applications,
        "retain_audio": args.retain_audio,
        "microphone_device": getattr(args, "device", None),
        "language": getattr(args, "language", None),
        "translate": bool(getattr(args, "translate", False)),
        "next_cursor": started.get("next_cursor", 0),
    }
    return Result(
        data,
        text=f"capture {data['state']} (session {data['session_id']})",
    )


def _capture_mark(args: argparse.Namespace, client: ControlClient) -> Result:
    marked = client.mark_capture(args.label or "")
    stamp = marked.get("at_ms", 0)
    return Result(
        marked,
        text=f"marked at {stamp // 60000:02d}:{stamp % 60000 // 1000:02d}"
        + (f": {marked['label']}" if marked.get("label") else ""),
    )


def _capture_status(args: argparse.Namespace, client: ControlClient) -> Result:
    return _capture_view(client.capture_status(after=args.after))


def _capture_control(action: str) -> Handler:
    def handler(args: argparse.Namespace, client: ControlClient) -> Result:
        view = client.control_capture(action)
        if action == "stop" and getattr(args, "wait", False):
            view = _wait_for_settled(client, args)
        return _capture_view(view, after=getattr(args, "after", 0))

    return handler


def _capture_view(view: dict[str, Any], *, after: int = 0) -> Result:
    """Render a capture view, showing only events at or past the caller's cursor.

    Cursors are absolute, so a caller that has already read up to `after`
    (the shell's caption follower, a script that ran `capture status`) is not
    shown the same captions again.
    """
    events = list(view.get("events", []))
    next_cursor = view.get("next_cursor", 0)
    first_index = next_cursor - len(events)
    events = events[max(0, after - first_index):]
    data = {
        "state": view.get("state"),
        "session_id": view.get("session_id"),
        "events": events,
        "next_cursor": next_cursor,
        "exit_code": view.get("exit_code"),
        "error": view.get("error"),
        "retention": view.get("retention", {}),
    }
    lines = [f"capture {data['state']}" + (f" (session {data['session_id']})" if data["session_id"] else "")]
    lines.extend(event_line(event) for event in data["events"])
    warnings: tuple[str, ...] = ()
    stopped = data["retention"].get("stopped_reason")
    if stopped:
        # A warning, not an error: capture continues, the recording does not.
        warnings = (
            "Audio is no longer being saved "
            + ("because the disk is nearly full." if stopped == "low_disk" else "at the session limit.")
            + " The transcript is still being written.",
        )
    # `error` carries the backend's last stderr line even after a clean exit,
    # where it is a summary; it is a failure only when the state says so.
    if data["error"] and data["state"] == "failed":
        lines.append(f"error: {data['error']}")
    return Result(data, warnings, text="\n".join(lines))


def _wait_for_settled(client: ControlClient, args: argparse.Namespace) -> dict[str, Any]:
    """Block until capture leaves its transitional state, or the timeout ends."""
    deadline = time.monotonic() + args.timeout
    view = client.capture_status()
    while view.get("state") not in CAPTURE_SETTLED_STATES:
        if time.monotonic() >= deadline:
            raise Failure(
                ErrorCode.TIMEOUT,
                f"Capture was still {view.get('state')} after {args.timeout:g}s.",
                details={"state": view.get("state")},
            )
        time.sleep(WATCH_INTERVAL_SECONDS)
        view = client.capture_status()
    return view


def _capture_watch(args: argparse.Namespace, client: ControlClient) -> Iterator[Result]:
    """Follow the capture event stream from a resumable cursor (§14.4).

    Cursors are absolute and monotonic, so a caller that reconnects with the
    last `next_cursor` it saw resumes without replaying or silently skipping.
    """
    cursor = args.after
    deadline = None if args.follow_forever else time.monotonic() + args.timeout
    while True:
        view = client.capture_status(after=cursor)
        cursor = view.get("next_cursor", cursor)
        for event in view.get("events", []):
            yield Result({"cursor": cursor, "event": event}, text=event_line(event))
        state = view.get("state")
        if state in CAPTURE_SETTLED_STATES:
            yield Result(
                {"cursor": cursor, "state": state, "error": view.get("error")},
                text=f"capture {state}",
            )
            return
        if deadline is not None and time.monotonic() >= deadline:
            raise Failure(
                ErrorCode.TIMEOUT,
                f"Capture did not finish within {args.timeout:g}s.",
                details={"state": state, "cursor": cursor},
            )
        time.sleep(WATCH_INTERVAL_SECONDS)


def _shell(args: argparse.Namespace, client: ControlClient) -> Result:
    """Run the interactive shell; every typed line goes through `main`."""
    passthrough: list[str] = []
    if args.endpoint_file is not None:
        passthrough += ["--endpoint-file", str(args.endpoint_file)]
    if args.token_file is not None:
        passthrough += ["--token-file", str(args.token_file)]
    if args.format != "text":
        passthrough += ["--format", args.format]
    if args.timeout != DEFAULT_TIMEOUT:
        passthrough += ["--timeout", str(args.timeout)]

    def connect() -> ControlClient:
        # A fresh request id per poll, as a one-shot call would carry.
        return ControlClient(
            client.endpoint, RequestContext.create(timeout=client.context.timeout), token=client.token
        )

    code = run_shell(
        lambda argv: main([*passthrough, *argv]),
        connect,
        event_line=event_line,
        defaults=StartDefaults(
            tuple(dict.fromkeys(args.sources or ["microphone"])),
            tuple(dict.fromkeys(args.applications)),
            args.retain_audio,
        ),
        stop_on_exit=args.stop_on_exit,
    )
    return Result({"exit_code": code})


def event_line(event: dict[str, Any]) -> str:
    """One human-readable line per capture event; the Terminal interface prints the same."""
    kind = event.get("type", "transcript")
    if kind == "translation":
        return f"    ({event.get('language', '')}) {event.get('text', '')}".rstrip()
    if kind == "mark":
        return f"[mark] {event.get('text', '')}".rstrip()
    if kind != "transcript":
        return f"[{kind}] {event.get('message', '')}".rstrip()
    label = event.get("channel_name") or event.get("channel") or event.get("source", "")
    suffix = "" if event.get("is_final", True) else " …"
    return f"[{label}] {event.get('text', '')}{suffix}".strip()


# ---------------------------------------------------------------------------
# Sessions and transcripts
# ---------------------------------------------------------------------------


def _sessions_list(args: argparse.Namespace, client: ControlClient) -> Result:
    response = client.sessions()
    sessions = response.get("sessions", [])
    warnings = ()
    if not response.get("durable_registry", False):
        warnings = (
            "Only sessions held open by the running helper are listed. Completed "
            "and interrupted sessions are discoverable with: voxink transcripts list",
        )
    text = "\n".join(
        f"{session['session_id']}\t{session['state']}\t{','.join(session.get('sources', []))}"
        for session in sessions
    )
    return Result({"sessions": sessions, "durable_registry": False}, warnings, text)


def _sessions_show(args: argparse.Namespace, client: ControlClient) -> Result:
    session = client.session(args.session_id)
    text = (
        f"{session['session_id']}\nstate: {session['state']}\n"
        f"sources: {', '.join(session.get('sources', []))}\n"
        f"applications: {', '.join(session.get('applications', [])) or 'none'}"
    )
    return Result(session, text=text)


_DIARIZATION_HINTS = {
    "disabled": "Diarization was not requested for this session.",
    "waiting_for_stop": "Diarization starts when the session stops.",
    "processing": "Speaker processing is running; the transcript is already complete.",
    "ready": "Speaker artifacts are ready.",
    "failed": "Speaker processing failed. The transcript is unaffected.",
    "stale": "The transcript changed after processing. Re-run with --regenerate.",
}


def _diarization_capability(args: argparse.Namespace, client: ControlClient) -> Result:
    capability = client.diarization_capability()
    lines = [
        f"enabled: {str(capability.get('enabled', False)).lower()}",
        f"available: {str(capability.get('available', False)).lower()}",
        f"device: {capability.get('device', 'unknown')}",
    ]
    if capability.get("model_id"):
        lines.append(f"model: {capability['model_id']}")
    warnings = ()
    error = capability.get("error")
    if error:
        warnings = (
            f"{error['code']}: {error['message']}",
        )
    return Result(capability, warnings, "\n".join(lines))


def _diarization_show(args: argparse.Namespace, client: ControlClient) -> Result:
    status = client.diarization(args.session_id)
    return Result(status, _diarization_warnings(status), _diarization_text(status))


def _diarization_start(args: argparse.Namespace, client: ControlClient) -> Result:
    status = client.start_diarization(args.session_id, regenerate=args.regenerate)
    if not args.wait:
        return Result(status, _diarization_warnings(status), _diarization_text(status))
    # Deliberately not --timeout: that bounds one request to the helper, while
    # this bounds a model run that legitimately takes minutes on CPU.
    deadline = time.monotonic() + args.wait_timeout
    warnings: tuple[str, ...] = ()
    while True:
        status = client.diarization(args.session_id)
        if status.get("state") != "processing":
            break
        if time.monotonic() >= deadline:
            warnings = (
                f"Still processing after {args.wait_timeout:g}s; "
                "it continues in the background.",
            )
            break
        time.sleep(1.0)
    return Result(
        status, warnings + _diarization_warnings(status), _diarization_text(status)
    )


def _diarization_text(status: dict[str, Any]) -> str:
    state = status.get("state", "unknown")
    lines = [f"{status.get('session_id', '')}", f"state: {state}"]
    if status.get("speaker_count"):
        lines.append(f"speakers: {status['speaker_count']}")
    if status.get("total_segments"):
        lines.append(
            f"labeled: {status.get('labeled_segments', 0)}/{status['total_segments']} segment(s)"
        )
    if status.get("split_segments"):
        lines.append(f"split between speakers: {status['split_segments']} segment(s)")
    # Artifact names come from the service so no client has to guess a filename.
    for name in status.get("artifacts", []):
        lines.append(f"artifact: {name}")
    hint = _DIARIZATION_HINTS.get(state)
    if hint:
        lines.append(hint)
    return "\n".join(line for line in lines if line)


def _diarization_warnings(status: dict[str, Any]) -> tuple[str, ...]:
    warnings = []
    for key, prefix in (("error", ""), ("alignment_error", "speaker splitting unavailable, ")):
        error = status.get(key)
        if error:
            warnings.append(f"{prefix}{error['code']}: {error['message']}")
    return tuple(warnings)


def _transcripts_list(args: argparse.Namespace, client: ControlClient) -> Result:
    response = client.transcripts()
    transcripts = response.get("transcripts", [])
    text = "\n".join(
        f"{item['session_id']}\t{item['part_count']} part(s)"
        f"\t{_human_bytes(item.get('total_bytes', item['size_bytes']))}"
        + (f" ({_human_bytes(item['audio_bytes'])} audio)" if item.get("audio_bytes") else "")
        + ("\tinterrupted" if item.get("interrupted") else "")
        for item in transcripts
    )
    retained = sum(item.get("total_bytes", item.get("size_bytes", 0)) for item in transcripts)
    return Result(
        {
            "output_root": response.get("output_root"),
            "transcripts": transcripts,
            "retained_bytes": retained,
        },
        text=text + (f"\n\n{_human_bytes(retained)} retained in total" if transcripts else ""),
    )


def _human_bytes(count: int) -> str:
    """Render a byte count at the scale a person reads it at."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _transcripts_show(args: argparse.Namespace, client: ControlClient) -> Result:
    transcript = client.transcript(args.session_id, part=args.part)
    return Result(transcript, text=transcript.get("text", ""))


def _transcripts_open(args: argparse.Namespace, client: ControlClient) -> Result:
    revealed = client.reveal(args.session_id)
    return Result(revealed, text=f"opened {revealed.get('directory', '')}")


# ---------------------------------------------------------------------------
# File transcription
# ---------------------------------------------------------------------------


def _transcribe(args: argparse.Namespace, client: ControlClient) -> Result:
    path = Path(args.path).expanduser()
    response = client.transcribe_file(
        path,
        session_id=args.session_id,
        source=args.source,
        language=args.language,
    )
    event = response.get("event", {})
    return Result(
        {"event": event, "file": str(path)},
        text=event.get("text", ""),
    )


# ---------------------------------------------------------------------------
# Models and vocabulary
# ---------------------------------------------------------------------------


def _models_list(args: argparse.Namespace, client: ControlClient) -> Result:
    response = client.models()
    models = response.get("models", [])
    text = "\n".join(
        f"{'*' if model.get('active') else ' '} {model['id']}\t{model.get('profile', '')}"
        f"\t{model.get('display_name', '')}"
        for model in models
    )
    return Result({"models": models, "active_model": response.get("active_model")}, text=text)


def _models_select(args: argparse.Namespace, client: ControlClient) -> Result:
    model = client.select_model(args.model)
    return Result(model, text=f"active model: {model['id']} ({model.get('display_name', '')})")


def _vocabulary_show(args: argparse.Namespace, client: ControlClient) -> Result:
    vocabulary = client.vocabulary()
    warnings = ()
    biased = vocabulary.get("biased_terms", 0)
    if biased < len(vocabulary.get("terms", [])):
        warnings = (
            f"Only the first {biased} term(s) fit the recognition context; "
            "the rest do not bias recognition.",
        )
    return Result(vocabulary, warnings, vocabulary.get("content", ""))


def _vocabulary_replace(args: argparse.Namespace, client: ControlClient) -> Result:
    if args.file == "-":
        if args.no_input:
            raise Failure(
                ErrorCode.INVALID_REQUEST,
                "--no-input forbids reading the vocabulary from stdin; pass --file PATH.",
            )
        content = sys.stdin.read()
    else:
        try:
            content = Path(args.file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise Failure(ErrorCode.NOT_FOUND, f"cannot read the vocabulary file: {exc}") from exc
    vocabulary = client.replace_vocabulary(content)
    return Result(
        vocabulary,
        text=(
            f"vocabulary replaced: {len(vocabulary.get('terms', []))} term(s), "
            f"{len(vocabulary.get('corrections', []))} correction(s)"
        ),
    )


# ---------------------------------------------------------------------------
# Speakers who keep their names
# ---------------------------------------------------------------------------


def _speakers_list(args: argparse.Namespace, client: ControlClient) -> Result:
    response = client.known_speakers()
    speakers = response.get("speakers", [])
    text = "\n".join(
        f"{item['id']}\t{item['name']}\t{item.get('samples', 1)} sample(s)\t{item.get('model_id', '')}"
        for item in speakers
    )
    return Result(
        {"speakers": speakers, "threshold": response.get("threshold"), "path": response.get("path")},
        text=text or "No remembered voices.",
    )


def _speakers_name(args: argparse.Namespace, client: ControlClient) -> Result:
    named = client.name_speaker(
        args.session_id, args.speaker_id, name=args.name, remember=args.remember
    )
    text = f"{args.speaker_id} is now {named.get('name') or 'unnamed'} in {args.session_id}"
    if named.get("registry_id"):
        text += f"; voice remembered as {named['registry_id']}"
    return Result(named, text=text)


def _speakers_rename(args: argparse.Namespace, client: ControlClient) -> Result:
    speaker = client.rename_known_speaker(args.registry_id, args.name).get("speaker", {})
    return Result({"speaker": speaker}, text=f"{speaker.get('id')} is now {speaker.get('name')}")


def _speakers_forget(args: argparse.Namespace, client: ControlClient) -> Result:
    speaker = client.forget_speaker(args.registry_id).get("speaker", {})
    return Result({"speaker": speaker}, text=f"forgot {speaker.get('name')} ({speaker.get('id')})")


# ---------------------------------------------------------------------------
# Permissions (§14.5): ask, claim, inspect. Never issue.
# ---------------------------------------------------------------------------


def _granted_scopes(client: ControlClient) -> list[str]:
    """Scopes the current credential carries, as the service reports them.

    This used to probe two scoped reads and infer the rest from their refusals,
    which could only ever learn about those two and filed no answer for the
    others. `/v1/grants/self` answers for every scope directly.
    """
    return list(client.grant_self().get("scopes", []))


def _permissions_scopes(args: argparse.Namespace, client: ControlClient) -> Result:
    scopes = client.grant_scopes().get("scopes", [])
    return Result({"scopes": scopes}, text="\n".join(scopes))


def _permissions_request(args: argparse.Namespace, client: ControlClient) -> Result:
    response = client.request_grant(
        client=args.client,
        reason=args.reason or "",
        scopes=list(dict.fromkeys(args.scopes)),
        applications=list(dict.fromkeys(args.applications or [])),
        max_duration_s=(args.max_minutes * 60) if args.max_minutes else None,
        retain_audio=args.retain_audio,
    )
    request = response.get("request", {})
    data = {
        "request_id": request.get("id"),
        "claim_secret": response.get("claim_secret"),
        "scopes": request.get("scopes", []),
        "applications": request.get("applications", []),
        "max_duration_s": request.get("max_duration_s"),
        "state": request.get("state"),
    }
    return Result(
        data,
        text=(
            f"request {data['request_id']} is waiting for the owner's approval in VoxInk.\n"
            f"claim it with: voxink permissions claim {data['request_id']} --claim-secret ... --out TOKEN_FILE"
        ),
    )


def _permissions_claim(args: argparse.Namespace, client: ControlClient) -> Result:
    deadline = time.monotonic() + (args.wait_timeout if args.wait else 0.0)
    while True:
        response = client.claim_grant(args.request_id, args.claim_secret)
        token = response.get("token")
        request = response.get("request", {})
        if token or not args.wait or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    if not token:
        raise Failure(
            ErrorCode.APPROVAL_REQUIRED,
            "The request has not been approved yet. Approve it in VoxInk, then claim again.",
            details={"request_id": args.request_id, "state": request.get("state")},
        )
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.touch(mode=0o600, exist_ok=True)
    out.chmod(0o600)
    out.write_text(token + "\n", encoding="utf-8")
    return Result(
        {"request_id": args.request_id, "grant_id": request.get("grant_id"), "token_file": str(out)},
        text=f"grant {request.get('grant_id')} claimed; token written to {out}\n"
        f"use it with: {GRANT_TOKEN_FILE_ENV}={out}",
    )


def _permissions_list(args: argparse.Namespace, client: ControlClient) -> Result:
    grants = client.grants().get("grants", [])
    text = "\n".join(
        f"{grant['id']}\t{'valid' if grant.get('valid') else 'inactive'}\t{grant.get('label', '')}"
        f"\t{','.join(grant.get('scopes', []))}"
        + (f"\tapps={','.join(grant['applications'])}" if grant.get("applications") else "")
        + (f"\tmax={grant['max_duration_s']}s" if grant.get("max_duration_s") else "")
        for grant in grants
    )
    return Result({"grants": grants}, text=text)


def _permissions_pending(args: argparse.Namespace, client: ControlClient) -> Result:
    requests = client.grant_requests().get("requests", [])
    text = "\n".join(
        f"{item['id']}\t{item.get('client', '')}\t{','.join(item.get('scopes', []))}\t{item.get('reason', '')}"
        for item in requests
    )
    return Result({"requests": requests}, text=text)


def _permissions_revoke(args: argparse.Namespace, client: ControlClient) -> Result:
    grant = client.revoke_grant(args.grant_id).get("grant", {})
    return Result({"grant": grant}, text=f"revoked {grant.get('id')}")


def _permissions_audit(args: argparse.Namespace, client: ControlClient) -> Result:
    entries = client.audit(limit=args.limit).get("entries", [])
    text = "\n".join(
        f"{entry.get('time', '')}\t{entry.get('operation', '')}\t{entry.get('client', '')}"
        f"\t{entry.get('result', '')}"
        + (f"\t{entry['session_id']}" if entry.get("session_id") else "")
        for entry in entries
    )
    return Result({"entries": entries}, text=text)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


GLOBAL_DEFAULTS: dict[str, Any] = {
    "format": "text",
    "schema_version": SCHEMA_VERSION,
    "timeout": DEFAULT_TIMEOUT,
    "request_id": None,
    "idempotency_key": None,
    "no_input": False,
    "endpoint_file": None,
    "token_file": None,
}


def _global_options() -> argparse.ArgumentParser:
    """Options §14.4 requires on every command, usable before or after it.

    Every option suppresses its default: the same parser is a parent of both the
    root and each subcommand, so a stored default would let the subcommand's
    parse overwrite a value the caller gave before the command name.
    """
    parser = argparse.ArgumentParser(add_help=False)
    suppress = argparse.SUPPRESS
    parser.add_argument(
        "--format",
        choices=("text", "json", "jsonl"),
        default=suppress,
        help="output format (default: text)",
    )
    parser.add_argument(
        "--schema-version",
        default=suppress,
        help=f"required output schema version (supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)})",
    )
    parser.add_argument(
        "--timeout", type=float, default=suppress, help="seconds to wait for the helper"
    )
    parser.add_argument(
        "--request-id", default=suppress, help="correlate this call in the audit log"
    )
    parser.add_argument(
        "--idempotency-key", default=suppress, help="deduplicate a retried mutating call"
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        default=suppress,
        help="never read from stdin or prompt",
    )
    parser.add_argument("--endpoint-file", type=Path, default=suppress, help=argparse.SUPPRESS)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=suppress,
        help=f"grant token file to act under (default: ${GRANT_TOKEN_FILE_ENV})",
    )
    return parser


def _apply_global_defaults(args: argparse.Namespace) -> argparse.Namespace:
    for name, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    return args


def build_parser() -> argparse.ArgumentParser:
    common = _global_options()
    parser = argparse.ArgumentParser(
        prog="voxink",
        description="Control the local VoxInk service (see PROJECT_SPEC.md section 14).",
        parents=[common],
    )
    groups = parser.add_subparsers(dest="group", required=True, metavar="COMMAND")

    def group(name: str, help_text: str) -> argparse.ArgumentParser:
        return groups.add_parser(name, help=help_text, parents=[common])

    def leaf(
        parent: argparse.ArgumentParser, name: str, help_text: str
    ) -> argparse.ArgumentParser:
        actions = getattr(parent, "_voxink_actions", None)
        if actions is None:
            actions = parent.add_subparsers(dest="action", required=True, metavar="ACTION")
            parent._voxink_actions = actions  # type: ignore[attr-defined]
        return actions.add_parser(name, help=help_text, parents=[common])

    group("capabilities", "Report supported, available, and permitted features.").set_defaults(
        handler=_capabilities, command="capabilities"
    )
    group("status", "Report local readiness.").set_defaults(handler=_status, command="status")
    group("doctor", "Report actionable local failures.").set_defaults(
        handler=_doctor, command="doctor"
    )

    devices = group("devices", "List usable microphone devices.")
    leaf(devices, "list", "List input devices.").set_defaults(
        handler=_devices_list, command="devices.list"
    )

    applications = group("applications", "List applications available for capture.")
    leaf(applications, "list", "List capturable applications.").set_defaults(
        handler=_applications_list, command="applications.list"
    )

    capture = group("capture", "Control live capture.")
    start = leaf(capture, "start", "Start a capture session.")
    start.add_argument(
        "--source",
        dest="sources",
        action="append",
        choices=("microphone", "system", "applications"),
        help="capture source; repeat to combine microphone with system or applications",
    )
    start.add_argument(
        "--app",
        dest="applications",
        action="append",
        default=[],
        metavar="BUNDLE_ID",
        help="application bundle identifier; repeat for multiple applications",
    )
    start.add_argument(
        "--retain-audio",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="save captured audio beside the transcript (default: enabled)",
    )
    start.add_argument(
        "--device", default=None, metavar="UID", help="microphone device UID from `devices list`"
    )
    start.add_argument(
        "--language", choices=("en", "zh"), default=None, help="fix the language instead of detecting it"
    )
    start.add_argument(
        "--translate", action="store_true", help="translate each caption into the other language"
    )
    start.add_argument(
        "--backend",
        choices=("auto", "tap", "screencapturekit"),
        default=None,
        help="system audio backend (default: auto, which prefers the process tap)",
    )
    start.set_defaults(handler=_capture_start, command="capture.start")

    mark = leaf(capture, "mark", "Drop a timestamped marker into the transcript.")
    mark.add_argument("label", nargs="?", default="", help="optional note for the marker")
    mark.set_defaults(handler=_capture_mark, command="capture.mark")

    status = leaf(capture, "status", "Report capture state and buffered events.")
    status.add_argument("--after", type=int, default=0, help="resume from this event cursor")
    status.set_defaults(handler=_capture_status, command="capture.status")

    watch = leaf(capture, "watch", "Stream capture events until the session ends.")
    watch.add_argument("--after", type=int, default=0, help="resume from this event cursor")
    watch.add_argument(
        "--follow-forever",
        action="store_true",
        help="ignore --timeout and follow until the session ends",
    )
    watch.set_defaults(handler=_capture_watch, command="capture.watch", streaming=True)

    for action, help_text in (
        ("pause", "Pause the running capture."),
        ("resume", "Resume the paused capture."),
        ("stop", "Stop the running capture."),
    ):
        subparser = leaf(capture, action, help_text)
        subparser.add_argument(
            "--after", type=int, default=0, help="show only events from this cursor on"
        )
        if action == "stop":
            subparser.add_argument(
                "--wait", action="store_true", help="wait until capture has fully stopped"
            )
        subparser.set_defaults(handler=_capture_control(action), command=f"capture.{action}")

    shell = group("shell", "Type voxink commands at a prompt, with live captions.")
    shell.add_argument(
        "--source",
        dest="sources",
        action="append",
        choices=("microphone", "system", "applications"),
        help="what a bare `start` records; repeat to combine (default: microphone)",
    )
    shell.add_argument(
        "--app", dest="applications", action="append", default=[], metavar="BUNDLE_ID",
        help="application a bare `start` captures; repeat for several",
    )
    shell.add_argument(
        "--retain-audio",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="whether a bare `start` keeps the audio (default: enabled)",
    )
    shell.add_argument(
        "--stop-on-exit",
        action="store_true",
        help="stop an open capture when the shell exits (voxink-dev sets this)",
    )
    shell.set_defaults(handler=_shell, command="shell")

    sessions = group("sessions", "Inspect capture sessions.")
    leaf(sessions, "list", "List sessions the helper holds open.").set_defaults(
        handler=_sessions_list, command="sessions.list"
    )
    show = leaf(sessions, "show", "Show one session.")
    show.add_argument("session_id")
    show.set_defaults(handler=_sessions_show, command="sessions.show")

    transcribe = group("transcribe", "Transcribe an existing local audio file.")
    transcribe.add_argument("path", help="path to a local audio file")
    transcribe.add_argument("--session-id", default=None, help="append to an open session")
    transcribe.add_argument(
        "--source", choices=("microphone", "system"), default="microphone"
    )
    transcribe.add_argument("--language", default=None, help="language hint, for example en or zh")
    transcribe.set_defaults(handler=_transcribe, command="transcribe")

    transcripts = group("transcripts", "Read transcript artifacts.")
    leaf(transcripts, "list", "List transcript artifacts.").set_defaults(
        handler=_transcripts_list, command="transcripts.list"
    )
    transcript_show = leaf(transcripts, "show", "Read one session's transcript.")
    transcript_show.add_argument("session_id")
    transcript_show.add_argument("--part", type=int, default=None, help="read one part only")
    transcript_show.set_defaults(handler=_transcripts_show, command="transcripts.show")
    transcript_open = leaf(transcripts, "open", "Reveal a session's folder in Finder.")
    transcript_open.add_argument(
        "session_id", nargs="?", default=None, help="session to open (default: the last one)"
    )
    transcript_open.set_defaults(handler=_transcripts_open, command="transcripts.open")

    diarization = group("diarization", "Inspect and run local speaker diarization.")
    leaf(
        diarization, "capability", "Report whether diarization can run here."
    ).set_defaults(handler=_diarization_capability, command="diarization.capability")
    diarization_show = leaf(diarization, "show", "Show one session's diarization state.")
    diarization_show.add_argument("session_id")
    diarization_show.set_defaults(handler=_diarization_show, command="diarization.show")
    diarization_run = leaf(diarization, "run", "Diarize a session that has stopped.")
    diarization_run.add_argument("session_id")
    diarization_run.add_argument(
        "--regenerate",
        action="store_true",
        help="re-run even when artifacts already exist, for example when stale",
    )
    diarization_run.add_argument(
        "--wait", action="store_true", help="wait until processing settles"
    )
    diarization_run.add_argument(
        "--wait-timeout",
        type=float,
        default=1800.0,
        help="seconds to wait with --wait (default: 1800; CPU runs are slow)",
    )
    diarization_run.set_defaults(handler=_diarization_start, command="diarization.run")

    models = group("models", "Inspect and select local models.")
    leaf(models, "list", "List installed local models.").set_defaults(
        handler=_models_list, command="models.list"
    )
    select = leaf(models, "select", "Make one installed model active.")
    select.add_argument("model")
    select.set_defaults(handler=_models_select, command="models.select")

    speakers = group("speakers", "Name speakers and manage remembered voices.")
    leaf(speakers, "list", "List voices remembered on this Mac.").set_defaults(
        handler=_speakers_list, command="speakers.list"
    )
    name = leaf(speakers, "name", "Name a speaker in a finished session.")
    name.add_argument("session_id")
    name.add_argument("speaker_id", help="anonymous id such as MIC_SPEAKER_00")
    name.add_argument("name", help="display name; an empty string clears it")
    name.add_argument(
        "--remember", action="store_true", help="store the voice so later sessions recognize it"
    )
    name.set_defaults(handler=_speakers_name, command="speakers.name")
    rename = leaf(speakers, "rename", "Rename a remembered voice.")
    rename.add_argument("registry_id")
    rename.add_argument("name")
    rename.set_defaults(handler=_speakers_rename, command="speakers.rename")
    forget = leaf(speakers, "forget", "Delete a remembered voice.")
    forget.add_argument("registry_id")
    forget.set_defaults(handler=_speakers_forget, command="speakers.forget")

    permissions = group("permissions", "Ask for, claim, and inspect agent grants.")
    leaf(permissions, "scopes", "List the scopes a grant can carry.").set_defaults(
        handler=_permissions_scopes, command="permissions.scopes"
    )
    request = leaf(permissions, "request", "Ask the owner for a scoped grant.")
    request.add_argument(
        "--scope", dest="scopes", action="append", required=True, choices=GRANT_SCOPES,
        help="scope to ask for; repeat for several",
    )
    request.add_argument(
        "--app", dest="applications", action="append", default=[], metavar="BUNDLE_ID",
        help="limit capture to this application; repeat for several",
    )
    request.add_argument("--max-minutes", type=int, default=None, help="longest recording to allow")
    request.add_argument("--retain-audio", action="store_true", help="ask to keep audio files")
    request.add_argument("--client", default="agent", help="who is asking, as shown to the owner")
    request.add_argument("--reason", default="", help="why, as shown to the owner")
    request.set_defaults(handler=_permissions_request, command="permissions.request")
    claim = leaf(permissions, "claim", "Claim an approved request's token.")
    claim.add_argument("request_id")
    claim.add_argument("--claim-secret", required=True, help="secret returned by `permissions request`")
    claim.add_argument("--out", required=True, help="file to write the token to (mode 0600)")
    claim.add_argument("--wait", action="store_true", help="poll until the owner decides")
    claim.add_argument("--wait-timeout", type=float, default=600.0, help="seconds to wait with --wait")
    claim.set_defaults(handler=_permissions_claim, command="permissions.claim")
    leaf(permissions, "list", "List issued grants (owner only).").set_defaults(
        handler=_permissions_list, command="permissions.list"
    )
    leaf(permissions, "pending", "List requests awaiting approval (owner only).").set_defaults(
        handler=_permissions_pending, command="permissions.pending"
    )
    revoke = leaf(permissions, "revoke", "Revoke a grant (owner only).")
    revoke.add_argument("grant_id")
    revoke.set_defaults(handler=_permissions_revoke, command="permissions.revoke")
    audit = leaf(permissions, "audit", "Read the audit trail (owner only).")
    audit.add_argument("--limit", type=int, default=100)
    audit.set_defaults(handler=_permissions_audit, command="permissions.audit")

    vocabulary = group("vocabulary", "Read and replace custom terms and corrections.")
    leaf(vocabulary, "show", "Show the vocabulary file.").set_defaults(
        handler=_vocabulary_show, command="vocabulary.show"
    )
    replace = leaf(vocabulary, "replace", "Replace the whole vocabulary file.")
    replace.add_argument(
        "--file", default="-", help="file to read, or - for stdin (default: stdin)"
    )
    replace.set_defaults(handler=_vocabulary_replace, command="vocabulary.replace")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already explained the problem; keep its exit code, which
        # matches the documented `invalid_request` code.
        return int(exc.code or 0)
    _apply_global_defaults(args)

    command = getattr(args, "command", args.group)
    if args.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        # Answer at the version this build speaks, never at the one it rejected.
        args.schema_version = SCHEMA_VERSION
        return _fail(
            command,
            Failure(
                ErrorCode.INVALID_REQUEST,
                f"unsupported schema version: {args.schema_version}",
                details={"supported": list(SUPPORTED_SCHEMA_VERSIONS)},
            ),
            args,
            out,
            err,
        )

    context = RequestContext.create(
        request_id=args.request_id,
        timeout=args.timeout,
        idempotency_key=args.idempotency_key,
    )
    try:
        client = ControlClient.connect(
            context, endpoint_path=args.endpoint_file, token_file=args.token_file
        )
        if getattr(args, "streaming", False):
            for result in args.handler(args, client):
                _emit(command, result, args, out, err)
        else:
            _emit(command, args.handler(args, client), args, out, err)
    except Failure as error:
        return _fail(command, error, args, out, err)
    except KeyboardInterrupt:
        return _fail(
            command, Failure(ErrorCode.TIMEOUT, "interrupted", request_id=context.request_id),
            args, out, err,
        )
    except Exception as exc:  # noqa: BLE001 - no traceback may reach a consumer
        return _fail(
            command,
            Failure(ErrorCode.INTERNAL_ERROR, str(exc), request_id=context.request_id),
            args,
            out,
            err,
        )
    return EXIT_OK


def _emit(
    command: str, result: Result, args: argparse.Namespace, out: TextIO, err: TextIO
) -> None:
    if args.format == "text":
        if result.text:
            print(result.text, file=out)
        for warning in result.warnings:
            print(f"warning: {warning}", file=err)
    else:
        print(
            dumps(
                success(
                    command,
                    result.data,
                    warnings=result.warnings,
                    schema_version=args.schema_version,
                )
            ),
            file=out,
        )
    out.flush()


def _fail(
    command: str,
    error: Failure,
    args: argparse.Namespace,
    out: TextIO,
    err: TextIO,
) -> int:
    if args.format == "text":
        print(f"voxink {command} failed [{error.code}]: {error.message}", file=err)
    else:
        print(dumps(failure(command, error, schema_version=args.schema_version)), file=out)
        out.flush()
    return error.exit_code
