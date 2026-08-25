"""Manual Start/Pause/Resume/Stop session lifecycle."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .models import SessionConfig, TranscriptEvent
from .persistence.markdown import MarkdownTranscriptWriter


class SessionState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class InvalidSessionTransition(RuntimeError):
    pass


@dataclass
class Session:
    config: SessionConfig
    writer: MarkdownTranscriptWriter
    state: SessionState = SessionState.CREATED

    def start(self) -> Path:
        self._require(SessionState.CREATED, action="start")
        path = self.writer.open()
        self.state = SessionState.RUNNING
        return path

    def pause(self) -> None:
        self._require(SessionState.RUNNING, action="pause")
        self.state = SessionState.PAUSED

    def resume(self) -> None:
        self._require(SessionState.PAUSED, action="resume")
        self.state = SessionState.RUNNING

    def stop(self) -> None:
        if self.state not in {SessionState.RUNNING, SessionState.PAUSED}:
            raise InvalidSessionTransition(f"cannot stop a {self.state.value} session")
        self.writer.close()
        self.state = SessionState.STOPPED

    def append(self, event: TranscriptEvent) -> Path | None:
        self._require(SessionState.RUNNING, action="append transcript to")
        return self.writer.append(event)

    def _require(self, required: SessionState, *, action: str) -> None:
        if self.state is not required:
            raise InvalidSessionTransition(
                f"cannot {action} a {self.state.value} session; expected {required.value}"
            )


WriterFactory = Callable[[Path, SessionConfig], MarkdownTranscriptWriter]


class SessionManager:
    """Own active sessions and prevent duplicate session identifiers."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        writer_factory: WriterFactory = MarkdownTranscriptWriter,
    ) -> None:
        self.output_root = Path(output_root).expanduser()
        self._writer_factory = writer_factory
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def create(self, config: SessionConfig) -> Session:
        with self._lock:
            if config.session_id in self._sessions:
                raise ValueError(f"session already exists: {config.session_id}")
            directory = config.output_directory or self.output_root / config.session_id
            session = Session(config, self._writer_factory(Path(directory), config))
            self._sessions[config.session_id] = session
            return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError:
                raise KeyError(f"unknown session: {session_id}") from None

    def list(self) -> tuple[Session, ...]:
        with self._lock:
            return tuple(self._sessions.values())

    def start(self, session_id: str) -> Path:
        with self._lock:
            return self.get(session_id).start()

    def pause(self, session_id: str) -> None:
        with self._lock:
            self.get(session_id).pause()

    def resume(self, session_id: str) -> None:
        with self._lock:
            self.get(session_id).resume()

    def stop(self, session_id: str) -> None:
        with self._lock:
            self.get(session_id).stop()

    def append(self, event: TranscriptEvent) -> Path | None:
        with self._lock:
            return self.get(event.session_id).append(event)
