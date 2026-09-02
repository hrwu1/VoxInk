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
    AlignmentBlock,
    DiarizationError,
    DiarizationErrorCode,
    DiarizationResult,
    JobState,
)
from .pyannote import OFFLINE_ENVIRONMENT, resolve_device
from .splitting import SplitPolicy
from .transcript import TranscriptBlock, file_digest, part_digests, read_blocks
from .worker import decode_alignment, decode_result
from ..speakers import SpeakerRegistry, SpeakerRegistryError


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
    #: Blocks that word alignment split into more than one speaker's pieces.
    split_segments: int = 0
    #: Why blocks were not aligned, when they were not: the typed error the
    #: aligner stage reported, or None when it ran or was never configured.
    alignment_error: dict[str, Any] | None = None
    artifacts: tuple[str, ...] = ()
    #: Speakers the local registry recognized, by anonymous id.
    recognized: dict[str, str] = field(default_factory=dict)
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
            "split_segments": self.split_segments,
            "alignment_error": self.alignment_error,
            "artifacts": list(self.artifacts),
            "recognized_speakers": dict(self.recognized),
        }


class DiarizationJobManager:
    """Dispatch and supervise at most one diarization job per session/source."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        device: str = "auto",
        policy: ReconciliationPolicy | None = None,
        # The forced aligner that lets a block be split between speakers.
        # Optional: without it, blocks are labeled whole, as before.
        aligner_path: str | Path | None = None,
        split_policy: SplitPolicy | None = None,
        min_timeout_seconds: float = 600.0,
        # Measured CPU real-time factor is 0.378, so twice the audio duration
        # leaves better than fivefold headroom. The budget stays sized against
        # CPU even though `auto` normally picks MPS: CPU is still the fallback
        # wherever MPS is unavailable, and a budget tuned to the fast device
        # would kill legitimate work on the slow one.
        timeout_factor: float = 2.0,
        runner: Runner | None = None,
        enabled: bool = True,
        # Voices the user chose to remember; None disables recognition.
        registry: SpeakerRegistry | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self.device = device
        self.policy = policy or ReconciliationPolicy()
        self.aligner_path = Path(aligner_path) if aligner_path else None
        self.split_policy = split_policy or SplitPolicy()
        self.registry = registry
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
        aligner = self._aligner_model()
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
            # Whether blocks can be split between speakers. Not a condition
            # of `available`: diarization works without it, more coarsely.
            "alignment_available": aligner is not None,
            "aligner_model_id": aligner.source_model_id if aligner else None,
            "aligner_model_revision": aligner.revision if aligner else None,
        }

    def _aligner_model(self):
        if self.aligner_path is None:
            return None
        try:
            return require_local_model(self.aligner_path)
        except DiarizationError:
            return None

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
        # Times and speakers only -- never transcript text. A caller that is
        # already displaying captions can match these by time and label them,
        # which is the whole reason the segment carries its own timestamps.
        payload["segments"] = [
            {
                "start_ms": segment.get("start_ms"),
                "end_ms": segment.get("end_ms"),
                "speaker_id": segment.get("speaker_id"),
                "overlap_detected": segment.get("overlap_detected", False),
                # Character ranges, not text: a caller holding the caption can
                # slice it, and one that is not never sees a word of it.
                "pieces": [
                    {
                        "text_start": piece["text_start"],
                        "text_end": piece["text_end"],
                        "start_ms": piece["start_ms"],
                        "end_ms": piece["end_ms"],
                        "speaker_id": piece.get("speaker_id"),
                    }
                    for piece in segment.get("pieces", ())
                ],
            }
            for segment in document.get("segments", ())
            if segment.get("start_ms") is not None
        ]
        payload["split_segments"] = sum(
            1 for s in document.get("segments", ()) if s.get("unlabeled_reason") == "multiple_speakers"
        )
        # Names and registry links only; the embeddings stay in the file.
        payload["speakers"] = {
            speaker_id: {
                "name": (info or {}).get("name"),
                "registry_id": (info or {}).get("registry_id"),
                "similarity": (info or {}).get("similarity"),
                "has_embedding": bool((info or {}).get("embedding")),
            }
            for speaker_id, info in (document.get("speakers") or {}).items()
        }
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
            blocks = read_blocks(session_dir)
            payload = {
                "session_id": job.session_id,
                "source": job.source.value,
                "audio_path": str(joined),
                "model_path": str(self.model_path),
                "device": self.device,
            }
            aligner = self._aligner_model()
            if aligner is not None:
                payload["alignment"] = {
                    "model_path": str(aligner.path),
                    "blocks": [
                        block.to_dict() for block in _alignment_blocks(blocks, job.source)
                    ],
                }
            response = self._runner(payload, self._timeout_for(job.duration_ms))
            if not response.get("ok"):
                raise _from_response(response)
            result = decode_result(response["result"], source=job.source)
            alignment = response.get("alignment") or {}
            if aligner is not None and not alignment.get("ok"):
                job.alignment_error = (alignment.get("error") or DiarizationError(
                    DiarizationErrorCode.WORKER_CRASHED, "worker returned no alignment"
                ).to_dict())
            self._publish(job, session_dir, blocks, result, alignment)
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
        blocks: tuple[TranscriptBlock, ...],
        result: DiarizationResult,
        alignment: dict[str, Any],
    ) -> None:
        words = decode_alignment(alignment)
        alignments = {
            _block_ref(block_id): timings for block_id, timings in words.items()
        }
        labels = reconcile(
            blocks,
            exclusive_turns=result.exclusive_turns,
            raw_turns=result.raw_turns,
            source=job.source,
            policy=self.policy,
            alignments=alignments,
            split_policy=self.split_policy,
        )
        sources = dict(part_digests(session_dir))
        for part in source_parts(session_dir, job.source):
            sources[part.name] = file_digest(part)
        speakers = self._identify(result)
        write_artifacts(
            session_dir,
            session_id=job.session_id,
            source=job.source,
            result=result,
            labels=labels,
            source_artifacts=sources,
            aligner=(
                {
                    "model_id": alignment["model_id"],
                    "model_revision": alignment["model_revision"],
                }
                if words
                else None
            ),
            speakers=speakers,
        )
        job.recognized = {
            speaker_id: info["name"] for speaker_id, info in speakers.items() if info.get("name")
        }
        job.speaker_count = len(result.speaker_ids)
        job.total_segments = len(labels)
        job.labeled_segments = sum(1 for label in labels if label.labeled)
        job.split_segments = sum(1 for label in labels if label.reason == "multiple_speakers")
        job.artifacts = (TURNS_FILENAME, MARKDOWN_FILENAME)

    def _identify(self, result: DiarizationResult) -> dict[str, dict[str, Any]]:
        """Match this session's voices against the ones the user remembered.

        One registry voice may claim at most one session speaker: if two
        anonymous speakers both resemble the same known voice, the closer one
        gets the name and the other stays anonymous rather than duplicating a
        person.
        """
        registry = self.registry
        if registry is None or not result.embeddings:
            return {}
        candidates = []
        for speaker_id, embedding in result.embeddings.items():
            try:
                match = registry.match(embedding, model_id=result.model)
            except SpeakerRegistryError:
                match = None
            if match is not None:
                candidates.append((match.similarity, speaker_id, match))
        speakers: dict[str, dict[str, Any]] = {}
        claimed: set[str] = set()
        for similarity, speaker_id, match in sorted(candidates, key=lambda item: -item[0]):
            if match.speaker.id in claimed:
                continue
            claimed.add(match.speaker.id)
            speakers[speaker_id] = {
                "name": match.speaker.name,
                "registry_id": match.speaker.id,
                "similarity": round(similarity, 4),
            }
        return speakers

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


def _alignment_blocks(
    blocks: tuple[TranscriptBlock, ...], source: Source
) -> list[AlignmentBlock]:
    """The blocks this job can speak for, addressed so timings come back home."""
    return [
        AlignmentBlock(
            id=_block_id(block),
            start_ms=block.start_ms,
            end_ms=block.end_ms,
            text=block.text,
        )
        for block in blocks
        if block.source is source and block.has_valid_range and block.text.strip()
    ]


def _block_id(block: TranscriptBlock) -> str:
    return f"{block.part}#{block.block}"


def _block_ref(block_id: str) -> tuple[str, int]:
    part, _, index = block_id.rpartition("#")
    return part, int(index)


def _key(session_id: str, source: Source) -> tuple[str, str]:
    return session_id, Source(source).value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
