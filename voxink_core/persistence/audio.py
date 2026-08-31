"""Optional, recoverable PCM audio retention for long-running sessions."""

from __future__ import annotations

import os
import shutil
import struct
import sys
import threading
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

from ..models import Source


_WAV_HEADER_SIZE = 44
_RIFF_DATA_LIMIT = 0xFFFFFFFF - 36

#: Space retention will not consume. A session that reaches this keeps
#: transcribing without its audio; the transcript is the artifact, and the
#: recording is a convenience that another session can produce again.
DEFAULT_MIN_FREE_BYTES = 512 * 1024 * 1024

#: How much audio may be appended between free-space probes. Probing is a
#: syscall, and at 16 kHz mono this is roughly two minutes of audio, so the
#: reserve above absorbs anything written between two checks many times over.
DEFAULT_FREE_SPACE_CHECK_BYTES = 4 * 1024 * 1024


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


@dataclass
class _Track:
    source: Source
    part: int
    path: Path
    handle: BinaryIO
    data_bytes: int
    written_until_frame: int


class AudioRetentionWriter:
    """Persist timestamped signed-16-bit little-endian PCM as aligned WAV parts.

    Retention is off unless a caller explicitly constructs this class and feeds
    it audio. Part boundaries are shared time/frame boundaries across sources,
    so independently written microphone and system tracks can be mixed later.
    """

    def __init__(
        self,
        session_dir: str | Path,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        max_part_seconds: float = 60 * 60,
        max_part_bytes: int = 2 * 1024 * 1024 * 1024,
        durable_flush: bool = True,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        max_session_bytes: int | None = None,
        free_space_check_bytes: int = DEFAULT_FREE_SPACE_CHECK_BYTES,
        free_space_probe: Callable[[Path], int] = _free_bytes,
    ) -> None:
        if sample_rate <= 0 or channels <= 0:
            raise ValueError("sample_rate and channels must be positive")
        if max_part_seconds <= 0 or max_part_bytes <= 0:
            raise ValueError("rotation limits must be positive")
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes cannot be negative")
        if max_session_bytes is not None and max_session_bytes <= 0:
            raise ValueError("max_session_bytes must be positive when set")
        if free_space_check_bytes <= 0:
            raise ValueError("free_space_check_bytes must be positive")
        self.session_dir = Path(session_dir)
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = 2
        self.frame_bytes = channels * self.sample_width
        safe_bytes = min(max_part_bytes, _RIFF_DATA_LIMIT)
        byte_frames = safe_bytes // self.frame_bytes
        time_frames = int(max_part_seconds * sample_rate)
        self.frames_per_part = min(byte_frames, time_frames)
        if self.frames_per_part <= 0:
            raise ValueError("rotation limits must accommodate at least one PCM frame")
        self.max_data_bytes = self.frames_per_part * self.frame_bytes
        self.durable_flush = durable_flush
        self._tracks: dict[Source, _Track] = {}
        # Part numbers stay time-aligned within one run so microphone and system
        # parts of the same number always cover the same audio. A run that finds
        # a previous run's parts in the directory numbers its own above them, and
        # every source shares the offset so that alignment survives.
        self._part_base = 0
        self._part_base_resolved = False
        self._resuming = False
        self._lock = threading.RLock()
        self.min_free_bytes = min_free_bytes
        self.max_session_bytes = max_session_bytes
        self.free_space_check_bytes = free_space_check_bytes
        self._free_space_probe = free_space_probe
        self._retained_bytes = 0
        # Primed so the first append probes: a session started on a disk that
        # is already full must not write a whole interval before noticing.
        self._bytes_since_probe = free_space_check_bytes
        self._stopped_reason: str | None = None

    @property
    def retained_bytes(self) -> int:
        """Audio bytes this writer has appended, excluding WAV headers."""
        return self._retained_bytes

    @property
    def retention_stopped(self) -> bool:
        return self._stopped_reason is not None

    @property
    def stopped_reason(self) -> str | None:
        """Why retention stopped: `low_disk`, `session_limit`, or None."""
        return self._stopped_reason

    def append_pcm(
        self,
        source: Source | str,
        pcm_s16le: bytes,
        *,
        start_ms: int,
    ) -> tuple[Path, ...]:
        """Append PCM at a session-relative time, returning all touched parts.

        Forward gaps are filled with digital silence. If a chunk overlaps audio
        already written to that source, the overlapping prefix is discarded.
        Chunks must arrive chronologically per source.
        """
        source = Source(source)
        if start_ms < 0:
            raise ValueError("start_ms must be non-negative")
        if not isinstance(pcm_s16le, bytes):
            raise TypeError("pcm_s16le must be bytes")
        if len(pcm_s16le) % self.frame_bytes:
            raise ValueError("PCM length must contain complete sample frames")
        if not pcm_s16le:
            return ()

        with self._lock:
            if self._stopped_reason is not None:
                return ()
            self.session_dir.mkdir(parents=True, exist_ok=True)
            if self._enforce_bounds(len(pcm_s16le)):
                return ()
            start_frame = start_ms * self.sample_rate // 1000
            chunk_frames = len(pcm_s16le) // self.frame_bytes
            track = self._tracks.get(source)
            if track is None:
                track = self._recover_or_open(source, start_frame, chunk_frames)

            overlap = max(0, track.written_until_frame - start_frame)
            if overlap >= chunk_frames:
                return ()
            if overlap:
                pcm_s16le = pcm_s16le[overlap * self.frame_bytes :]
                start_frame += overlap

            touched: list[Path] = []
            cursor = start_frame
            offset = 0
            remaining = len(pcm_s16le) // self.frame_bytes
            while remaining:
                part = self._part_for(cursor)
                if track.part != part:
                    self._close_track(track)
                    track = self._open_track(source, part)
                    self._tracks[source] = track
                part_start = self._part_start(part)
                position = cursor - part_start
                existing_frames = track.data_bytes // self.frame_bytes
                if position > existing_frames:
                    self._write_bytes(
                        track, b"\0" * ((position - existing_frames) * self.frame_bytes)
                    )
                capacity = self.frames_per_part - position
                count = min(remaining, capacity)
                byte_count = count * self.frame_bytes
                self._write_bytes(track, pcm_s16le[offset : offset + byte_count])
                if not touched or touched[-1] != track.path:
                    touched.append(track.path)
                cursor += count
                offset += byte_count
                remaining -= count
                track.written_until_frame = cursor
                self._flush_track(track)
            return tuple(touched)

    def close(self) -> None:
        with self._lock:
            for track in tuple(self._tracks.values()):
                self._close_track(track)
            self._tracks.clear()

    def generate_combined(
        self,
        *,
        overwrite: bool = False,
        chunk_frames: int = 8192,
    ) -> tuple[Path, ...]:
        """Create aligned combined parts with a chunked saturating sample mix.

        Parts that already have a combined file are left untouched and skipped
        rather than aborting the run: a session directory can hold parts from an
        earlier run whose combined output is already final, and stopping at the
        first of those would leave this run's parts unmixed. Only the parts
        written here are returned.
        """
        if chunk_frames <= 0:
            raise ValueError("chunk_frames must be positive")
        with self._lock:
            self.close()
            parts = sorted(set(self._part_numbers(Source.MICROPHONE)) | set(self._part_numbers(Source.SYSTEM)))
            outputs: list[Path] = []
            for part in parts:
                mic_path = self._path(Source.MICROPHONE, part)
                system_path = self._path(Source.SYSTEM, part)
                inputs = [path for path in (mic_path, system_path) if path.is_file()]
                if not inputs:
                    continue
                output = self.session_dir / f"combined-part-{part:03d}.wav"
                if output.exists() and not overwrite:
                    continue
                temporary = output.with_suffix(".wav.tmp")
                if temporary.exists():
                    temporary.unlink()
                try:
                    self._mix_part(inputs, temporary, chunk_frames)
                    os.replace(temporary, output)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                outputs.append(output)
            return tuple(outputs)

    def __enter__(self) -> "AudioRetentionWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _part_for(self, frame: int) -> int:
        return self._part_base + frame // self.frames_per_part + 1

    def _part_start(self, part: int) -> int:
        return (part - 1 - self._part_base) * self.frames_per_part

    def _recover_or_open(
        self, source: Source, requested_start: int, chunk_frames: int
    ) -> _Track:
        """Resume the previous writer's last part, or roll past a finished run.

        A process that stops and continues hands the next writer audio that
        carries on from where the last one stopped, and resuming its part is
        what keeps one recording in one file. Such a chunk normally starts a
        little behind the stored cursor, because chunkers retain overlap, but it
        still reaches past it.

        A *new* run reusing the session directory restarts its clock instead, so
        its first chunk ends before the cursor and carries nothing new. That is
        indistinguishable from a fully repeated chunk, and treating it as one
        would silently discard the whole recording, so the run is given fresh
        part numbers above everything already stored, the way the Markdown
        writer does. A restarted run whose first chunk is longer than the whole
        previous run still loses that much to overlap trimming, which is the
        most these two cases can be told apart from the audio alone.
        """
        parts = self._part_numbers(source)
        if not self._part_base_resolved:
            # The first source to write decides, for the whole run, whether this
            # continues the stored parts or numbers above them. Deciding per
            # source instead would let whichever source happens to arrive first
            # sit below the other's offset, and generate_combined would pair this
            # run's audio with a previous run's.
            committed = self._committed_end(source, parts[-1]) if parts else None
            self._resuming = (
                committed is not None and requested_start + chunk_frames > committed
            )
            if not self._resuming:
                self._part_base = max(self._part_base, self._highest_part())
            self._part_base_resolved = True
        if self._resuming and parts:
            # _committed_end reads _part_base, which is untouched on this path.
            committed = self._committed_end(source, parts[-1])
            if committed is not None and requested_start + chunk_frames > committed:
                track = self._open_existing_track(source, parts[-1])
                self._tracks[source] = track
                return track
        track = self._open_track(source, self._part_for(requested_start))
        self._tracks[source] = track
        return track

    def _committed_end(self, source: Source, part: int) -> int | None:
        """Frame index one past the last sample committed to a part's header.

        `None` means the part cannot be read, and therefore cannot prove that
        incoming audio duplicates it. The caller rolls to a fresh part rather
        than resuming a corrupt file and discarding live audio into it.
        """
        try:
            with self._path(source, part).open("rb") as handle:
                _, _, data_bytes = _read_header(handle)
        except (OSError, ValueError):
            return None
        return self._part_start(part) + data_bytes // self.frame_bytes

    def _highest_part(self) -> int:
        return max(
            (numbers[-1] for numbers in map(self._part_numbers, Source) if numbers),
            default=0,
        )

    def _open_track(self, source: Source, part: int) -> _Track:
        path = self._path(source, part)
        try:
            handle = path.open("x+b")
        except FileExistsError:
            return self._open_existing_track(source, part)
        handle.write(_wav_header(self.sample_rate, self.channels, 0))
        handle.flush()
        return _Track(
            source=source,
            part=part,
            path=path,
            handle=handle,
            data_bytes=0,
            written_until_frame=self._part_start(part),
        )

    def _open_existing_track(self, source: Source, part: int) -> _Track:
        path = self._path(source, part)
        handle = path.open("r+b")
        try:
            rate, channels, data_bytes = _read_header(handle)
            if rate != self.sample_rate or channels != self.channels:
                raise ValueError(f"audio format mismatch in recovered file: {path}")
            data_bytes -= data_bytes % self.frame_bytes
            actual_bytes = max(0, path.stat().st_size - _WAV_HEADER_SIZE)
            # Trust only bytes committed to the header. Extra bytes may be from
            # a crash between data append and header update.
            data_bytes = min(data_bytes, actual_bytes, self.max_data_bytes)
            handle.truncate(_WAV_HEADER_SIZE + data_bytes)
            handle.seek(0, os.SEEK_END)
        except Exception:
            handle.close()
            raise
        return _Track(
            source, part, path, handle, data_bytes,
            self._part_start(part) + data_bytes // self.frame_bytes,
        )

    def _enforce_bounds(self, incoming_bytes: int) -> bool:
        """Stop retention if this append would cross a bound. True if stopped.

        Both bounds are checked before the write rather than after, so that the
        reserve is a reserve: crossing it is what stops retention, not what it
        has already consumed.
        """
        if self.max_session_bytes is not None:
            if self._retained_bytes + incoming_bytes > self.max_session_bytes:
                self._stop_retention("session_limit")
                return True
        if self.min_free_bytes and self._bytes_since_probe >= self.free_space_check_bytes:
            self._bytes_since_probe = 0
            try:
                free = self._free_space_probe(self.session_dir)
            except OSError:
                # An unreadable destination is the filesystem's problem to
                # report on the next write; it is not evidence of a full disk,
                # and stopping retention here would hide the real error.
                return False
            if free - incoming_bytes < self.min_free_bytes:
                self._stop_retention("low_disk")
                return True
        return False

    def _stop_retention(self, reason: str) -> None:
        """Close cleanly and record why, leaving what was written playable."""
        for track in tuple(self._tracks.values()):
            self._close_track(track)
        self._tracks.clear()
        self._stopped_reason = reason

    def _write_bytes(self, track: _Track, data: bytes) -> None:
        if track.data_bytes + len(data) > self.max_data_bytes:
            raise RuntimeError("internal error: audio write exceeded part capacity")
        track.handle.write(data)
        track.data_bytes += len(data)
        self._retained_bytes += len(data)
        self._bytes_since_probe += len(data)

    def _flush_track(self, track: _Track) -> None:
        _update_wav_sizes(track.handle, track.data_bytes)
        track.handle.flush()
        if self.durable_flush:
            os.fsync(track.handle.fileno())

    def _close_track(self, track: _Track) -> None:
        if track.handle.closed:
            return
        self._flush_track(track)
        track.handle.close()

    def _part_numbers(self, source: Source) -> list[int]:
        prefix = f"{source.value}-part-"
        return sorted(
            int(path.stem.removeprefix(prefix))
            for path in self.session_dir.glob(f"{prefix}[0-9][0-9][0-9].wav")
        )

    def _path(self, source: Source, part: int) -> Path:
        return self.session_dir / f"{source.value}-part-{part:03d}.wav"

    def _mix_part(self, inputs: Iterable[Path], output: Path, chunk_frames: int) -> None:
        readers = [wave.open(str(path), "rb") for path in inputs]
        try:
            for reader in readers:
                if (
                    reader.getframerate() != self.sample_rate
                    or reader.getnchannels() != self.channels
                    or reader.getsampwidth() != self.sample_width
                    or reader.getcomptype() != "NONE"
                ):
                    raise ValueError("all source WAV files must use matching PCM s16le")
            total_frames = max(reader.getnframes() for reader in readers)
            with wave.open(str(output), "wb") as writer:
                writer.setnchannels(self.channels)
                writer.setsampwidth(self.sample_width)
                writer.setframerate(self.sample_rate)
                remaining = total_frames
                while remaining:
                    count = min(chunk_frames, remaining)
                    sample_count = count * self.channels
                    mixed = [0] * sample_count
                    for reader in readers:
                        samples = array("h", reader.readframes(count))
                        if sys.byteorder != "little":
                            samples.byteswap()
                        for index, sample in enumerate(samples):
                            mixed[index] += sample
                    saturated = array(
                        "h", (max(-32768, min(32767, value)) for value in mixed)
                    )
                    if sys.byteorder != "little":
                        saturated.byteswap()
                    writer.writeframesraw(saturated.tobytes())
                    remaining -= count
        finally:
            for reader in readers:
                reader.close()


def _wav_header(sample_rate: int, channels: int, data_bytes: int) -> bytes:
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_bytes, b"WAVE", b"fmt ", 16, 1, channels,
        sample_rate, byte_rate, block_align, 16, b"data", data_bytes,
    )


def _read_header(handle: BinaryIO) -> tuple[int, int, int]:
    handle.seek(0)
    data = handle.read(_WAV_HEADER_SIZE)
    if len(data) != _WAV_HEADER_SIZE:
        raise ValueError("incomplete WAV header")
    values = struct.unpack("<4sI4s4sIHHIIHH4sI", data)
    riff, _, wave_id, fmt, fmt_size, encoding, channels, rate, _, align, bits, data_id, size = values
    if (
        riff != b"RIFF" or wave_id != b"WAVE" or fmt != b"fmt "
        or fmt_size != 16 or encoding != 1 or bits != 16 or data_id != b"data"
        or align != channels * 2
    ):
        raise ValueError("unsupported or corrupt WAV header")
    return rate, channels, size


def _update_wav_sizes(handle: BinaryIO, data_bytes: int) -> None:
    position = handle.tell()
    handle.seek(4)
    handle.write(struct.pack("<I", 36 + data_bytes))
    handle.seek(40)
    handle.write(struct.pack("<I", data_bytes))
    handle.seek(position)
