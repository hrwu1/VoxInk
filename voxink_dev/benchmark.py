"""Measure the local inference path so latency claims rest on a number.

The caption experience the specs are organized around assumes transcription
keeps up with speech.  Nothing in the repository recorded whether it does, so
this runs the real provider over real audio and reports the real-time factor:
compute milliseconds per millisecond of audio.  Below 1.0 the pipeline keeps
up; above 1.0 it falls behind and the live path can only stay responsive by
discarding audio.

Chunk length is swept because the VAD emits utterances of very different sizes,
and a model's cost per second of audio is not constant across them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import sys
import tempfile
import time
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path

from voxink_core.models import Source, TranscriptionOptions
from voxink_core.providers.qwen import QwenLocalProvider

from .preflight import DEFAULT_MODEL_PATH


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIO = PROJECT_ROOT / "samples" / "asr_en.wav"
SAMPLE_RATE = 16_000


@dataclass(frozen=True, slots=True)
class ChunkResult:
    chunk_seconds: float
    runs: int
    median_ms: float
    p95_ms: float
    slowest_ms: float
    real_time_factor: float

    @property
    def keeps_up(self) -> bool:
        return self.real_time_factor < 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        nargs="+",
        default=[2.0, 5.0, 10.0, 15.0],
        help="utterance lengths to sweep (default: the VAD's usual range)",
    )
    parser.add_argument("--repeat", type=int, default=3, help="timed runs per chunk length")
    parser.add_argument("--json", type=Path, default=None, help="also write the results as JSON")
    return parser


def read_mono_16k(path: Path) -> bytes:
    """Return mono signed 16-bit little-endian PCM at the service's sample rate.

    Block averaging is a crude anti-alias filter, but this measures how long
    inference takes, not how well it transcribes, and it keeps the benchmark
    free of dependencies the service itself does not have.
    """
    with wave.open(str(path), "rb") as reader:
        if reader.getcomptype() != "NONE":
            raise ValueError(f"{path} must be uncompressed PCM")
        if reader.getnchannels() != 1:
            raise ValueError(f"{path} must be mono; it has {reader.getnchannels()} channels")
        width = reader.getsampwidth()
        rate = reader.getframerate()
        frames = reader.readframes(reader.getnframes())

    samples = _to_int16(frames, width)
    if rate == SAMPLE_RATE:
        return samples.tobytes()
    if rate % SAMPLE_RATE:
        raise ValueError(
            f"{path} is {rate} Hz, which is not a whole multiple of {SAMPLE_RATE} Hz; "
            "supply audio the benchmark can decimate without a resampler"
        )
    return _decimate(samples, rate // SAMPLE_RATE).tobytes()


def _to_int16(data: bytes, width: int) -> array:
    """Narrow 8-, 24- or 32-bit PCM to the 16-bit form the service streams."""
    if width == 2:
        samples = array("h")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples
    if width == 1:
        return array("h", ((byte - 128) << 8 for byte in data))
    if width == 3:
        return array(
            "h",
            (
                int.from_bytes(data[offset : offset + 3], "little", signed=True) >> 8
                for offset in range(0, len(data) - len(data) % 3, 3)
            ),
        )
    if width == 4:
        wide = array("i")
        wide.frombytes(data)
        if sys.byteorder != "little":
            wide.byteswap()
        return array("h", (value >> 16 for value in wide))
    raise ValueError(f"unsupported sample width: {width} bytes")


def _decimate(samples: array, factor: int) -> array:
    return array(
        "h",
        (
            sum(samples[index : index + factor]) // factor
            for index in range(0, len(samples) - len(samples) % factor, factor)
        ),
    )


def chunk_of(pcm: bytes, seconds: float) -> bytes:
    """Take `seconds` of audio, repeating the sample when it is too short."""
    needed = int(seconds * SAMPLE_RATE) * 2
    if not pcm:
        raise ValueError("the benchmark audio contains no samples")
    repeated = pcm * (needed // len(pcm) + 1)
    return repeated[:needed]


def _write_wav(pcm: bytes, path: str) -> None:
    with wave.open(path, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)


async def _time_chunk(provider: QwenLocalProvider, pcm: bytes, index: int) -> float:
    with tempfile.NamedTemporaryFile(suffix=".wav") as temporary:
        _write_wav(pcm, temporary.name)
        began = time.monotonic()
        await provider.transcribe_file(
            temporary.name,
            TranscriptionOptions(
                session_id="benchmark",
                segment_id=f"seg-{index:06d}",
                stream_id="microphone-1",
                source=Source.MICROPHONE,
            ),
        )
        return (time.monotonic() - began) * 1000.0


async def run_benchmark(args: argparse.Namespace) -> tuple[float, list[ChunkResult]]:
    pcm = read_mono_16k(args.audio.expanduser().resolve())
    provider = QwenLocalProvider(
        args.model, device=args.device, max_new_tokens=args.max_new_tokens
    )

    began = time.monotonic()
    await provider.load()
    load_seconds = time.monotonic() - began
    print(f"Model loaded in {load_seconds:.1f}s on device '{args.device}'.\n", flush=True)

    results: list[ChunkResult] = []
    index = 0
    for chunk_seconds in args.chunk_seconds:
        chunk = chunk_of(pcm, chunk_seconds)
        # One untimed pass per length: the first call at a new input size pays
        # for allocation and kernel selection that steady-state use does not.
        await _time_chunk(provider, chunk, index)
        index += 1
        timings = []
        for _ in range(max(1, args.repeat)):
            timings.append(await _time_chunk(provider, chunk, index))
            index += 1
        audio_ms = chunk_seconds * 1000.0
        median = statistics.median(timings)
        results.append(
            ChunkResult(
                chunk_seconds=chunk_seconds,
                runs=len(timings),
                median_ms=round(median, 1),
                p95_ms=round(_percentile(timings, 0.95), 1),
                slowest_ms=round(max(timings), 1),
                real_time_factor=round(median / audio_ms, 3),
            )
        )
        print(_row(results[-1]), flush=True)
    return load_seconds, results


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[position]


def _row(result: ChunkResult) -> str:
    verdict = "keeps up" if result.keeps_up else "FALLS BEHIND"
    return (
        f"{result.chunk_seconds:>5.1f}s audio  "
        f"median {result.median_ms:>9.1f} ms  "
        f"slowest {result.slowest_ms:>9.1f} ms  "
        f"RTF {result.real_time_factor:>6.2f}x  {verdict}"
    )


def _peak_memory_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports kibibytes.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return peak / divisor


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_seconds, results = asyncio.run(run_benchmark(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"Benchmark cannot run: {exc}", file=sys.stderr)
        return 2

    peak_mib = _peak_memory_mib()
    behind = [result for result in results if not result.keeps_up]
    print(f"\nPeak resident memory: {peak_mib:.0f} MiB")
    if behind:
        lengths = ", ".join(f"{result.chunk_seconds:g}s" for result in behind)
        print(
            f"Inference is slower than real time at {lengths}. Live captions on this "
            "device will lag or lose audio until the runtime changes."
        )
    else:
        print("Inference keeps up with real time at every measured chunk length.")

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "device": args.device,
                    "model": str(args.model),
                    "model_load_seconds": round(load_seconds, 2),
                    "peak_memory_mib": round(peak_mib, 1),
                    "chunks": [asdict(result) for result in results],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
