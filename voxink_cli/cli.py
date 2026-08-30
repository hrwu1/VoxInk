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

from .client import DEFAULT_TIMEOUT, ControlClient, RequestContext
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

# Scopes the application will issue grants for (§14.5). The developer edition
# has no grant issuer yet, so possession of the loopback credential stands in
# for every scope; `capabilities` reports that honestly rather than implying
# grants are being enforced.
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

# Command groups §14.3 defines but the first slice does not implement.
DEFERRED_COMMAND_GROUPS = ("audio", "config", "diagnostics", "permissions")

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
            "capture.microphone": {"supported": True, "available": True, "permitted": True},
            "capture.system": {"supported": True, "available": native, "permitted": native},
            "capture.applications": {"supported": True, "available": native, "permitted": native},
            "sessions.read": {"supported": True, "available": True, "permitted": True},
            "transcripts.read": {"supported": True, "available": True, "permitted": True},
            "models.switch": {"supported": True, "available": True, "permitted": True},
            "vocabulary.write": {"supported": True, "available": True, "permitted": True},
            "durable_session_registry": {
                "supported": False,
                "available": False,
                "permitted": False,
            },
        },
        "grant_scopes": list(GRANT_SCOPES),
        "grant_enforcement": "developer-credential",
        "deferred_command_groups": list(DEFERRED_COMMAND_GROUPS),
    }
    lines = [
        f"protocol {data['protocol_version']}, schema {SCHEMA_VERSION}, "
        f"service {data['service_version']}",
        f"provider {data['provider']} ({data['model']}), {data['privacy_mode']}, "
        f"{data['bind_policy']}",
        "capture modes: " + (", ".join(data["capture_modes"]) or "none"),
    ]
    warnings = [
        "Scoped grants are not enforced yet; the loopback credential authorizes "
        "every scope in this edition."
    ]
    if not native:
        warnings.append(
            "Native capture is unavailable, so system and application capture "
            "cannot start. Build it with: ./scripts/build-macos-capture"
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
    devices = client.devices().get("devices", [])
    warnings = (
        ()
        if devices
        else ("No input devices were reported; the local audio backend may be unavailable.",)
    )
    text = "\n".join(
        f"{device['index']:>3}  {device['name']}"
        f"{'  (default)' if device.get('default') else ''}"
        for device in devices
    )
    return Result({"devices": devices}, warnings, text)


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
    if sources == ["microphone"]:
        # The control plane starts native capture only, and native capture
        # always carries a system stream. A microphone-only session is a real
        # capability gap, not a rejected request, so it names its own code.
        raise Failure(
            ErrorCode.CAPTURE_UNAVAILABLE,
            "Microphone-only capture is not available from the CLI yet; "
            "combine it with --source system, or use ./scripts/voxink-dev.",
        )
    started = client.start_capture(
        sources=["system" if source == "applications" else source for source in sources],
        applications=applications,
        retain_audio=args.retain_audio,
    )
    data = {
        "session_id": started.get("session_id"),
        "state": started.get("state"),
        "sources": sources,
        "applications": applications,
        "retain_audio": args.retain_audio,
        "next_cursor": started.get("next_cursor", 0),
    }
    return Result(
        data,
        text=f"capture {data['state']} (session {data['session_id']})",
    )


def _capture_status(args: argparse.Namespace, client: ControlClient) -> Result:
    return _capture_view(client.capture_status(after=args.after))


def _capture_control(action: str) -> Handler:
    def handler(args: argparse.Namespace, client: ControlClient) -> Result:
        view = client.control_capture(action)
        if action == "stop" and getattr(args, "wait", False):
            view = _wait_for_settled(client, args)
        return _capture_view(view)

    return handler


def _capture_view(view: dict[str, Any]) -> Result:
    data = {
        "state": view.get("state"),
        "session_id": view.get("session_id"),
        "events": view.get("events", []),
        "next_cursor": view.get("next_cursor", 0),
        "exit_code": view.get("exit_code"),
        "error": view.get("error"),
    }
    lines = [f"capture {data['state']}" + (f" (session {data['session_id']})" if data["session_id"] else "")]
    lines.extend(_event_line(event) for event in data["events"])
    if data["error"]:
        lines.append(f"error: {data['error']}")
    return Result(data, text="\n".join(lines))


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
            yield Result({"cursor": cursor, "event": event}, text=_event_line(event))
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


def _event_line(event: dict[str, Any]) -> str:
    if event.get("type") != "transcript":
        return f"[{event.get('type', 'event')}] {event.get('message', '')}".rstrip()
    source = event.get("source", "")
    return f"[{source}] {event.get('text', '')}".strip()


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


def _transcripts_list(args: argparse.Namespace, client: ControlClient) -> Result:
    response = client.transcripts()
    transcripts = response.get("transcripts", [])
    text = "\n".join(
        f"{item['session_id']}\t{item['part_count']} part(s)\t{item['size_bytes']} bytes"
        + ("\tinterrupted" if item.get("interrupted") else "")
        for item in transcripts
    )
    return Result(
        {"output_root": response.get("output_root"), "transcripts": transcripts}, text=text
    )


def _transcripts_show(args: argparse.Namespace, client: ControlClient) -> Result:
    transcript = client.transcript(args.session_id, part=args.part)
    return Result(transcript, text=transcript.get("text", ""))


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
    start.set_defaults(handler=_capture_start, command="capture.start")

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
        if action == "stop":
            subparser.add_argument(
                "--wait", action="store_true", help="wait until capture has fully stopped"
            )
        subparser.set_defaults(handler=_capture_control(action), command=f"capture.{action}")

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

    models = group("models", "Inspect and select local models.")
    leaf(models, "list", "List installed local models.").set_defaults(
        handler=_models_list, command="models.list"
    )
    select = leaf(models, "select", "Make one installed model active.")
    select.add_argument("model")
    select.set_defaults(handler=_models_select, command="models.select")

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
        client = ControlClient.connect(context, endpoint_path=args.endpoint_file)
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
