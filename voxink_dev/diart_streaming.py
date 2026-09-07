"""Opt-in Diart -> bounded turn queue -> one Qwen worker streaming trial.

Run with the disposable Diart Python. Outputs are provisional experiment
artifacts, never canonical VoxInk captions. See the experiment guideline.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import queue
import resource
import select
import subprocess
import sys
import threading
import time
import wave

from .diart_turns import Span, TurnBuffer, context_window, partition

RATE = 16_000
STEP = 8_000
PROJECT = Path(__file__).resolve().parents[1]
SOURCE_PROJECT = Path("/path/to/VoxInk")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--runtime", type=Path, required=True)
    inputs = p.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--wav", type=Path, help="16kHz mono PCM s16 WAV; read-only")
    inputs.add_argument("--microphone", action="store_true")
    p.add_argument("--source", choices=("microphone", "system"), required=True)
    p.add_argument("--session-id", required=True)
    p.add_argument("--stream-id", default="diart-experiment")
    p.add_argument("--file-offset-s", type=float, default=0)
    p.add_argument("--session-offset-s", type=float, default=0,
                   help="Session time at the beginning of this WAV part")
    p.add_argument("--duration-s", type=float, default=60)
    p.add_argument("--microphone-device", type=int)
    p.add_argument("--unpaced", action="store_true", help="Throughput diagnostic, not timed streaming proof")
    p.add_argument("--latency-s", type=float, default=2)
    p.add_argument("--max-turn-s", type=float, default=8)
    p.add_argument("--queue-size", type=int, default=4)
    p.add_argument("--device", choices=("cpu", "mps"), default="cpu")
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--tau-active", type=float, default=.6)
    p.add_argument("--rho-update", type=float, default=.3)
    p.add_argument("--delta-new", type=float, default=1)
    p.add_argument("--qwen-device", choices=("auto", "mps", "cpu"), default="auto")
    p.add_argument("--qwen-python", type=Path, default=SOURCE_PROJECT / ".venv/bin/python")
    p.add_argument("--qwen-model", type=Path, default=SOURCE_PROJECT / "models/Qwen3-ASR-0.6B")
    p.add_argument("--context", default="", help="Optional ASR vocabulary context")
    p.add_argument("--run-name", default=time.strftime("%Y%m%d-%H%M%S"))
    return p


def emit(handle, event: dict) -> None:
    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    handle.flush()


def receive(process: subprocess.Popen, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    buffer = getattr(process, "_diart_response_buffer", b"")
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise TimeoutError("Qwen worker response timed out; inspect qwen.stderr.log")
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError("Qwen worker exited; inspect qwen.stderr.log")
        buffer += chunk
        if len(buffer) > 1_000_000:
            raise RuntimeError("Qwen worker response exceeded experiment IPC limit")
    line, process._diart_response_buffer = buffer.split(b"\n", 1)
    return json.loads(line)


@contextmanager
def qwen_worker(args, run: Path, environment: dict):
    with (run / "qwen.stderr.log").open("w") as log:
        process = subprocess.Popen(
            [str(args.qwen_python), "-m", "voxink_dev.diart_qwen_worker", "--model",
             str(args.qwen_model), "--device", args.qwen_device],
            cwd=PROJECT, env={**environment, "PYTHONPATH": str(PROJECT)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1,
        )
        try:
            if not receive(process).get("ready"):
                raise RuntimeError("Qwen worker did not become ready")
            yield process
        finally:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()


def wav_blocks(args):
    with wave.open(str(args.wav), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getcomptype()) != (RATE, 1, 2, "NONE"):
            raise ValueError("WAV must be 16000 Hz mono 16-bit PCM; never implicitly mix sources/channels")
        offset = round(args.file_offset_s * RATE)
        if offset >= reader.getnframes():
            raise ValueError("file offset lies beyond WAV audio")
        reader.setpos(offset)
        remaining = min(round(args.duration_s * RATE), reader.getnframes() - offset)
        while remaining:
            frames = reader.readframes(min(STEP, remaining))
            if not frames or len(frames) % 2:
                raise ValueError("WAV ended before its declared PCM samples")
            remaining -= len(frames) // 2
            yield frames


def microphone_blocks(args, timestamp_path: Path):
    import sounddevice as sd
    pending: queue.Queue = queue.Queue(maxsize=8)
    overflow = threading.Event()

    def callback(data, frames, timing, status):
        if status:
            overflow.set()
        try:
            pending.put_nowait((bytes(data), time.monotonic_ns(), float(timing.inputBufferAdcTime)))
        except queue.Full:
            overflow.set()

    remaining = round(args.duration_s * RATE)
    samples_seen = 0
    with timestamp_path.open("w") as timestamps, sd.RawInputStream(samplerate=RATE, blocksize=STEP, device=args.microphone_device,
                           channels=1, dtype="int16", callback=callback):
        while remaining:
            block, callback_ns, adc_time = pending.get(timeout=5)
            if overflow.is_set():
                raise RuntimeError("microphone overflow: experiment stopped instead of silently losing input")
            block = block[:remaining * 2]
            emit(timestamps, {"source": "microphone", "stream_id": args.stream_id,
                              "start_sample": samples_seen, "sample_count": len(block) // 2,
                              "callback_monotonic_ns": callback_ns, "input_buffer_adc_s": adc_time})
            samples_seen += len(block) // 2
            remaining -= len(block) // 2
            yield block


class CaptionQueue:
    def __init__(self, args, run, worker, clock_start, offset_ms):
        self.args, self.run, self.worker = args, run, worker
        self.clock_start, self.offset_ms = clock_start, offset_ms
        self.pending: queue.Queue = queue.Queue(maxsize=args.queue_size)
        self.error: Exception | None = None
        self.cancelled = threading.Event()
        self.results: list[dict] = []
        self.maximum_depth = 0
        self.blocked_s = 0.0
        self.thread = threading.Thread(target=self.consume, name="diart-qwen-queue", daemon=True)
        self.thread.start()

    def submit(self, span, available_ms):
        if self.cancelled.is_set():
            raise RuntimeError("caption queue cancelled")
        if self.error:
            raise self.error
        lo, hi = context_window(span, available_ms)
        job = {**asdict(span), "audio_start_ms": lo, "audio_end_ms": hi,
               "enqueued_s": time.monotonic() - self.clock_start}
        before = time.monotonic()
        while True:
            if self.cancelled.is_set():
                raise RuntimeError("caption queue cancelled")
            if self.error:
                raise self.error
            try:
                self.pending.put(job, timeout=.1)
                break
            except queue.Full:
                pass
        self.blocked_s += time.monotonic() - before
        self.maximum_depth = max(self.maximum_depth, self.pending.qsize())

    def consume(self):
        try:
            with (self.run / "captions.jsonl").open("w") as output, (self.run / "input.s16le").open("rb") as audio:
                while not self.cancelled.is_set():
                    try:
                        job = self.pending.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if job is None:
                        break
                    index = len(self.results)
                    name = f"turn-{index:05d}"
                    clip = self.run / "clips" / f"{name}.wav"
                    lo, hi = job["audio_start_ms"], job["audio_end_ms"]
                    audio.seek(lo * RATE // 1000 * 2)
                    frames = audio.read((hi - lo) * RATE // 1000 * 2)
                    if len(frames) != (hi - lo) * RATE // 1000 * 2:
                        raise RuntimeError("audio spool does not cover the requested caption context")
                    with wave.open(str(clip), "wb") as writer:
                        writer.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                        writer.writeframes(frames)
                    request = {"id": name, "path": str(clip), "session_id": self.args.session_id,
                               "stream_id": self.args.stream_id, "source": self.args.source,
                               "start_ms": lo + self.offset_ms, "end_ms": hi + self.offset_ms,
                               "context": self.args.context}
                    started = time.monotonic() - self.clock_start
                    self.worker.stdin.write(json.dumps(request) + "\n")
                    self.worker.stdin.flush()
                    reply = receive(self.worker)
                    if "error" in reply or reply.get("id") != name:
                        raise RuntimeError(f"Qwen caption failed: {reply.get('error', 'invalid response ID')}")
                    now = time.monotonic() - self.clock_start
                    result = {**job, **reply, "session_id": self.args.session_id,
                              "stream_id": self.args.stream_id, "source": self.args.source,
                              "start_ms": job["start_ms"] + self.offset_ms,
                              "end_ms": job["end_ms"] + self.offset_ms,
                              "audio_start_ms": lo + self.offset_ms, "audio_end_ms": hi + self.offset_ms,
                              "provisional": True, "word_attribution": "unverified",
                              "overlap": len(job["speakers"]) > 1,
                              "context_extends_turn": lo < job["start_ms"] or hi > job["end_ms"],
                              "caption_elapsed_s": now, "queue_wait_s": started - job["enqueued_s"],
                              "caption_latency_s": None if self.args.unpaced else now - job["end_ms"] / 1000,
                              "caption_delay_from_start_s": None if self.args.unpaced else now - job["start_ms"] / 1000,
                              "short_turn": job["end_ms"] - job["start_ms"] < 1000}
                    self.results.append(result)
                    emit(output, result)
                    print(json.dumps({"caption": result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            self.error = exc

    def finish(self):
        while self.thread.is_alive():
            try:
                self.pending.put(None, timeout=.1)
                break
            except queue.Full:
                if self.error:
                    raise self.error
        self.thread.join(timeout=180 * (self.args.queue_size + 1))
        if self.thread.is_alive():
            raise TimeoutError("caption queue did not drain")
        if self.error:
            raise self.error

    def abort(self):
        """Stop this experiment's child and join before its pipes are closed."""
        self.cancelled.set()
        if self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("caption consumer did not stop after worker termination")


def percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)] if values else None


def run(args) -> Path:
    from .diart_runtime import runtime_environment, validate_runtime, model_paths, record_action, save_manifest
    validate_runtime(args.runtime)
    args.runtime = args.runtime.absolute()
    if (args.runtime / "runs").is_symlink():
        raise ValueError("experiment runs directory must not be a symlink")
    environment = {**runtime_environment(args.runtime), "PYTHONDONTWRITEBYTECODE": "1",
                   "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                   "PYANNOTE_METRICS_ENABLED": "0", "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    os.environ.clear()
    os.environ.update(environment)
    import numpy as np
    import torch
    from diart import SpeakerDiarization, SpeakerDiarizationConfig
    from diart.models import SegmentationModel, EmbeddingModel
    from pyannote.core import SlidingWindow, SlidingWindowFeature
    torch.set_num_threads(args.threads)

    if Path(args.run_name).name != args.run_name or args.run_name in {".", ".."}:
        raise ValueError("run name must be a simple directory name")
    target = args.runtime / "runs" / args.run_name
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    (target / "clips").mkdir()
    record_action(args.runtime, "streaming-run", run=str(target), arguments={
        k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}, status="started")
    manifest = validate_runtime(args.runtime)
    manifest["read_only_assets"] = sorted(set(manifest.get("read_only_assets", [])) | {
        str(args.qwen_python), str(args.qwen_model), *([str(args.wav)] if args.wav else [])})
    save_manifest(args.runtime, manifest)
    paths = model_paths(args.runtime)
    started = time.monotonic()
    pipeline = SpeakerDiarization(SpeakerDiarizationConfig(
        segmentation=SegmentationModel.from_pretrained(str(paths["segmentation"]), use_hf_token=False),
        embedding=EmbeddingModel.from_pretrained(str(paths["embedding"]), use_hf_token=False),
        duration=5, step=.5, latency=args.latency_s, device=torch.device(args.device),
        tau_active=args.tau_active, rho_update=args.rho_update, delta_new=args.delta_new,
    ))
    metadata = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    metadata.update({"diart_load_s": time.monotonic() - started,
                     "speaker_labels": "session-local, provisional", "word_attribution": "unverified",
                     "models": {k: str(v) for k, v in paths.items()},
                     "input_encoding": "pcm_s16le/16000/mono", "input_saved": "input.s16le",
                     "reference_is_ground_truth": False})
    (target / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
    offset_ms = round((args.session_offset_s + args.file_offset_s) * 1000)
    with (target / "input.s16le").open("wb", buffering=0) as spool, qwen_worker(args, target, environment) as worker:
        origin = time.monotonic()
        captions = CaptionQueue(args, target, worker, origin, offset_ms)
        turns = TurnBuffer(round(args.max_turn_s * 1000))
        rolling = np.zeros(5 * RATE, dtype=np.float32)
        received = 0
        tick = 0
        committed_ms = 0
        inference_s: list[float] = []
        input_lag_s: list[float] = []
        spans: list[Span] = []
        blocks = microphone_blocks(args, target / "capture-timestamps.jsonl") if args.microphone else wav_blocks(args)
        with (target / "diart.jsonl").open("w") as annotations, (target / "spans.jsonl").open("w") as span_log:
            def predict(block: bytes, *, tail: bool = False):
                nonlocal received, tick, committed_ms, rolling
                if not tail:
                    spool.write(block)
                    received += len(block) // 2
                    if not args.unpaced and not args.microphone:
                        time.sleep(max(0, origin + received / RATE - time.monotonic()))
                if tail and not args.unpaced:
                    time.sleep(max(0, origin + (tick + 1) * .5 - time.monotonic()))
                samples = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768
                samples = np.pad(samples, (0, STEP - len(samples)))
                rolling = np.concatenate((rolling[STEP:], samples))
                tick += 1
                window_end = tick * .5
                feature = SlidingWindowFeature(rolling[:, None], SlidingWindow(
                    start=window_end - 5, duration=1 / RATE, step=1 / RATE))
                before = time.monotonic()
                input_lag_s.append(max(0, before - origin - window_end))
                annotation, _ = pipeline([feature])[0]
                inference_s.append(time.monotonic() - before)
                tracks = []
                raw = []
                for segment, _, label in annotation.itertracks(yield_label=True):
                    a, b = round(segment.start * 1000), round(segment.end * 1000)
                    raw.append({"start_ms": a + offset_ms, "end_ms": b + offset_ms, "speaker": label})
                    if b > max(0, a):
                        tracks.append(Span(max(0, a), b, (f"{args.source}:{label}",)))
                end_ms = min(received * 1000 // RATE, round((window_end - args.latency_s + .5) * 1000))
                emit(annotations, {"input_end_ms": round(window_end * 1000) + offset_ms,
                                   "elapsed_s": time.monotonic() - origin, "provisional": True,
                                   "tracks": raw, "inference_s": inference_s[-1]})
                if end_ms > committed_ms:
                    # Diart binarizes at frame midpoints (~8.5 ms from each
                    # step edge); snap only those edges to avoid inventing a
                    # tiny unknown turn every 500 ms. Internal turns survive.
                    for span in partition(committed_ms, end_ms, tracks, edge_tolerance_ms=10):
                        spans.append(span)
                        emit(span_log, {**asdict(span), "start_ms": span.start_ms + offset_ms,
                                        "end_ms": span.end_ms + offset_ms, "source": args.source,
                                        "provisional": True})
                        for ready in turns.feed(span):
                            captions.submit(ready, received * 1000 // RATE)
                    committed_ms = end_ms
            try:
                for block in blocks:
                    predict(block)
                for _ in range(math.ceil(args.latency_s / .5)):
                    if committed_ms >= received * 1000 // RATE:
                        break
                    predict(bytes(STEP * 2), tail=True)
                for ready in turns.flush():
                    captions.submit(ready, received * 1000 // RATE)
                captions.finish()
            except BaseException as exc:
                captions.abort()
                (target / "failure.json").write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}",
                                                                 "received_samples": received,
                                                                 "committed_ms": committed_ms}) + "\n")
                record_action(args.runtime, "streaming-run", run=str(target), status="failed", error=type(exc).__name__)
                raise
            finally:
                blocks.close()
        elapsed = time.monotonic() - origin
        results = captions.results
        short = [r for r in results if r["short_turn"] and r["speakers"]]
        summary = {"status": "completed", "run": str(target), "mode": "live-microphone" if args.microphone else
                   ("unpaced-replay" if args.unpaced else "timed-replay"),
                   "audio_s": received / RATE, "elapsed_s": elapsed, "diart_device": args.device,
                   "qwen_device_requested": args.qwen_device, "captions": len(results),
                   "speakers": sorted({s for span in spans for s in span.speakers}),
                   "speaker_turn_counts": {s: sum(s in r["speakers"] for r in results)
                                           for s in sorted({s for span in spans for s in span.speakers})},
                   "unknown_audio_s": sum(s.end_ms - s.start_ms for s in spans if not s.speakers) / 1000,
                   "overlap_audio_s": sum(s.end_ms - s.start_ms for s in spans if len(s.speakers) > 1) / 1000,
                   "short_labeled_turns": len(short), "short_labeled_empty": sum(not r["text"] for r in short),
                   "empty_captions": sum(not r["text"] for r in results),
                   "diart_rtf": sum(inference_s) / (received / RATE),
                   "qwen_rtf": sum(r["asr_s"] for r in results) / (received / RATE),
                   "diart_step_p95_s": percentile(inference_s, .95),
                   "caption_latency_p50_s": percentile([r["caption_latency_s"] for r in results if r["caption_latency_s"] is not None], .5),
                   "caption_latency_p95_s": percentile([r["caption_latency_s"] for r in results if r["caption_latency_s"] is not None], .95),
                   "caption_start_delay_p50_s": percentile([r["caption_delay_from_start_s"] for r in results if r["caption_delay_from_start_s"] is not None], .5),
                   "caption_start_delay_p95_s": percentile([r["caption_delay_from_start_s"] for r in results if r["caption_delay_from_start_s"] is not None], .95),
                   "queue_wait_max_s": max((r["queue_wait_s"] for r in results), default=0),
                   "input_lag_max_s": max(input_lag_s, default=0),
                   "queue_depth_max": captions.maximum_depth, "queue_blocked_s": captions.blocked_s,
                   "diart_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                   "qwen_peak_rss_bytes": max((r["max_rss_bytes"] for r in results), default=0),
                   "qwen_peak_mps_allocated_bytes": max((r.get("mps_allocated_bytes", 0) for r in results), default=0),
                   "qwen_peak_mps_driver_bytes": max((r.get("mps_driver_bytes", 0) for r in results), default=0),
                   "committed_ms": committed_ms, "no_ground_truth_accuracy_measured": True}
        if args.device == "mps":
            summary["diart_mps_allocated_bytes"] = torch.mps.current_allocated_memory()
        (target / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        record_action(args.runtime, "streaming-run", run=str(target), status="completed")
        print(json.dumps({"summary": summary}), flush=True)
    return target


def main():
    p = parser()
    args = p.parse_args()
    if args.duration_s <= 0 or args.file_offset_s < 0 or args.session_offset_s < 0:
        p.error("duration must be positive; file and session offsets must be nonnegative")
    if args.latency_s < .5 or args.latency_s > 5 or args.latency_s * 2 != round(args.latency_s * 2):
        p.error("latency must be 0.5 through 5 seconds in 0.5 second steps")
    if args.max_turn_s < .001 or args.queue_size < 1 or args.threads < 1:
        p.error("turn duration must be at least 1 ms; queue size and threads must be positive")
    if not (0 < args.tau_active < 1 and 0 < args.rho_update <= 1 and 0 < args.delta_new <= 2):
        p.error("activity/update thresholds must be probabilities; cosine distance must be in (0, 2]")
    if args.microphone and (args.source != "microphone" or args.file_offset_s or args.session_offset_s or args.unpaced):
        p.error("live microphone uses microphone source, zero offsets, and real-time pacing")
    run(args)


if __name__ == "__main__":
    main()
