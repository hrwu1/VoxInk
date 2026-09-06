"""Own the private transcription helper and capture client processes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.error import URLError
from urllib.request import urlopen

from services.transcription.app import PROTOCOL_VERSION, SERVICE_VERSION
from voxink_cli import discovery
from .diagnostics import DiagnosticLog


PREFERRED_HELPER_PORT = 8300
HELPER_PORT_RANGE = range(8300, 8400)


class SupervisorError(RuntimeError):
    """A child process failed or did not satisfy the local protocol contract."""


@dataclass(frozen=True, slots=True)
class HelperConfig:
    model_path: Path
    output_root: Path
    device: str = "auto"
    readiness_timeout: float = 30.0
    service_module: str = "services.transcription"
    native_capture_path: Path | None = None
    vocabulary_path: Path | None = None
    # Where the public CLI looks for this helper; None keeps it undiscoverable.
    control_endpoint_path: Path | None = None
    # Optional local translation model; None leaves the service default.
    translation_model_path: Path | None = None
    partial_captions: bool = True
    echo_suppression: bool = True


class HelperSupervisor:
    def __init__(
        self,
        config: HelperConfig,
        *,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        diagnostics: DiagnosticLog | None = None,
    ) -> None:
        self.config = config
        self.process_factory = process_factory
        self.diagnostics = diagnostics
        self.auth_secret = secrets.token_urlsafe(32)
        self.ui_bootstrap_secret = secrets.token_urlsafe(24)
        self.port: int | None = None
        self.process: subprocess.Popen[str] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._stderr_reader: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise SupervisorError("helper has not been started")
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> dict[str, object]:
        if self.process is not None:
            raise SupervisorError("helper is already running")
        self.port = _preferred_loopback_port()
        self._stderr_lines.clear()
        environment = os.environ.copy()
        environment["VOXINK_AUTH_SECRET"] = self.auth_secret
        environment["VOXINK_UI_BOOTSTRAP_SECRET"] = self.ui_bootstrap_secret
        if self.config.native_capture_path is not None:
            environment["VOXINK_NATIVE_CAPTURE"] = str(
                self.config.native_capture_path.expanduser().resolve()
            )
        command = [
            sys.executable,
            "-m",
            self.config.service_module,
            "--port",
            str(self.port),
            "--model",
            str(self.config.model_path),
            "--output",
            str(self.config.output_root),
            "--device",
            self.config.device,
        ]
        if self.config.vocabulary_path is not None:
            command.extend(
                ("--vocabulary", str(self.config.vocabulary_path.expanduser().resolve()))
            )
        if self.config.translation_model_path is not None:
            command.extend(
                ("--translation-model", str(self.config.translation_model_path.expanduser().resolve()))
            )
        if not self.config.partial_captions:
            command.append("--no-partial-captions")
        if not self.config.echo_suppression:
            command.append("--no-echo-suppression")
        self.process = self.process_factory(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._start_stderr_reader()
        self._emit("helper.starting", component="helper", pid=self.process.pid, port=self.port)
        try:
            health = self.wait_until_ready()
            self._publish_control_endpoint()
            self._emit(
                "helper.ready",
                component="helper",
                pid=self.process.pid,
                port=self.port,
                protocol_version=health["protocol_version"],
            )
            return health
        except BaseException:
            self._emit("helper.start_failed", level="error", component="helper")
            self.stop()
            raise

    def wait_until_ready(self) -> dict[str, object]:
        deadline = time.monotonic() + self.config.readiness_timeout
        last_error = "helper did not answer"
        while time.monotonic() < deadline:
            process = self.process
            if process is None:
                raise SupervisorError("helper has not been started")
            if process.poll() is not None:
                detail = self._stderr_detail()
                raise SupervisorError(
                    f"transcription helper exited with status {process.returncode}"
                    + (f": {detail}" if detail else "")
                )
            try:
                with urlopen(f"{self.base_url}/health", timeout=0.5) as response:
                    health = json.load(response)
                if health.get("status") != "ok":
                    last_error = "helper reported an unhealthy state"
                elif health.get("protocol_version") != PROTOCOL_VERSION:
                    raise SupervisorError(
                        "local protocol mismatch: "
                        f"client={PROTOCOL_VERSION}, service={health.get('protocol_version')}"
                    )
                else:
                    return health
            except SupervisorError:
                raise
            except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            time.sleep(0.05)
        raise SupervisorError(
            f"transcription helper was not ready after {self.config.readiness_timeout:.1f}s: "
            f"{last_error}"
        )

    def capture_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["VOXINK_AUTH_SECRET"] = self.auth_secret
        return environment

    def stop(self, timeout: float = 10.0) -> None:
        self._revoke_control_endpoint()
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                self._emit("helper.stopping", component="helper", pid=process.pid)
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        finally:
            self._emit(
                "helper.stopped",
                component="helper",
                pid=process.pid,
                return_code=process.returncode,
            )
            if process.stderr is not None:
                process.stderr.close()
            reader, self._stderr_reader = self._stderr_reader, None
            if reader is not None:
                reader.join(timeout=1.0)

    def _publish_control_endpoint(self) -> None:
        """Let the public CLI find this helper (`PROJECT_SPEC.md` §14.2).

        The launcher owns the helper's lifetime, so it owns the handshake too.
        A failure here leaves capture working and only the CLI unable to attach,
        so it is reported rather than raised.
        """
        path = self.config.control_endpoint_path
        if path is None:
            return
        try:
            discovery.publish(
                path,
                base_url=self.base_url,
                auth_secret=self.auth_secret,
                protocol_version=str(PROTOCOL_VERSION),
                service_version=SERVICE_VERSION,
                pid=self.process.pid if self.process else None,
            )
        except OSError as exc:
            self._emit(
                "control_endpoint.publish_failed",
                level="error",
                component="supervisor",
                reason=str(exc),
            )
            return
        self._emit("control_endpoint.published", component="supervisor", port=self.port)

    def _revoke_control_endpoint(self) -> None:
        path = self.config.control_endpoint_path
        if path is None:
            return
        try:
            discovery.revoke(path)
        except OSError as exc:
            self._emit(
                "control_endpoint.revoke_failed",
                level="error",
                component="supervisor",
                reason=str(exc),
            )

    def _start_stderr_reader(self) -> None:
        """Drain the helper's stderr continuously.

        uvicorn logs every unhandled-endpoint traceback to stderr. Left unread,
        the pipe fills and the helper blocks inside its own event loop, so the
        whole service stops responding.
        """
        process = self.process
        if process is None or process.stderr is None:
            return

        def drain(stream: object) -> None:
            try:
                for line in stream:  # type: ignore[attr-defined]
                    self._stderr_lines.append(line.rstrip("\n"))
            except (OSError, ValueError):
                return

        reader = threading.Thread(
            target=drain,
            args=(process.stderr,),
            name="voxink-helper-stderr",
            daemon=True,
        )
        self._stderr_reader = reader
        reader.start()

    def _stderr_detail(self) -> str:
        reader = self._stderr_reader
        if reader is not None:
            reader.join(timeout=1.0)
        return "\n".join(self._stderr_lines).strip()

    def _emit(self, event: str, *, level: str = "info", **fields: object) -> None:
        if self.diagnostics is not None:
            self.diagnostics.emit(event, level=level, **fields)

    def __enter__(self) -> HelperSupervisor:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def _preferred_loopback_port(preferred_port: int = PREFERRED_HELPER_PORT) -> int:
    if preferred_port not in HELPER_PORT_RANGE:
        raise ValueError("VoxInk helper ports must be within 8300-8399")
    candidates = (
        preferred_port,
        *(port for port in HELPER_PORT_RANGE if port != preferred_port),
    )
    last_error: OSError | None = None
    for port in candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", port))
                return int(listener.getsockname()[1])
        except OSError as error:
            last_error = error
    raise OSError("no available VoxInk helper port in 8300-8399") from last_error
