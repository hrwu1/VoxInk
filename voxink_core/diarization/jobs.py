"""Own the post-Stop diarization job for a session.

Stop must never wait for a model. The transcript is finished and readable the
moment capture ends; diarization is a slower, entirely optional pass that runs
afterwards and is allowed to fail without taking anything else with it. So this
returns immediately, does its work on a background thread, supervises a child
process, and records a state a caller can poll.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any, Callable

from ..models import Source
from .alignment import ReconciliationPolicy, reconcile
from .artifacts import (
    MARKDOWN_FILENAME,
    TURNS_FILENAME,
    read_document,
    stale_sources,
    write_artifacts,
)
from .audio import assemble, source_parts
from .base import require_local_model
from .models import (
    DiarizationError,
    DiarizationErrorCode,
    DiarizationResult,
    JobState,
)
from .pyannote import OFFLINE_ENVIRONMENT, resolve_device
from .transcript import file_digest, part_digests, read_blocks
from .worker import decode_result


#: Private scratch space inside the session directory. Anything here is
#: disposable and is removed on every exit path.
WORKING_DIRECTORY = ".voxink-diarization"

#: A joined session is roughly 32 kB/s, and the worker needs room for it plus
#: headroom. Refusing early beats filling the user's disk mid-job.
DISK_HEADROOM_BYTES = 256 * 1024 * 1024

Runner = Callable[[dict[str, Any], float], dict[str, Any]]


@dataclass
class DiarizationJob:
    """One session/source pair's diarization lifecycle."""

    session_id: str
    source: Source
    state: JobState = JobState.WAITING_FOR_STOP
    error: dict[str, Any] | None = None
    created_at: str = ""
    finished_at: str | None = None
    duration_ms: int = 0
    speaker_count: int = 0
    labeled_segments: int = 0
    total_segments: int = 0
    artifacts: tuple[str, ...] = ()
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source": self.source.value,
            "state": self.state.value,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "audio_duration_ms": self.duration_ms,
            "speaker_count": self.speaker_count,
            "labeled_segments": self.labeled_segments,
            "total_segments": self.total_segments,
            "artifacts": list(self.artifacts),
        }


class DiarizationJobManager:
    """Dispatch and supervise at most one diarization job per session/source."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        device: str = "auto",
        policy: ReconciliationPolicy | None = None,
        min_timeout_seconds: float = 600.0,
        # Measured CPU real-time factor is 0.378, so twice the audio duration
        # leaves better than fivefold headroom. The budget stays sized against
        # CPU even though `auto` normally picks MPS: CPU is still the fallback
        # wherever MPS is unavailable, and a budget tuned to the fast device
        # would kill legitimate work on the slow one.
        timeout_factor: float = 2.0,
        runner: Runner | None = None,
        enabled: bool = True,
    ) -> None:
        self.model_path = Path(model_path)
        self.device = device
        self.policy = policy or ReconciliationPolicy()
        self.min_timeout_seconds = min_timeout_seconds
        self.timeout_factor = timeout_factor
        self.enabled = enabled
        self._runner = runner or self._run_worker
        self._jobs: dict[tuple[str, str], DiarizationJob] = {}
        self._lock = threading.RLock()

    # -- capability and status -------------------------------------------------

    def capability(self) -> dict[str, Any]:
        """Whether diarization can run at all, without loading anything heavy."""
        available = True
        detail = None
        try:
            model = require_local_model(self.model_path)
        except DiarizationError as exc:
            available = False
            detail = exc.to_dict()
            model = None
        return {
            "enabled": self.enabled,
            "available": self.enabled and available,
            "device": self.device,
            # What `auto` actually picks, so a caller is not left guessing
            # whether the fast path is in play.
            "resolved_device": resolve_device(self.device),
            "model_id": model.source_model_id if model else None,
            "model_revision": model.revision if model else None,
            "error": detail,
        }

    def get(self, session_id: str, source: Source = Source.MICROPHONE) -> DiarizationJob | None:
        with self._lock:
            return self._jobs.get(_key(session_id, source))

    def describe(
        self, session_dir: str | Path, session_id: str, source: Source = Source.MICROPHONE
    ) -> dict[str, Any]:
        """Report the job, falling back to what is already on disk.

        A job is in-memory state and does not survive a restart, but the
        artifacts do. Reading them back means a completed session still reports
        `ready` -- or `stale`, if a transcript part changed since.
        """
        job = self.get(session_id, source)
        if job is not None and job.state is not JobState.READY:
            return job.to_dict()
        document = read_document(session_dir)
        if document is None:
            return (job or self._disabled_job(session_id, source)).to_dict()
        stale = stale_sources(session_dir, document)
        resolved = job or DiarizationJob(
            session_id=session_id,
            source=Source(source),
            state=JobState.READY,
            created_at=document.get("created_at", ""),
            finished_at=document.get("created_at"),
            artifacts=(TURNS_FILENAME, MARKDOWN_FILENAME),
            speaker_count=len({t["speaker_id"] for t in document.get("raw_turns", ())}),
            total_segments=len(document.get("segments", ())),
            labeled_segments=sum(
                1 for s in document.get("segments", ()) if s.get("speaker_id")
            ),
        )
        payload = resolved.to_dict()
        if stale:
            payload["state"] = JobState.STALE.value
            payload["error"] = DiarizationError(
                DiarizationErrorCode.ARTIFACT_STALE,
                "source transcript changed after diarization; regenerate to refresh",
                changed=list(stale),
            ).to_dict()
        return payload

    # -- dispatch --------------------------------------------------------------

    def request(
        self,
        session_dir: str | Path,
        session_id: str,
        source: Source = Source.MICROPHONE,
        *,
        regenerate: bool = False,
    ) -> DiarizationJob:
        """Start diarization for a finished session, or return the running job.

        The caller is responsible for the trigger conditions: the logical
        session is stopped, every stream has flushed, and the transcript and
        audio writers are closed. Dispatching earlier would diarize a partial
        recording.
        """
        source = Source(source)
        directory = Path(session_dir)
        with self._lock:
            key = _key(session_id, source)
            existing = self._jobs.get(key)
            if existing is not None and existing.state is JobState.PROCESSING:
                # One owner per session/source. A second request joins the job
                # that is already running rather than starting a rival worker.
                return existing
            if existing is not None and existing.state is JobState.READY and not regenerate:
                return existing
            if not self.enabled:
                return self._disabled_job(session_id, source)
            job = DiarizationJob(
                session_id=session_id,
                source=source,
                state=JobState.PROCESSING,
                created_at=_now(),
            )
            self._jobs[key] = job
            thread = threading.Thread(
                target=self._process,
                args=(job, directory),
                name=f"diarization-{session_id}",
                daemon=True,
            )
            job._thread = thread
            thread.start()
            return job

    def wait(self, session_id: str, source: Source = Source.MICROPHONE, timeout: float | None = None) -> None:
        """Block until a job settles. For tests and shutdown, not request paths."""
        job = self.get(session_id, source)
        if job is not None and job._thread is not None:
            job._thread.join(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            threads = [job._thread for job in self._jobs.values() if job._thread]
        for thread in threads:
            thread.join(timeout)

    # -- the job itself --------------------------------------------------------

    def _process(self, job: DiarizationJob, session_dir: Path) -> None:
        working = session_dir / WORKING_DIRECTORY
        joined = working / "microphone-joined.wav"
        try:
            working.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._require_disk(session_dir, job.source)
            job.duration_ms = assemble(session_dir, joined, source=job.source)
            os.chmod(joined, 0o600)
            response = self._runner(
                {
                    "session_id": job.session_id,
                    "source": job.source.value,
                    "audio_path": str(joined),
                    "model_path": str(self.model_path),
                    "device": self.device,
                },
                self._timeout_for(job.duration_ms),
            )
            if not response.get("ok"):
                raise _from_response(response)
            result = decode_result(response["result"], source=job.source)
            self._publish(job, session_dir, joined, result)
            job.state = JobState.READY
        except DiarizationError as exc:
            job.state = JobState.FAILED
            job.error = exc.to_dict()
        except Exception as exc:  # noqa: BLE001 - the job must never take the service with it
            job.state = JobState.FAILED
            job.error = DiarizationError(
                DiarizationErrorCode.WORKER_CRASHED, f"{type(exc).__name__}: {exc}"
            ).to_dict()
        finally:
            job.finished_at = _now()
            # Temporary audio is removed on every path: success, typed failure,
            # crash, and timeout. It is a copy of data the user already has.
            shutil.rmtree(working, ignore_errors=True)

    def _publish(
        self,
        job: DiarizationJob,
        session_dir: Path,
        joined: Path,
        result: DiarizationResult,
    ) -> None:
        blocks = read_blocks(session_dir)
        labels = reconcile(
            blocks,
            exclusive_turns=result.exclusive_turns,
            raw_turns=result.raw_turns,
            source=job.source,
            policy=self.policy,
        )
        sources = dict(part_digests(session_dir))
        for part in source_parts(session_dir, job.source):
            sources[part.name] = file_digest(part)
        write_artifacts(
            session_dir,
            session_id=job.session_id,
            source=job.source,
            result=result,
            labels=labels,
            source_artifacts=sources,
        )
        job.speaker_count = len(result.speaker_ids)
        job.total_segments = len(labels)
        job.labeled_segments = sum(1 for label in labels if label.labeled)
        job.artifacts = (TURNS_FILENAME, MARKDOWN_FILENAME)

    def _timeout_for(self, duration_ms: int) -> float:
        return max(self.min_timeout_seconds, duration_ms / 1000.0 * self.timeout_factor)

    def _require_disk(self, session_dir: Path, source: Source) -> None:
        needed = sum(part.stat().st_size for part in source_parts(session_dir, source))
        try:
            free = shutil.disk_usage(session_dir).free
        except OSError:
            return
        if free < needed + DISK_HEADROOM_BYTES:
            raise DiarizationError(
                DiarizationErrorCode.INSUFFICIENT_DISK,
                "not enough free space to assemble the session audio",
                needed_bytes=needed + DISK_HEADROOM_BYTES,
                free_bytes=free,
            )

    def _run_worker(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Run the worker as a child process and bring back its response."""
        environment = {**os.environ, **OFFLINE_ENVIRONMENT}
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "voxink_core.diarization.worker"],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=environment,
                cwd=Path(__file__).resolve().parents[2],
            )
        except subprocess.TimeoutExpired as exc:
            raise DiarizationError(
                DiarizationErrorCode.WORKER_TIMED_OUT,
                f"diarization exceeded its {timeout:.0f}s budget",
                timeout_seconds=round(timeout),
            ) from exc
        if completed.returncode != 0:
            raise DiarizationError(
                DiarizationErrorCode.WORKER_CRASHED,
                f"worker exited with status {completed.returncode}: "
                f"{completed.stderr.strip()[-200:]}",
                exit_status=completed.returncode,
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DiarizationError(
                DiarizationErrorCode.WORKER_CRASHED,
                f"worker produced an unreadable response: {exc}",
            ) from exc

    def _disabled_job(self, session_id: str, source: Source) -> DiarizationJob:
        return DiarizationJob(
            session_id=session_id,
            source=Source(source),
            state=JobState.DISABLED,
            created_at=_now(),
        )


def _from_response(response: dict[str, Any]) -> DiarizationError:
    error = response.get("error") or {}
    try:
        code = DiarizationErrorCode(error.get("code"))
    except ValueError:
        code = DiarizationErrorCode.WORKER_CRASHED
    return DiarizationError(
        code, error.get("message", "diarization failed"), **(error.get("metadata") or {})
    )


def _key(session_id: str, source: Source) -> tuple[str, str]:
    return session_id, Source(source).value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
