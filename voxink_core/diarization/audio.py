"""Assemble retained microphone parts into one timeline for the diarizer.

Community-1 clusters better with the whole session in front of it, so rotated
parts are joined rather than diarized one at a time. Only microphone audio is
accepted: microphone and system tracks can contain the same remote voice a few
milliseconds apart, and mixing them invents echoes, duplicate speakers, and
overlap that never happened.
"""

from __future__ import annotations

from pathlib import Path
import wave

from ..models import Source
from .models import DiarizationError, DiarizationErrorCode


EXPECTED_SAMPLE_RATE = 16_000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2

_COPY_FRAMES = 1 << 16


def source_parts(session_dir: str | Path, source: Source = Source.MICROPHONE) -> tuple[Path, ...]:
    """Retained WAV parts for one source, ordered by their numeric suffix."""
    directory = Path(session_dir)
    prefix = f"{Source(source).value}-part-"
    return tuple(
        sorted(
            directory.glob(f"{prefix}[0-9][0-9][0-9].wav"),
            key=lambda path: int(path.stem.removeprefix(prefix)),
        )
    )


def assemble(
    session_dir: str | Path,
    destination: str | Path,
    *,
    source: Source = Source.MICROPHONE,
) -> int:
    """Join a session's retained parts into one WAV; return its duration in ms.

    Part numbers are only a position on the session clock when they start at
    001 and run unbroken. `AudioRetentionWriter` offsets numbering by a base it
    chooses at runtime and never persists, so a directory holding a second
    run's parts would otherwise be concatenated into a timeline that no longer
    lines up with the transcript. That case is rejected instead of guessed at.
    """
    if Source(source) is not Source.MICROPHONE:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            f"version one diarizes microphone audio only, not {Source(source).value}",
        )
    parts = source_parts(session_dir, source)
    if not parts:
        raise DiarizationError(
            DiarizationErrorCode.AUDIO_UNAVAILABLE,
            "no retained microphone audio for this session; "
            "diarization needs audio_saved and save_separate_audio",
        )
    _require_contiguous_from_first(parts)
    frame_counts = _validate_formats(parts)
    _require_uniform_interior(parts, frame_counts)

    output = Path(destination)
    total_frames = 0
    with wave.open(str(output), "wb") as writer:
        writer.setnchannels(EXPECTED_CHANNELS)
        writer.setsampwidth(EXPECTED_SAMPLE_WIDTH)
        writer.setframerate(EXPECTED_SAMPLE_RATE)
        for part in parts:
            with wave.open(str(part), "rb") as reader:
                while True:
                    frames = reader.readframes(_COPY_FRAMES)
                    if not frames:
                        break
                    writer.writeframesraw(frames)
                    total_frames += len(frames) // (
                        EXPECTED_CHANNELS * EXPECTED_SAMPLE_WIDTH
                    )
    if total_frames <= 0:
        output.unlink(missing_ok=True)
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            "retained microphone audio contains no samples",
        )
    return total_frames * 1000 // EXPECTED_SAMPLE_RATE


def _part_number(path: Path) -> int:
    return int(path.stem.split("-part-")[-1])


def _require_contiguous_from_first(parts: tuple[Path, ...]) -> None:
    numbers = [_part_number(path) for path in parts]
    if numbers[0] != 1:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            f"retained audio starts at part {numbers[0]:03d}, not 001, so its "
            "position on the session clock cannot be established",
            first_part=parts[0].name,
        )
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        missing = sorted(set(expected) - set(numbers))
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            f"retained audio is missing part(s) {', '.join(f'{n:03d}' for n in missing)}",
        )


def _validate_formats(parts: tuple[Path, ...]) -> list[int]:
    frame_counts: list[int] = []
    for part in parts:
        try:
            with wave.open(str(part), "rb") as reader:
                rate = reader.getframerate()
                channels = reader.getnchannels()
                width = reader.getsampwidth()
                compression = reader.getcomptype()
                frame_counts.append(reader.getnframes())
        except (OSError, wave.Error) as exc:
            raise DiarizationError(
                DiarizationErrorCode.INVALID_AUDIO,
                f"retained audio could not be read: {exc}",
                part=part.name,
            ) from exc
        if (
            rate != EXPECTED_SAMPLE_RATE
            or channels != EXPECTED_CHANNELS
            or width != EXPECTED_SAMPLE_WIDTH
            or compression != "NONE"
        ):
            raise DiarizationError(
                DiarizationErrorCode.INVALID_AUDIO,
                f"expected {EXPECTED_SAMPLE_RATE} Hz mono 16-bit PCM, found "
                f"{rate} Hz {channels}ch {width * 8}-bit {compression}",
                part=part.name,
            )
    return frame_counts


def _require_uniform_interior(parts: tuple[Path, ...], frame_counts: list[int]) -> None:
    """Every part but the last must be full, or the joined timeline shifts.

    Rotation happens on a fixed frame boundary, so a short interior part means
    audio is missing from the middle of the session. Concatenating anyway would
    slide everything after it earlier and silently misalign every later
    transcript block.
    """
    if len(frame_counts) < 2:
        return
    interior = frame_counts[:-1]
    full = max(interior)
    short = [
        parts[index].name
        for index, count in enumerate(interior)
        if count != full
    ]
    if short:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            f"retained audio part(s) {', '.join(short)} are shorter than the "
            "rotation boundary, so the joined timeline would not match the "
            "transcript",
        )
    if frame_counts[-1] > full:
        raise DiarizationError(
            DiarizationErrorCode.INVALID_AUDIO,
            f"retained audio part {parts[-1].name} is longer than the rotation boundary",
        )
