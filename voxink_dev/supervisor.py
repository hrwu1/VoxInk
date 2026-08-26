"""Own the private transcription helper and capture client processes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from typing import Callable
from urllib.error import URLError
from urllib.request import urlopen

from services.transcription.app import PROTOCOL_VERSION


class SupervisorError(RuntimeError):
    """A child process failed or did not satisfy the local protocol contract."""


@dataclass(frozen=True, slots=True)
class HelperConfig:
    model_path: Path
    output_root: Path
    device: str = "auto"
    readiness_timeout: float = 30.0


class HelperSupervisor:
    def __init__(
        self,
        config: HelperConfig,
        *,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.config = config
        self.process_factory = process_factory
        self.auth_secret = secrets.token_urlsafe(32)
        self.port: int | None = None
        self.process: subprocess.Popen[str] | None = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise SupervisorError("helper has not been started")
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> dict[str, object]:
        if self.process is not None:
            raise SupervisorError("helper is already running")
        self.port = _unused_loopback_port()
        environment = os.environ.copy()
        environment["VOXINK_AUTH_SECRET"] = self.auth_secret
        command = [
            sys.executable,
            "-m",
            "services.transcription",
            "--port",
            str(self.port),
            "--model",
            str(self.config.model_path),
            "--output",
            str(self.config.output_root),
            "--device",
            self.config.device,
        ]
        self.process = self.process_factory(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            return self.wait_until_ready()
        except BaseException:
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
                detail = (process.stderr.read() if process.stderr else "").strip()
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
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        finally:
            if process.stderr is not None:
                process.stderr.close()

    def __enter__(self) -> HelperSupervisor:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
