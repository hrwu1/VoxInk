"""Capture models shared by every module of the Windows backend.

The counterpart of `CaptureModels.swift`: sources, selection, configuration,
the frame that flows from a capture source to the service client, status
events, errors, and the reconnect policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
from urllib.parse import quote


class AudioSource(str, Enum):
    MICROPHONE = "microphone"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class SystemAudioSelection:
    """Which system audio to capture: every application, or the named ones.

    Applications are identified by executable name (`zoom.exe`), the Windows
    counterpart of a bundle identifier. Names are compared case-insensitively.
    """

    applications: frozenset[str] = frozenset()

    @classmethod
    def all(cls) -> SystemAudioSelection:
        return cls()

    @classmethod
    def for_applications(cls, identifiers: set[str] | list[str] | tuple[str, ...]) -> SystemAudioSelection:
        normalized = frozenset(identifier.strip().lower() for identifier in identifiers if identifier.strip())
        if not normalized:
            raise ValueError("at least one application identifier is required")
        return cls(normalized)

    @property
    def is_all(self) -> bool:
        return not self.applications

    @property
    def sorted_applications(self) -> list[str]:
        return sorted(self.applications)


class SystemAudioBackend(str, Enum):
    """Which Windows facility delivers system audio."""

    # Prefer a process loopback and fall back to endpoint loopback.
    AUTO = "auto"
    # WASAPI process loopback (Windows 10 build 20348 and newer). Follows the
    # default output device, keeps delivering while the endpoint is muted, and
    # can isolate one application per stream.
    PROCESS_LOOPBACK = "process-loopback"
    # WASAPI endpoint loopback through PortAudio, mixed into one stream.
    LOOPBACK = "loopback"

    @classmethod
    def parse(cls, value: str) -> SystemAudioBackend:
        # The helper's names for the same two roles, so its `--backend`
        # values need no translation.
        aliases = {"tap": cls.PROCESS_LOOPBACK, "screencapturekit": cls.LOOPBACK}
        normalized = value.strip().lower()
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError:
            raise ValueError(
                f"Unknown backend: {value}. Use auto, process-loopback, or loopback."
            ) from None


def default_service_url(session_id: str) -> str:
    return f"ws://127.0.0.1:8300/v1/sessions/{quote(session_id, safe='')}/stream"


@dataclass(frozen=True, slots=True)
class CaptureConfiguration:
    session_id: str
    microphone_enabled: bool
    system_audio: SystemAudioSelection | None
    sample_rate: int = 16_000
    service_url: str | None = None
    auth_token: str | None = field(default_factory=lambda: os.environ.get("VOXINK_AUTH_SECRET"))
    retain_audio: bool = False
    backend: SystemAudioBackend = SystemAudioBackend.AUTO
    # Device UID of the microphone to open, from `--list-devices`; None means
    # the default input device.
    microphone_device_uid: str | None = None
    # Manual language override handed to the service ("en" or "zh"); None
    # lets the model detect it.
    language: str | None = None
    # Display names for selected applications, keyed by identifier, so a
    # per-application stream can be labeled the way the user knows it.
    application_names: dict[str, str] = field(default_factory=dict)
    # Where audio the service never received is written as WAV files; None
    # keeps it under the user's local application data.
    spool_directory: Path | None = None

    def __post_init__(self) -> None:
        if self.service_url is None:
            object.__setattr__(self, "service_url", default_service_url(self.session_id))

    @property
    def selected_applications(self) -> list[str]:
        """Identifiers this session captures, empty for microphone-only or
        all-system capture. Every stream reports the same list so whichever
        one opens first records it in the transcript metadata."""
        if self.system_audio is None:
            return []
        return self.system_audio.sorted_applications


@dataclass(frozen=True, slots=True)
class AudioFrame:
    session_id: str
    stream_id: str
    source: AudioSource
    sequence: int
    timestamp_ns: int
    sample_rate: int
    pcm_s16le: bytes
    channels: int = 1
    application_identifiers: tuple[str, ...] | None = None
    # For a per-application system stream, the identifier it isolates.
    channel: str | None = None
    channel_name: str | None = None

    @property
    def duration_ms(self) -> float:
        return len(self.pcm_s16le) / 2.0 / self.channels / self.sample_rate * 1000.0


class StatusLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CaptureStatusEvent:
    session_id: str
    source: AudioSource | None
    level: StatusLevel
    code: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "capture.status",
            "schema_version": 1,
            "session_id": self.session_id,
            "source": self.source.value if self.source else None,
            "level": self.level.value,
            "code": self.code,
            "message": self.message,
        }


class CaptureError(Exception):
    """A capture that cannot start or continue, with a stable `code`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code

    @classmethod
    def no_capture_source(cls) -> CaptureError:
        return cls("no_capture_source", "Enable microphone capture, system capture, or both.")

    @classmethod
    def invalid_service_url(cls, url: str) -> CaptureError:
        return cls(
            "invalid_service_url",
            f"The capture service must be a ws:// or wss:// loopback URL; received {url}.",
        )

    @classmethod
    def microphone_unavailable(cls, detail: str) -> CaptureError:
        return cls(
            "microphone_unavailable",
            f"The microphone could not be opened: {detail}. Check System Settings > Privacy > Microphone.",
        )

    @classmethod
    def microphone_device_unavailable(cls, uid: str) -> CaptureError:
        return cls("microphone_device_unavailable", f"The selected microphone is not connected: {uid}.")

    @classmethod
    def no_selected_applications(cls, identifiers: set[str] | frozenset[str]) -> CaptureError:
        return cls(
            "no_selected_applications",
            "None of the selected applications are currently running: "
            + ", ".join(sorted(identifiers)) + ".",
        )

    @classmethod
    def audio_format_unavailable(cls, detail: str) -> CaptureError:
        return cls("audio_format_unavailable", f"The capture source exposed an unusable audio format: {detail}.")

    @classmethod
    def conversion_failed(cls, detail: str) -> CaptureError:
        return cls("conversion_failed", f"An audio frame could not be normalized: {detail}.")

    @classmethod
    def loopback_unavailable(cls, detail: str) -> CaptureError:
        return cls("loopback_unavailable", f"System audio could not be captured through endpoint loopback: {detail}")

    @classmethod
    def process_loopback_unavailable(cls, detail: str) -> CaptureError:
        return cls("process_loopback_unavailable", f"System audio could not be captured through process loopback: {detail}")


@dataclass(frozen=True, slots=True)
class InputDevice:
    """One microphone the system exposes, addressed by a UID stable for as
    long as the device keeps its name."""

    uid: str
    name: str
    is_default: bool


@dataclass(frozen=True, slots=True)
class CapturableApplication:
    """A process with an audio session: anything that has opened the audio
    engine. Processes that have never played audio are not listed, which is
    why selected-application capture waits for them rather than failing."""

    name: str
    identifier: str
    process_id: int


class ReconnectDecision(Enum):
    CONNECT = "connect"
    RECONNECT = "reconnect"
    WAIT = "wait"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class SocketDecision:
    decision: ReconnectDecision
    # For RECONNECT, one more than the consecutive failures so far.
    attempt: int = 0
    # For WAIT, how long until the next attempt is allowed.
    wait_seconds: float = 0.0


class StreamReconnectPolicy:
    """What a stream does when it has no socket.

    The first time is an ordinary connect. After that the socket died under a
    live stream, and reopening it is a reconnect the user must hear about: the
    service holds the session open for a grace period, so a prompt reconnect
    keeps the recording whole. A reconnect that fails is retried with
    exponential backoff for as long as the recording runs, never abandoned:
    the frames captured meanwhile are queued, and a service that is gone for
    good costs a spool file, not the recording. Only a service that says the
    session is unavailable ends the stream, because it has finalized the
    session and would keep nothing sent afterwards.
    """

    def __init__(self, initial_delay_seconds: float = 0.25, maximum_delay_seconds: float = 5.0) -> None:
        self.initial_delay_seconds = initial_delay_seconds
        self.maximum_delay_seconds = maximum_delay_seconds
        self._started: set[str] = set()
        self._reconnecting: set[str] = set()
        self._failures: dict[str, int] = {}
        self._not_before: dict[str, float] = {}
        self._dead: set[str] = set()

    def socket_needed(self, stream_id: str, now: float) -> SocketDecision:
        """Called when a frame arrives for a stream with no socket. `now` is a
        monotonic clock in seconds, the same one `attempt_failed` was given."""
        if stream_id in self._dead:
            return SocketDecision(ReconnectDecision.DEAD)
        if stream_id not in self._started:
            self._started.add(stream_id)
            return SocketDecision(ReconnectDecision.CONNECT)
        earliest = self._not_before.get(stream_id)
        if earliest is not None and now < earliest:
            return SocketDecision(ReconnectDecision.WAIT, wait_seconds=earliest - now)
        self._reconnecting.add(stream_id)
        return SocketDecision(ReconnectDecision.RECONNECT, attempt=self._failures.get(stream_id, 0) + 1)

    def attempt_failed(self, stream_id: str, now: float) -> float:
        """The socket could not be opened or `stream.start` not sent. Returns
        how long to wait before the next attempt."""
        count = self._failures.get(stream_id, 0) + 1
        self._failures[stream_id] = count
        delay = min(self.initial_delay_seconds * 2 ** (count - 1), self.maximum_delay_seconds)
        self._not_before[stream_id] = now + delay
        return delay

    def stream_ready(self, stream_id: str) -> bool:
        """The service accepted the stream. Returns whether this closed a reconnect."""
        self._failures.pop(stream_id, None)
        self._not_before.pop(stream_id, None)
        if stream_id in self._reconnecting:
            self._reconnecting.discard(stream_id)
            return True
        return False

    def session_unavailable(self, stream_id: str) -> None:
        """The service refused the stream because its session is gone."""
        self._dead.add(stream_id)

    def is_dead(self, stream_id: str) -> bool:
        return stream_id in self._dead
