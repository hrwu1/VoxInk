"""Experiment: diarize first, then transcribe each speaker turn on its own.

The shipped pipeline transcribes chunker windows live and, after Stop, splits
each window between speakers using forced word alignment. This script tries
the other order on a finished session: take the speaker turns, cut the
retained microphone audio at them, and run Qwen on each single-speaker clip.

It is deliberately self-contained. It reads a session directory and writes
two files beside the transcript -- `diarize-then-asr.md` and
`diarize-then-asr.json` -- and nothing in the service, the worker, or the
derived speaker artifacts knows it exists. Delete this module, its test, and
those two files to remove the experiment entirely.

    .venv/bin/python -m voxink_dev.diarize_then_asr ~/Documents/VoxInk/<session>

Existing `speaker-turns.json` output is reused when present, so the usual
cost is one ASR pass over the speech in the session. Pass `--rediarize` to
run the diarization worker afresh instead.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence
import wave

from voxink_core.diarization.audio import assemble
from voxink_core.diarization.pyannote import OFFLINE_ENVIRONMENT
from voxink_core.models import Source, TranscriptionOptions
from voxink_core.persistence.markdown import format_timestamp
from voxink_core.providers.qwen import QwenLocalProvider
from voxink_core.vocabulary import VocabularyFile

from .preflight import DEFAULT_MODEL_PATH, PROJECT_ROOT


DEFAULT_DIARIZATION_MODEL = PROJECT_ROOT / "models" / "pyannote-speaker-diarization-community-1"
#: The service keeps the custom vocabulary beside the sessions under this name.
VOCABULARY_FILENAME = "vocabulary.txt"
MARKDOWN_NAME = "diarize-then-asr.md"
JSON_NAME = "diarize-then-asr.json"
SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class Turn:
    speaker_id: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Clip:
    """One single-speaker stretch of audio handed to the ASR model."""

    speaker_id: str
    start_ms: int
    end_ms: int
    #: The turns this clip was built from, before padding.
    turn_count: int


@dataclass
class ClipResult:
    speaker_id: str
    start_ms: int
    end_ms: int
    turn_count: int
    text: str
    language: str | None
    asr_ms: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session_dir", type=Path, help="A finished session directory.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--device", default="auto", choices=("auto", "mps", "cpu"))
    parser.add_argument("--diarization-model", type=Path, default=DEFAULT_DIARIZATION_MODEL)
    parser.add_argument(
        "--rediarize",
        action="store_true",
        help="Run diarization afresh even when speaker-turns.json exists.",
    )
    parser.add_argument(
        "--merge-gap-ms",
        type=int,
        default=500,
        help="Consecutive turns of one speaker closer than this become one clip.",
    )
    parser.add_argument(
        "--pad-ms",
        type=int,
        default=150,
        help="Audio added before and after each clip, never into a neighbour.",
    )
    parser.add_argument("--min-ms", type=int, default=300, help="Clips shorter than this are dropped.")
    parser.add_argument(
        "--max-ms",
        type=int,
        default=30_000,
        help="A longer stretch is cut at a turn boundary before reaching this.",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="Defaults to the session directory.")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    return parser


# -- the pure part -------------------------------------------------------------


def group_turns(
    turns: Sequence[Turn],
    *,
    total_ms: int,
    merge_gap_ms: int = 500,
    pad_ms: int = 150,
    min_ms: int = 300,
    max_ms: int = 30_000,
) -> tuple[Clip, ...]:
    """Turn exclusive speaker turns into single-speaker clips for the ASR.

    Consecutive turns of one speaker separated by less than `merge_gap_ms`
    are one clip, up to `max_ms`. Each clip is then padded by `pad_ms` on
    both sides, but never past the midpoint of the gap to a neighbouring clip
    of another speaker, so no clip carries a second voice by construction.
    """
    ordered = sorted(turns, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_id))
    merged: list[list[Turn]] = []
    for turn in ordered:
        if (
            merged
            and merged[-1][0].speaker_id == turn.speaker_id
            and turn.start_ms - merged[-1][-1].end_ms < merge_gap_ms
            and turn.end_ms - merged[-1][0].start_ms <= max_ms
        ):
            merged[-1].append(turn)
        else:
            merged.append([turn])
    raw = [
        Clip(group[0].speaker_id, group[0].start_ms, group[-1].end_ms, len(group))
        for group in merged
    ]
    clips: list[Clip] = []
    for index, clip in enumerate(raw):
        start = max(0, clip.start_ms - pad_ms)
        end = min(total_ms, clip.end_ms + pad_ms)
        if index > 0:
            previous = raw[index - 1]
            start = max(start, min(clip.start_ms, (previous.end_ms + clip.start_ms) // 2))
        if index + 1 < len(raw):
            following = raw[index + 1]
            end = min(end, max(clip.end_ms, (clip.end_ms + following.start_ms) // 2))
        if end - start >= min_ms:
            clips.append(Clip(clip.speaker_id, start, end, clip.turn_count))
    return tuple(clips)


def load_turns(document: dict[str, Any]) -> tuple[Turn, ...]:
    return tuple(
        Turn(item["speaker_id"], int(item["start_ms"]), int(item["end_ms"]))
        for item in document.get("exclusive_turns", ())
    )


# -- the run -------------------------------------------------------------------


def diarize(session_dir: Path, joined: Path, model_path: Path, device: str) -> dict[str, Any]:
    """Run the shipped isolated worker once, without the aligner stage."""
    payload = {
        "session_id": session_dir.name,
        "source": Source.MICROPHONE.value,
        "audio_path": str(joined),
        "model_path": str(model_path),
        "device": device,
    }
    completed = subprocess.run(
        [sys.executable, "-m", "voxink_core.diarization.worker"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **OFFLINE_ENVIRONMENT},
        cwd=PROJECT_ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"diarization worker failed: {completed.stderr.strip()[-300:]}")
    response = json.loads(completed.stdout)
    if not response.get("ok"):
        raise RuntimeError(f"diarization failed: {response.get('error')}")
    return response["result"]


async def transcribe_clips(
    joined: Path,
    clips: Sequence[Clip],
    *,
    model_path: Path,
    device: str,
    context: str | None,
    session_id: str,
    progress,
) -> list[ClipResult]:
    provider = QwenLocalProvider(model_path, device=device)
    await provider.load()
    results: list[ClipResult] = []
    with wave.open(str(joined), "rb") as reader, tempfile.TemporaryDirectory() as scratch:
        clip_path = Path(scratch) / "clip.wav"
        for index, clip in enumerate(clips):
            reader.setpos(clip.start_ms * SAMPLE_RATE // 1000)
            frames = reader.readframes((clip.end_ms - clip.start_ms) * SAMPLE_RATE // 1000)
            with wave.open(str(clip_path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(SAMPLE_RATE)
                writer.writeframes(frames)
            started = time.perf_counter()
            event = await provider.transcribe_file(
                clip_path,
                TranscriptionOptions(
                    session_id=session_id,
                    segment_id=f"clip-{index:05d}",
                    stream_id="microphone-diarize-first",
                    source=Source.MICROPHONE,
                    start_ms=clip.start_ms,
                    end_ms=clip.end_ms,
                    context=context,
                ),
            )
            results.append(
                ClipResult(
                    speaker_id=clip.speaker_id,
                    start_ms=clip.start_ms,
                    end_ms=clip.end_ms,
                    turn_count=clip.turn_count,
                    text=event.text,
                    language=event.language,
                    asr_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            progress(index + 1, len(clips))
    await provider.unload()
    return results


def render_markdown(session_id: str, results: Sequence[ClipResult], summary: dict[str, Any]) -> str:
    header = (
        "---\n"
        "experiment: diarize-then-asr\n"
        "derived: true\n"
        f'session_id: "{session_id}"\n'
        f'created_at: "{summary["created_at"]}"\n'
        f'asr_model: "{summary["asr_model"]}"\n'
        f"clips: {summary['clips']}\n"
        "---\n\n"
        "# Diarize first, then transcribe\n\n"
        "Experimental. Each block is one single-speaker clip cut at diarization\n"
        "turns and transcribed on its own; the text is a second ASR pass and\n"
        "will differ from the canonical transcript.\n\n"
    )
    blocks = []
    for item in results:
        if not item.text.strip():
            continue
        blocks.append(
            f"**[{format_timestamp(item.start_ms)}-{format_timestamp(item.end_ms)}] "
            f"[MIC] [{item.speaker_id}]**\n{item.text.strip()}\n\n"
        )
    return header + "".join(blocks)


def run(args: argparse.Namespace) -> dict[str, Any]:
    session_dir = args.session_dir.expanduser().resolve()
    if not session_dir.is_dir():
        raise SystemExit(f"not a session directory: {session_dir}")
    output_dir = (args.output_dir or session_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    scratch = Path(tempfile.mkdtemp(prefix="diarize-then-asr-"))
    try:
        joined = scratch / "microphone-joined.wav"
        total_ms = assemble(session_dir, joined)

        turns_path = session_dir / "speaker-turns.json"
        if turns_path.is_file() and not args.rediarize:
            document = json.loads(turns_path.read_text(encoding="utf-8"))
            turns_source = turns_path.name
        else:
            document = diarize(session_dir, joined, args.diarization_model, args.device)
            turns_source = "fresh diarization"
        turns = load_turns(document)
        clips = group_turns(
            turns,
            total_ms=total_ms,
            merge_gap_ms=args.merge_gap_ms,
            pad_ms=args.pad_ms,
            min_ms=args.min_ms,
            max_ms=args.max_ms,
        )
        speech_ms = sum(clip.end_ms - clip.start_ms for clip in clips)
        print(
            f"{len(turns)} turns from {turns_source} -> {len(clips)} clips, "
            f"{speech_ms / 1000:.0f}s of audio to transcribe",
            file=sys.stderr,
        )

        vocabulary = VocabularyFile(session_dir.parent / VOCABULARY_FILENAME)
        context = vocabulary.current().context

        def progress(done: int, total: int) -> None:
            if done % 25 == 0 or done == total:
                print(f"  {done}/{total} clips", file=sys.stderr)

        asr_started = time.perf_counter()
        results = asyncio.run(
            transcribe_clips(
                joined,
                clips,
                model_path=args.model,
                device=args.device,
                context=context,
                session_id=session_dir.name,
                progress=progress,
            )
        )
        asr_seconds = time.perf_counter() - asr_started
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    summary = {
        "session_id": session_dir.name,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asr_model": args.model.name,
        "device": args.device,
        "turns_source": turns_source,
        "turns": len(turns),
        "clips": len(clips),
        "empty_clips": sum(1 for item in results if not item.text.strip()),
        "speech_seconds": round(speech_ms / 1000, 1),
        "audio_seconds": round(total_ms / 1000, 1),
        "asr_seconds": round(asr_seconds, 1),
        "asr_real_time_factor": round(asr_seconds / (speech_ms / 1000), 3) if speech_ms else None,
        "total_seconds": round(time.perf_counter() - started, 1),
        "parameters": {
            "merge_gap_ms": args.merge_gap_ms,
            "pad_ms": args.pad_ms,
            "min_ms": args.min_ms,
            "max_ms": args.max_ms,
        },
    }
    (output_dir / MARKDOWN_NAME).write_text(
        render_markdown(session_dir.name, results, summary), encoding="utf-8"
    )
    (output_dir / JSON_NAME).write_text(
        json.dumps({"summary": summary, "clips": [asdict(item) for item in results]}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    summary["outputs"] = [str(output_dir / MARKDOWN_NAME), str(output_dir / JSON_NAME)]
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
