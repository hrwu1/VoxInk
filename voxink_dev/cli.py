"""Command-line entry point for the VoxInk developer edition."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode
import webbrowser

from .preflight import (
    DEFAULT_MENUBAR_APP,
    DEFAULT_MODEL_PATH,
    DEFAULT_NATIVE_CAPTURE,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_STATE_ROOT,
    PreflightError,
    PreflightOptions,
    run_preflight,
)
from voxink_cli.discovery import endpoint_path
from voxink_core.session_ids import new_session_id
from .supervisor import HelperConfig, HelperSupervisor, SupervisorError
from .diagnostics import DiagnosticLog


@dataclass(frozen=True, slots=True)
class CapturePlan:
    modes: tuple[str, ...]
    session_sources: tuple[str, ...]
    applications: tuple[str, ...]

    @property
    def uses_native_capture(self) -> bool:
        return any(mode in {"system", "applications"} for mode in self.modes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument(
        "--source",
        dest="sources",
        choices=("microphone", "system", "applications"),
        action="append",
        help=(
            "capture source; repeat to combine microphone with system or applications "
            "(default: microphone)"
        ),
    )
    parser.add_argument(
        "--app",
        dest="applications",
        action="append",
        default=[],
        metavar="BUNDLE_ID",
        help="application bundle identifier to capture; repeat for multiple applications",
    )
    parser.add_argument("--native-capture", type=Path, default=DEFAULT_NATIVE_CAPTURE)
    parser.add_argument(
        "--list-apps",
        action="store_true",
        help="list applications available to native capture and exit",
    )
    parser.add_argument(
        "--retain-audio",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="save captured audio beside the transcript (default: enabled)",
    )
    parser.add_argument("--skip-checksums", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument(
        "--vocabulary",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "custom vocabulary file of names and terms to recognize "
            "(default: vocabulary.txt in the output directory)"
        ),
    )
    parser.add_argument("--session-id")
    parser.add_argument(
        "--translation-model",
        type=Path,
        default=None,
        metavar="PATH",
        help="local instruct model for caption translation (default: models/Qwen3-0.6B)",
    )
    parser.add_argument(
        "--partial-captions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show captions while a sentence is still being spoken (default: enabled)",
    )
    parser.add_argument(
        "--echo-suppression",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="drop microphone audio that is the speakers' echo of a system stream (default: enabled)",
    )
    interface = parser.add_mutually_exclusive_group()
    interface.add_argument(
        "--menubar",
        dest="interface",
        action="store_const",
        const="menubar",
        help=(
            "run the menu-bar application with floating captions and push to talk "
            "(default, unless capture sources are named)"
        ),
    )
    interface.add_argument(
        "--web",
        dest="interface",
        action="store_const",
        const="web",
        help="open the local browser control surface",
    )
    interface.add_argument(
        "--terminal",
        dest="interface",
        action="store_const",
        const="terminal",
        help="use the interactive Terminal microphone controls",
    )
    parser.add_argument("--menubar-app", type=Path, default=DEFAULT_MENUBAR_APP, help=argparse.SUPPRESS)
    return parser


def _resolve_interface(args: argparse.Namespace) -> str:
    """The interface to run when the command line does not name one.

    Naming capture sources selects the route those flags have always driven:
    the browser for the microphone alone, native capture for system or
    application audio. The menu-bar app takes its sources from its own Start
    items, so it would silently ignore them.
    """
    if args.interface is not None:
        return args.interface
    if args.sources or args.applications:
        return "web"
    return "menubar"


def _built_menubar_app(path: Path) -> Path | None:
    """The built menu-bar application, or None when it has not been built."""
    executable = path.expanduser().resolve()
    if executable.is_file() and executable.stat().st_mode & 0o111:
        return executable
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_apps:
        return _list_applications(args.native_capture)
    interface = _resolve_interface(args)
    menubar_app = _built_menubar_app(args.menubar_app) if interface == "menubar" else None
    if interface == "menubar" and menubar_app is None:
        expected = args.menubar_app.expanduser().resolve()
        if args.interface == "menubar":
            print(
                "VoxInk cannot start:\n"
                f"- The menu-bar application is not built: {expected}\n"
                "  Build it with: ./scripts/build-macos-menubar",
                file=sys.stderr,
            )
            return 2
        # The menu bar is only the default here, so the browser still gives a
        # working session on a checkout where the app has not been built.
        print(
            f"The menu-bar application is not built ({expected}); "
            "opening the browser instead.\n"
            "Build it with ./scripts/build-macos-menubar to start in the menu bar."
        )
        interface = "web"
    # The menu-bar app is a control surface over the helper, like the browser:
    # both need the native helper for capture, not Python capture.
    helper_capture = interface != "terminal"
    try:
        capture_plan = _build_capture_plan(args.sources, args.applications)
    except ValueError as exc:
        print(f"VoxInk cannot start:\n- {exc}", file=sys.stderr)
        return 2
    try:
        report = run_preflight(
            PreflightOptions(
                model_path=args.model,
                output_root=args.output,
                state_root=args.state_root,
                sources=capture_plan.modes,
                retain_audio=args.retain_audio,
                native_capture=(
                    args.native_capture if capture_plan.uses_native_capture else None
                ),
                verify_checksums=not args.skip_checksums,
                python_capture=(
                    not helper_capture and not capture_plan.uses_native_capture
                ),
            )
        )
    except PreflightError as exc:
        print(f"VoxInk cannot start:\n{exc}", file=sys.stderr)
        return 2
    print(
        f"Preflight passed: {report.checked_files} model files verified, "
        f"{report.free_bytes // (1024 * 1024)} MiB free."
    )
    if args.preflight_only:
        return 0
    session_id = args.session_id or new_session_id()
    diagnostics = DiagnosticLog(report.log_root)
    supervisor = HelperSupervisor(
        HelperConfig(
            report.model_path,
            report.output_root,
            device=args.device,
            vocabulary_path=args.vocabulary,
            translation_model_path=args.translation_model,
            partial_captions=args.partial_captions,
            echo_suppression=args.echo_suppression,
            native_capture_path=args.native_capture if helper_capture else None,
            # Publishing the endpoint is what lets `voxink` attach to this
            # helper; the launcher owns it because it owns the helper's life.
            control_endpoint_path=endpoint_path(args.state_root),
        ),
        diagnostics=diagnostics,
    )
    try:
        health = supervisor.start()
        print(
            f"Helper ready on a private loopback port "
            f"(protocol {health['protocol_version']}, provider {health['provider']})."
        )
        if menubar_app is not None:
            return _run_menubar(
                supervisor, diagnostics, report.output_root, menubar_app
            )
        if interface == "web" and not capture_plan.uses_native_capture:
            return _run_web_ui(
                supervisor, health, diagnostics, report.output_root, args.retain_audio
            )
        endpoint = f"ws://127.0.0.1:{supervisor.port}/v1/sessions/{session_id}/stream"
        if capture_plan.uses_native_capture:
            command = _native_capture_command(
                args.native_capture,
                endpoint,
                session_id,
                capture_plan,
                args.retain_audio,
            )
            print(
                "Starting native macOS capture. Approve Microphone and/or "
                "Screen & System Audio Recording access if prompted."
            )
        else:
            command = [
                sys.executable,
                "-m",
                "clients.python_capture",
                "--url",
                endpoint,
                "--session-id",
                session_id,
            ]
            if args.retain_audio:
                command.append("--retain-audio")
        return _run_capture_process(
            supervisor,
            command,
            diagnostics,
            report.output_root,
            session_id,
        )
    except (OSError, SupervisorError, subprocess.TimeoutExpired) as exc:
        diagnostics.emit("supervisor.failed", level="error", component="supervisor")
        print(f"VoxInk failed: {exc}", file=sys.stderr)
        return 1
    finally:
        supervisor.stop()
        diagnostics.close()


def _build_capture_plan(
    requested_sources: list[str] | None,
    requested_applications: list[str],
) -> CapturePlan:
    modes = tuple(dict.fromkeys(requested_sources or ["microphone"]))
    applications = tuple(dict.fromkeys(requested_applications))
    if "system" in modes and "applications" in modes:
        raise ValueError("choose all system audio or selected applications, not both")
    if "applications" in modes and not applications:
        raise ValueError("--source applications requires at least one --app BUNDLE_ID")
    if applications and "applications" not in modes:
        raise ValueError("--app requires --source applications")
    session_sources = tuple(
        dict.fromkeys("system" if mode == "applications" else mode for mode in modes)
    )
    return CapturePlan(modes, session_sources, applications)


def _native_capture_command(
    executable: Path,
    endpoint: str,
    session_id: str,
    plan: CapturePlan,
    retain_audio: bool = True,
) -> list[str]:
    command = [
        str(executable.expanduser().resolve()),
        "--endpoint",
        endpoint,
        "--session-id",
        session_id,
    ]
    if retain_audio:
        command.append("--retain-audio")
    if "microphone" in plan.modes:
        command.append("--mic")
    if "system" in plan.modes:
        command.append("--system-all")
    if "applications" in plan.modes:
        for bundle_identifier in plan.applications:
            command.extend(("--app", bundle_identifier))
    return command


def _list_applications(executable: Path) -> int:
    executable = executable.expanduser().resolve()
    if not executable.is_file() or not executable.stat().st_mode & 0o111:
        print(
            "VoxInk cannot list applications:\n"
            f"- Native capture executable is unavailable: {executable}\n"
            "  Build it with: ./scripts/build-macos-capture",
            file=sys.stderr,
        )
        return 2
    try:
        return subprocess.run([str(executable), "--list-apps"], check=False).returncode
    except OSError as exc:
        print(f"VoxInk cannot list applications: {exc}", file=sys.stderr)
        return 1


def _run_capture_process(
    supervisor: HelperSupervisor,
    command: list[str],
    diagnostics: DiagnosticLog,
    output_root: Path,
    session_id: str,
) -> int:
    capture = subprocess.Popen(command, env=supervisor.capture_environment())
    diagnostics.emit("capture.started", component="capture", pid=capture.pid)
    try:
        try:
            while capture.poll() is None:
                helper = supervisor.process
                if helper is None or helper.poll() is not None:
                    raise SupervisorError("transcription helper exited unexpectedly")
                time.sleep(0.1)
        except KeyboardInterrupt:
            capture.send_signal(signal.SIGINT)
            capture.wait(timeout=10)
    finally:
        if capture.poll() is None:
            capture.terminate()
            capture.wait(timeout=5)
        diagnostics.emit(
            "capture.stopped",
            component="capture",
            pid=capture.pid,
            return_code=capture.returncode,
        )
    transcripts = sorted((output_root / session_id).glob("transcript-part-*.md"))
    if transcripts:
        print(f"Transcript saved: {transcripts[-1]}")
    else:
        print("Session stopped before any transcript text was finalized.")
    return int(capture.returncode or 0)


def _run_menubar(
    supervisor: HelperSupervisor,
    diagnostics: DiagnosticLog,
    output_root: Path,
    executable: Path,
) -> int:
    """Run the menu-bar application until it quits; it drives the helper itself."""
    environment = supervisor.capture_environment()
    environment["VOXINK_SERVICE_URL"] = supervisor.base_url
    environment["VOXINK_OUTPUT_ROOT"] = str(output_root)
    application = subprocess.Popen([str(executable)], env=environment)
    diagnostics.emit("menubar.started", component="menubar", pid=application.pid)
    print("VoxInk is in the menu bar. Quit it from the menu, or press Ctrl-C here.")
    try:
        try:
            while application.poll() is None:
                helper = supervisor.process
                if helper is None or helper.poll() is not None:
                    raise SupervisorError("transcription helper exited unexpectedly")
                time.sleep(0.25)
        except KeyboardInterrupt:
            application.terminate()
            application.wait(timeout=10)
    finally:
        if application.poll() is None:
            application.kill()
            application.wait(timeout=5)
        diagnostics.emit(
            "menubar.stopped", component="menubar", pid=application.pid,
            return_code=application.returncode,
        )
    return int(application.returncode or 0)


def _run_web_ui(
    supervisor: HelperSupervisor,
    health: dict[str, object],
    diagnostics: DiagnosticLog,
    output_root: Path,
    retain_audio: bool = True,
) -> int:
    if not health.get("ui_available"):
        raise SupervisorError(
            "the local UI build is missing; run the frontend build:local script"
        )
    # The fragment never reaches the helper, and the recorder strips it from the
    # address bar as soon as it has read both values.
    fragment = urlencode(
        {"bootstrap": supervisor.ui_bootstrap_secret, "retain_audio": "1" if retain_audio else "0"}
    )
    # A fresh public URL reloads an existing tab instead of just changing its
    # fragment, and also bypasses HTML cached by earlier service versions.
    url = f"{supervisor.base_url}/ui/?launch={time.time_ns()}#{fragment}"
    if not webbrowser.open(url):
        raise SupervisorError("the browser could not be opened")
    diagnostics.emit("ui.opened", component="ui", port=supervisor.port)
    print("VoxInk is open in your browser. Press Ctrl-C here when you are finished.")
    print(f"Transcripts will be saved under: {output_root}")
    try:
        while True:
            helper = supervisor.process
            if helper is None or helper.poll() is not None:
                raise SupervisorError("transcription helper exited unexpectedly")
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("Stopping VoxInk and finalizing open transcript files…")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
