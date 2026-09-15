"""Converts capture buffers to the service's 16 kHz mono signed 16-bit PCM.

The counterpart of `PCMNormalizer.swift`. The resampler keeps its filter
history and fractional phase across buffers: restarting it at every buffer
boundary leaves audible discontinuities in the 48 kHz to 16 kHz path.

Each capture source owns its own normalizer and drives it from a single
callback thread, so no locking is required here.
"""

from __future__ import annotations

from typing import Any

from .models import CaptureError


def require_numpy() -> Any:
    try:
        import numpy  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CaptureError.conversion_failed(
            "audio normalization requires 'numpy'; install requirements-capture.txt"
        ) from exc
    return numpy


class PCMNormalizer:
    # A short windowed-sinc low-pass ahead of the decimating interpolation.
    # 63 taps at 48 kHz is a 1.3 ms delay, constant across buffers.
    FILTER_TAPS = 63

    def __init__(self, output_sample_rate: int = 16_000) -> None:
        if output_sample_rate <= 0:
            raise ValueError("output sample rate must be positive")
        self.output_sample_rate = output_sample_rate
        self._np = require_numpy()
        self._input_rate: int | None = None
        self._filter: Any = None
        self._history: Any = None
        self._carry: Any = None
        self._phase = 0.0

    def normalize(self, samples: Any, sample_rate: int) -> bytes:
        """`samples` is an array shaped (frames,) or (frames, channels) of
        int16, int32, float32, or float64; returns s16le mono bytes at the
        output rate."""
        np = self._np
        if sample_rate <= 0:
            raise CaptureError.audio_format_unavailable(f"sample rate {sample_rate}")
        array = np.asarray(samples)
        if array.ndim == 0 or array.size == 0:
            return b""
        mono = self._to_mono_float(array)
        if sample_rate != self._input_rate:
            self._reset(sample_rate)
        if sample_rate == self.output_sample_rate:
            resampled = mono
        else:
            resampled = self._resample(mono)
        if resampled.size == 0:
            return b""
        clipped = np.clip(np.rint(resampled * 32768.0), -32768.0, 32767.0)
        return clipped.astype("<i2").tobytes()

    def _to_mono_float(self, array: Any) -> Any:
        np = self._np
        if array.ndim > 2:
            raise CaptureError.audio_format_unavailable(f"{array.ndim}-dimensional buffer")
        if array.dtype.kind == "i":
            scale = float(np.iinfo(array.dtype).max) + 1.0
            floats = array.astype(np.float64) / scale
        elif array.dtype.kind == "f":
            floats = array.astype(np.float64)
        else:
            raise CaptureError.audio_format_unavailable(f"sample type {array.dtype}")
        if floats.ndim == 2:
            if floats.shape[1] == 0:
                raise CaptureError.audio_format_unavailable("zero channels")
            floats = floats.mean(axis=1)
        return floats

    def _reset(self, sample_rate: int) -> None:
        np = self._np
        self._input_rate = sample_rate
        self._phase = 0.0
        self._carry = None
        if sample_rate > self.output_sample_rate:
            taps = self.FILTER_TAPS
            # Cut off a little below the output Nyquist so the transition band
            # of a short filter does not fold back as aliasing.
            cutoff = 0.45 * self.output_sample_rate / sample_rate
            n = np.arange(taps) - (taps - 1) / 2.0
            kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.hamming(taps)
            self._filter = kernel / kernel.sum()
            self._history = np.zeros(taps - 1)
        else:
            self._filter = None
            self._history = None

    def _resample(self, mono: Any) -> Any:
        np = self._np
        assert self._input_rate is not None
        if self._filter is not None:
            padded = np.concatenate((self._history, mono))
            filtered = np.convolve(padded, self._filter, mode="valid")
            self._history = padded[-(self._filter.size - 1):]
        else:
            filtered = mono
        if self._carry is None:
            # The first sample anchors the interpolation; nothing precedes it.
            self._carry = filtered[:1]
            filtered = filtered[1:]
            if filtered.size == 0:
                return np.empty(0)
        chain = np.concatenate((self._carry, filtered))
        last = chain.size - 1
        step = self._input_rate / self.output_sample_rate
        count = int((last - self._phase) // step) + 1 if self._phase <= last else 0
        if count <= 0:
            self._carry = chain[-1:]
            self._phase -= last
            return np.empty(0)
        positions = self._phase + step * np.arange(count)
        output = np.interp(positions, np.arange(chain.size), chain)
        self._phase = positions[-1] + step - last
        self._carry = chain[-1:]
        return output
