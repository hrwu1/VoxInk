"""Deterministic audio fixtures for voice-activity tests.

The VAD gates on frame RMS, so what these fixtures have to reproduce is an
amplitude envelope at a known level, not spectral realism.  Everything here is
generated from a seeded PRNG or derived from the repository's speech sample, so
a level regression fails the same way on every machine.

Levels are given in dBFS relative to a full-scale 16-bit sample, because that
is how the capture problem was measured: a session peaking at -19.8 dBFS lost
most of its frames to a gate at RMS 500, which is -36.3 dBFS.
"""

from __future__ import annotations

from array import array
import math
from pathlib import Path
import random
import wave

SAMPLE_RATE_HZ = 16_000
FRAME_MS = 20
FULL_SCALE = 32_768.0

_SAMPLE_WAV = Path(__file__).resolve().parents[2] / "samples" / "asr_en.wav"


def dbfs_to_amplitude(dbfs: float) -> float:
    """Return the linear 16-bit amplitude for a level in dBFS."""
    return FULL_SCALE * (10.0 ** (dbfs / 20.0))


def amplitude_to_dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -math.inf
    return 20.0 * math.log10(amplitude / FULL_SCALE)


def to_pcm(samples: list[float]) -> bytes:
    """Clamp and pack float samples as signed 16-bit little-endian PCM."""
    packed = array("h", (int(max(-32768, min(32767, round(value)))) for value in samples))
    if array("h", [1]).tobytes() != b"\x01\x00":
        packed.byteswap()
    return packed.tobytes()


def frames_of(pcm: bytes, frame_ms: int = FRAME_MS) -> list[bytes]:
    frame_bytes = SAMPLE_RATE_HZ * frame_ms // 1000 * 2
    return [pcm[index : index + frame_bytes] for index in range(0, len(pcm) - frame_bytes + 1, frame_bytes)]


def rms_of(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm)
    if array("h", [1]).tobytes() != b"\x01\x00":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def peak_dbfs(pcm: bytes) -> float:
    samples = array("h")
    samples.frombytes(pcm)
    if array("h", [1]).tobytes() != b"\x01\x00":
        samples.byteswap()
    return amplitude_to_dbfs(max(abs(sample) for sample in samples)) if samples else -math.inf


def silence(duration_ms: int) -> bytes:
    """Digital silence: every sample exactly zero."""
    return bytes(SAMPLE_RATE_HZ * duration_ms // 1000 * 2)


def room_tone(duration_ms: int, *, rms_dbfs: float = -60.0, seed: int = 7) -> bytes:
    """Steady broadband background at a known level, with no speech in it."""
    rng = random.Random(seed)
    target = dbfs_to_amplitude(rms_dbfs)
    # Uniform noise on [-1, 1] has RMS 1/sqrt(3); scale so the result lands on
    # the requested level rather than near it.
    scale = target * math.sqrt(3.0)
    count = SAMPLE_RATE_HZ * duration_ms // 1000
    return to_pcm([rng.uniform(-1.0, 1.0) * scale for _ in range(count)])


def speech_like(
    *,
    peak_dbfs_level: float = -20.0,
    seed: int = 11,
    floor_dbfs: float = -60.0,
) -> bytes:
    """A syllable envelope over noise, peaking at the requested level.

    Real speech is not one amplitude.  Vowels carry the peak, fricatives and
    word endings sit far below it, and those quiet parts are exactly what a
    fixed gate discards.  The envelope here spans roughly 18 dB from loudest
    vowel to quietest fricative, which is ordinary for conversational speech.
    """
    rng = random.Random(seed)
    peak = dbfs_to_amplitude(peak_dbfs_level)
    floor = dbfs_to_amplitude(floor_dbfs)
    samples: list[float] = []

    def add(duration_ms: int, amplitude: float) -> None:
        count = SAMPLE_RATE_HZ * duration_ms // 1000
        for index in range(count):
            # A raised-cosine envelope avoids clicks that would read as onsets.
            window = 0.5 - 0.5 * math.cos(2.0 * math.pi * index / max(1, count - 1))
            samples.append(rng.uniform(-1.0, 1.0) * (amplitude * window + floor))

    # loud vowel, quiet fricative, medium vowel, very quiet word ending
    for relative_db in (0.0, -14.0, -4.0, -18.0, -2.0, -16.0):
        add(180, peak * (10.0 ** (relative_db / 20.0)))
        add(40, 0.0)
    return to_pcm(samples)


def load_speech_sample(*, peak_dbfs_level: float | None = None) -> bytes:
    """Return the repository speech sample as 16 kHz signed 16-bit mono PCM.

    `samples/asr_en.wav` is 48 kHz 24-bit, so it is decimated by three with
    averaging rather than committing a second copy of the same recording.
    """
    with wave.open(str(_SAMPLE_WAV)) as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        raw = source.readframes(source.getnframes())

    step = width * channels
    values: list[float] = []
    for offset in range(0, len(raw) - step + 1, step):
        chunk = raw[offset : offset + width]
        values.append(float(int.from_bytes(chunk, "little", signed=True)) / (1 << (8 * width - 1)))

    factor = rate // SAMPLE_RATE_HZ
    decimated = [
        sum(values[index : index + factor]) / factor for index in range(0, len(values) - factor + 1, factor)
    ]

    scale = FULL_SCALE
    if peak_dbfs_level is not None:
        observed = max(abs(value) for value in decimated) or 1.0
        scale = dbfs_to_amplitude(peak_dbfs_level) / observed
    return to_pcm([value * scale for value in decimated])


def energy_coverage(pcm: bytes, chunks) -> float:
    """Fraction of the signal's total energy that the emitted chunks contain.

    Coverage is the question the transcript failure actually asked: not how
    many chunks were produced, but whether the words were inside them.
    """
    samples = array("h")
    samples.frombytes(pcm)
    if array("h", [1]).tobytes() != b"\x01\x00":
        samples.byteswap()
    total = sum(float(sample) * sample for sample in samples)
    if total <= 0:
        return 0.0
    covered = 0.0
    for chunk in chunks:
        start = max(0, min(len(samples), chunk.start_sample))
        end = max(start, min(len(samples), chunk.end_sample))
        covered += sum(float(samples[index]) * samples[index] for index in range(start, end))
    return covered / total
