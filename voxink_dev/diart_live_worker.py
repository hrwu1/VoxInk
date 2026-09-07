"""Disposable, offline Diart worker for the opt-in microphone UI path.

Only this child imports Diart. JSON request/reply pipes contain bounded PCM
buffers; no recording or Qwen model is created here. Times on the wire are
absolute session milliseconds, including pauses in the incoming source.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

from .diart_turns import Span, TurnBuffer, context_window, partition

RATE = 16_000
STEP = 8_000
STEP_BYTES = STEP * 2
RING_BYTES = RATE * 2 * 15
MAX_REQUEST_BYTES = 32_768
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def validate_pcm(pcm: bytes, start_ms: int) -> None:
    if not isinstance(start_ms, int) or isinstance(start_ms, bool) or start_ms < 0:
        raise ValueError("audio start_ms must be a nonnegative integer")
    if not isinstance(pcm, bytes) or not pcm or len(pcm) > STEP_BYTES or len(pcm) % 32:
        raise ValueError("audio must be 1–500 ms of whole-millisecond mono 16kHz s16 PCM")


class StreamingTurns:
    """Timestamped, bounded turn/context buffering independent of ML imports.

    predictor(block) consumes exactly 500 ms (zero-padded only at flush), and
    returns local-time diarization tracks. reset() must clear rolling windows
    while preserving the existing speaker clustering state.
    """

    def __init__(self, predictor, reset):
        self.predictor, self.reset_predictor = predictor, reset
        self.last_end_ms = 0
        self._clear()

    def _clear(self):
        self.origin_ms: int | None = None
        self.received_ms = self.committed_ms = self.tick = 0
        self.pending = bytearray()
        self.ring = bytearray()
        self.ring_start_ms = 0
        self.turns = TurnBuffer(max_ms=4_000)

    def feed(self, pcm: bytes, start_ms: int) -> list[dict]:
        validate_pcm(pcm, start_ms)
        expected = self.origin_ms + self.received_ms if self.origin_ms is not None else self.last_end_ms
        if start_ms < expected - 1:
            raise ValueError("audio timestamps overlap or moved backwards")
        ready = []
        if self.origin_ms is not None and start_ms > expected + 1:
            ready.extend(self.flush())
        if self.origin_ms is None:
            self.origin_ms = start_ms
        self.pending.extend(pcm)
        self.ring.extend(pcm)
        self.received_ms += len(pcm) // 32
        self.last_end_ms = self.origin_ms + self.received_ms
        if len(self.ring) > RING_BYTES:
            excess = len(self.ring) - RING_BYTES
            del self.ring[:excess]
            self.ring_start_ms += excess // 32
        while len(self.pending) >= STEP_BYTES:
            block = bytes(self.pending[:STEP_BYTES])
            del self.pending[:STEP_BYTES]
            ready.extend(self._predict(block))
        return ready

    def _predict(self, block: bytes) -> list[dict]:
        tracks = self.predictor(block)
        self.tick += 1
        end_ms = min(self.received_ms, self.tick * 500 - 1_500)
        ready = []
        if end_ms > self.committed_ms:
            for span in partition(self.committed_ms, end_ms, tracks, edge_tolerance_ms=10):
                ready.extend(self._output(turn) for turn in self.turns.feed(span))
            self.committed_ms = end_ms
        return ready

    def _output(self, span: Span) -> dict:
        lo, hi = context_window(span, self.received_ms)
        if lo < self.ring_start_ms:
            raise RuntimeError("Diart context exceeded the bounded PCM ring")
        pcm = bytes(self.ring[(lo - self.ring_start_ms) * 32:(hi - self.ring_start_ms) * 32])
        if len(pcm) != (hi - lo) * 32:
            raise RuntimeError("Diart context is outside available source audio")
        return {"pcm": base64.b64encode(pcm).decode("ascii"),
                "start_ms": self.origin_ms + span.start_ms,
                "end_ms": self.origin_ms + span.end_ms,
                "audio_start_ms": self.origin_ms + lo,
                "audio_end_ms": self.origin_ms + hi,
                "speakers": list(span.speakers)}

    def flush(self) -> list[dict]:
        if self.origin_ms is None:
            return []
        ready = []
        if self.pending:
            ready.extend(self._predict(bytes(self.pending).ljust(STEP_BYTES, b"\0")))
            self.pending.clear()
        # At most four extra steps include every final real sample. Padding
        # informs the model of the end; it never becomes output audio.
        for _ in range(4):
            if self.committed_ms >= self.received_ms:
                break
            ready.extend(self._predict(bytes(STEP_BYTES)))
        if self.committed_ms != self.received_ms:
            raise RuntimeError("Diart failed to drain the current source segment")
        ready.extend(self._output(turn) for turn in self.turns.flush())
        self.reset_predictor()
        self._clear()
        return ready


class DiartPredictor:
    def __init__(self, runtime: Path, device: str):
        from .diart_runtime import model_paths
        import numpy as np
        import torch
        from diart import SpeakerDiarization, SpeakerDiarizationConfig
        from diart.models import SegmentationModel, EmbeddingModel
        from pyannote.core import SlidingWindow, SlidingWindowFeature

        torch.set_num_threads(2)
        paths = model_paths(runtime)  # Check exact manifested copies before loading.
        self.device = ("mps" if torch.backends.mps.is_available() else "cpu") if device == "auto" else device
        self.np, self.SlidingWindow, self.Feature = np, SlidingWindow, SlidingWindowFeature
        self.pipeline = SpeakerDiarization(SpeakerDiarizationConfig(
            segmentation=SegmentationModel.from_pretrained(str(paths["segmentation"]), use_hf_token=False),
            embedding=EmbeddingModel.from_pretrained(str(paths["embedding"]), use_hf_token=False),
            duration=5, step=.5, latency=2, device=torch.device(self.device),
        ))
        self.reset()
        # Constructors load both model objects. No capture should start until
        # an actual warmup forward pass has also succeeded on the chosen device.
        self(bytes(STEP_BYTES))
        self.pipeline.reset()
        self.reset()

    def reset(self):
        # Diart 0.9.2 reset() replaces clustering too. Clear just the two
        # aggregation buffers between pauses, retaining the speaker centroids.
        self.pipeline.chunk_buffer.clear()
        self.pipeline.pred_buffer.clear()
        self.pipeline.set_timestamp_shift(0)
        self.rolling = self.np.zeros(5 * RATE, dtype=self.np.float32)
        self.tick = 0

    def __call__(self, block: bytes) -> list[Span]:
        samples = self.np.frombuffer(block, dtype="<i2").astype(self.np.float32) / 32768
        self.rolling[:-STEP] = self.rolling[STEP:]
        self.rolling[-STEP:] = samples
        self.tick += 1
        # Each stored pipeline waveform needs its own copy: later ring updates
        # must not mutate an earlier aggregation window.
        feature = self.Feature(self.rolling[:, None].copy(), self.SlidingWindow(
            start=self.tick * .5 - 5, duration=1 / RATE, step=1 / RATE))
        annotation, _ = self.pipeline([feature])[0]
        tracks = []
        for segment, _, label in annotation.itertracks(yield_label=True):
            lo, hi = max(0, round(segment.start * 1000)), round(segment.end * 1000)
            if hi > lo:
                tracks.append(Span(lo, hi, (f"microphone:{label}",)))
        return tracks


def serve(stream: StreamingTurns, source, output) -> None:
    while True:
        line = source.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            return
        request_id = None
        try:
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise ValueError("Diart request exceeded bounded IPC limit")
            request = json.loads(line)
            request_id = request["id"]
            if request["op"] == "feed":
                turns = stream.feed(base64.b64decode(request["pcm"], validate=True), request["start_ms"])
            elif request["op"] == "flush":
                turns = stream.flush()
            else:
                raise ValueError("Unknown Diart request operation")
            response = {"id": request_id, "turns": turns}
        except Exception as exc:
            response = {"id": request_id, "error": f"{type(exc).__name__}: {exc}"}
        encoded = json.dumps(response, separators=(",", ":")) + "\n"
        if len(encoded) > MAX_RESPONSE_BYTES:
            response = {"id": request_id, "error": "Diart response exceeded bounded IPC limit"}
            encoded = json.dumps(response) + "\n"
        output.write(encoded)
        output.flush()
        if "error" in response:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--device", choices=("auto", "cpu", "mps"), default="auto")
    args = parser.parse_args()
    from .diart_runtime import runtime_environment, validate_runtime
    validate_runtime(args.runtime)
    environment = runtime_environment(args.runtime)
    os.environ.clear()
    os.environ.update(environment)
    output = sys.stdout
    try:
        # Third-party model load/inference logging must never corrupt IPC.
        with redirect_stdout(sys.stderr):
            predictor = DiartPredictor(args.runtime, args.device)
            output.write(json.dumps({"ready": True, "device": predictor.device}) + "\n")
            output.flush()
            serve(StreamingTurns(predictor, predictor.reset), sys.stdin.buffer, output)
    except Exception as exc:
        output.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n")
        output.flush()
        raise


if __name__ == "__main__":
    main()
