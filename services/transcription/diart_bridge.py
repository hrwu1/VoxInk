"""Bounded async service bridge to the disposable Diart Python environment."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import select
import subprocess
import time
import uuid

from voxink_dev.diart_live_worker import MAX_RESPONSE_BYTES, STEP_BYTES
from voxink_dev.diart_runtime import runtime_environment, validate_runtime, record_action

PROJECT = Path(__file__).resolve().parents[2]
# Native microphone timestamps describe callback delivery, not a sample clock.
# A measured 37-frame capture varied by up to 12 ms from its sample timeline.
# Sequence numbers distinguish replay/loss while this bounded tolerance handles
# callback scheduling jitter. Unsequenced replay input retains its 1 ms check.
NATIVE_TIMESTAMP_TOLERANCE_MS = 32


@dataclass(frozen=True)
class LiveTurn:
    pcm: bytes
    start_ms: int
    end_ms: int
    audio_start_ms: int
    audio_end_ms: int
    speakers: tuple[str, ...]


def availability(runtime_path: Path | str) -> dict:
    """Cheap offline check; actual hashes/model readiness are checked at start."""
    try:
        root = Path(runtime_path).absolute()
        manifest = validate_runtime(root)
        if not os.access(root / ".venv/bin/python", os.X_OK):
            raise ValueError("Diart experiment Python is missing")
        for kind in ("segmentation", "embedding"):
            checkpoint = Path(manifest.get("models", {}).get(kind, {}).get("checkpoint", ""))
            if not checkpoint.is_file() or not checkpoint.resolve().is_relative_to(root):
                raise ValueError(f"Diart {kind} model is missing from its owned runtime")
        if (root / "INSTALL-ACTIONS.jsonl").is_symlink():
            raise ValueError("Runtime action log must not be a symlink")
        return {"available": True, "error": None}
    except (OSError, ValueError, TypeError) as exc:
        return {"available": False, "error": str(exc)}


class DiartBridge:
    def __init__(self, runtime_path: Path | str, *, device: str = "auto", session_id: str, stream_id: str):
        if device not in {"auto", "cpu", "mps"}:
            raise ValueError("Diart device must be auto, cpu or mps")
        self.runtime = Path(runtime_path).absolute()
        self.device, self.session_id, self.stream_id = device, session_id, stream_id
        self.process: subprocess.Popen | None = None
        self.log_path: Path | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._response = bytearray()
        self._pending = bytearray()
        self._pending_start: int | None = None
        self._expected_ms: float | None = None
        self._segment_start_ms: int | None = None
        self._segment_samples = 0
        self._minimum_start_ms = 0
        self._last_frame_sequence: int | None = None
        self.padded_samples = 0
        self._sequence = 0
        self._last_turn_end = 0

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Diart bridge is closed")
        if self.process is not None:
            raise RuntimeError("Diart bridge already started")
        info = availability(self.runtime)
        if not info["available"]:
            raise RuntimeError(info["error"])
        name = f"live-ui-{uuid.uuid4().hex}.stderr.log"
        self.log_path = self.runtime / "logs" / name
        environment = {**runtime_environment(self.runtime), "PYTHONPATH": str(PROJECT),
                       "PYTHONDONTWRITEBYTECODE": "1", "TRANSFORMERS_OFFLINE": "1",
                       "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
        try:
            with self.log_path.open("x") as log:
                self.process = subprocess.Popen([
                    str(self.runtime / ".venv/bin/python"), "-B", "-u", "-m",
                    "voxink_dev.diart_live_worker", "--runtime", str(self.runtime), "--device", self.device,
                ], cwd=PROJECT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=log, bufsize=0)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            record_action(self.runtime, "live-ui-worker", session_id=self.session_id,
                          stream_id=self.stream_id, log=str(self.log_path), pid=self.process.pid, status="started")
            reply = await asyncio.to_thread(self._exchange, None, 180)
            if not reply.get("ready"):
                raise RuntimeError(reply.get("error", "Diart worker did not become ready"))
            self.device = reply.get("device", self.device)
        except BaseException:
            self.close()
            raise

    def _exchange(self, request: dict | None, timeout: float) -> dict:
        process = self.process
        if process is None or self._closed:
            raise RuntimeError("Diart worker is not running")
        deadline = time.monotonic() + timeout
        if request is not None:
            payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
            sent = 0
            while sent < len(payload):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [process.stdin], [], remaining)[1]:
                    raise TimeoutError("Diart worker input timed out")
                sent += os.write(process.stdin.fileno(), payload[sent:])
        while b"\n" not in self._response:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise TimeoutError(f"Diart worker timed out; inspect {self.log_path}")
            chunk = os.read(process.stdout.fileno(), 65_536)
            if not chunk:
                raise RuntimeError(f"Diart worker exited; inspect {self.log_path}")
            self._response.extend(chunk)
            if len(self._response) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Diart worker response exceeded bounded IPC limit")
        line, _, remainder = self._response.partition(b"\n")
        self._response = bytearray(remainder)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise RuntimeError("Diart worker returned an invalid response")
        if "error" in reply:
            raise RuntimeError(f"Diart: {reply['error']}")
        if request is not None and reply.get("id") != request["id"]:
            raise RuntimeError("Diart worker response is out of sequence")
        return reply

    async def _request(self, op: str, **fields) -> list[LiveTurn]:
        self._sequence += 1
        reply = await asyncio.to_thread(self._exchange, {"id": self._sequence, "op": op, **fields}, 30)
        items = reply.get("turns")
        if not isinstance(items, list) or len(items) > 256:
            raise RuntimeError("Diart worker returned an invalid turn list")
        turns = []
        for item in items:
            times = [item[key] for key in ("audio_start_ms", "start_ms", "end_ms", "audio_end_ms")]
            if any(type(t) is not int for t in times) or not (0 <= times[0] <= times[1] < times[2] <= times[3]):
                raise RuntimeError("Diart worker returned invalid source times")
            if times[1] < self._last_turn_end or times[2] - times[1] > 4_000 or times[3] - times[0] > 4_500:
                raise RuntimeError("Diart worker returned duplicated or unbounded turns")
            pcm = base64.b64decode(item["pcm"], validate=True)
            if len(pcm) != (times[3] - times[0]) * 32:
                raise RuntimeError("Diart worker returned PCM inconsistent with its timestamps")
            speakers = item["speakers"]
            if not isinstance(speakers, list) or any(not isinstance(s, str) or not s.startswith("microphone:") for s in speakers):
                raise RuntimeError("Diart worker returned invalid source speaker labels")
            if speakers != sorted(set(speakers)):
                raise RuntimeError("Diart speaker labels must be unique and sorted")
            turns.append(LiveTurn(pcm, times[1], times[2], times[0], times[3], tuple(speakers)))
            self._last_turn_end = times[2]
        return turns

    async def _send_pending(self, *, pad: bool = False) -> list[LiveTurn]:
        if not self._pending:
            return []
        if pad:
            # The native resampler can finish between millisecond boundaries.
            # Keep every real sample; round only this final worker block up,
            # adding at most 15 zero samples (<1 ms) per source segment.
            missing = -len(self._pending) % 32
            self._pending.extend(bytes(missing))
            self.padded_samples += missing // 2
        pcm, start = bytes(self._pending), self._pending_start
        self._pending.clear()
        self._pending_start = None
        return await self._request("feed", pcm=base64.b64encode(pcm).decode("ascii"), start_ms=start)

    async def _flush_segment(self) -> list[LiveTurn]:
        ready = await self._send_pending(pad=True)
        ready.extend(await self._request("flush"))
        if self._segment_start_ms is not None:
            self._minimum_start_ms = self._segment_start_ms + (self._segment_samples + 15) // 16
        self._segment_start_ms = None
        self._segment_samples = 0
        return ready

    async def feed(self, pcm: bytes, start_ms: int, *, sequence: int | None = None) -> list[LiveTurn]:
        """Accept arbitrary native callback sample counts without losing audio.

        Consecutive sequenced frames use the first timestamp plus exact sample
        counts. A sequence gap or a >32 ms forward clock gap flushes context;
        replayed sequences and larger backwards clock changes fail explicitly.
        """
        if type(start_ms) is not int or start_ms < 0:
            raise ValueError("audio start_ms must be a nonnegative integer")
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > STEP_BYTES:
            raise ValueError("audio must be at most 500 ms of mono 16kHz s16 PCM with whole samples")
        if sequence is not None and (type(sequence) is not int or sequence < 0):
            raise ValueError("audio sequence must be a nonnegative integer")
        if self._closed or self.process is None:
            raise RuntimeError("Diart worker is not running")
        if self._lock.locked():
            raise RuntimeError("Diart feeds must be awaited sequentially; concurrent input is not queued")
        async with self._lock:
            try:
                if sequence is not None and self._last_frame_sequence is not None and sequence <= self._last_frame_sequence:
                    raise ValueError("audio sequence was replayed or moved backwards")
                skipped = sequence is not None and self._last_frame_sequence is not None and sequence > self._last_frame_sequence + 1
                tolerance = NATIVE_TIMESTAMP_TOLERANCE_MS if sequence is not None else 1
                if self._expected_ms is not None and start_ms < self._expected_ms - tolerance:
                    raise ValueError("audio timestamps overlap or moved backwards")
                ready = []
                if self._segment_start_ms is not None and (skipped or start_ms > self._expected_ms + tolerance):
                    ready.extend(await self._flush_segment())
                if self._segment_start_ms is None:
                    self._segment_start_ms = max(start_ms, self._minimum_start_ms)
                cursor_samples = self._segment_samples
                remaining = pcm
                while remaining:
                    if self._pending_start is None:
                        self._pending_start = self._segment_start_ms + cursor_samples // 16
                    take = min(STEP_BYTES - len(self._pending), len(remaining))
                    self._pending.extend(remaining[:take])
                    remaining = remaining[take:]
                    cursor_samples += take // 2
                    if len(self._pending) == STEP_BYTES:
                        ready.extend(await self._send_pending())
                self._segment_samples = cursor_samples
                self._expected_ms = self._segment_start_ms + cursor_samples / 16
                if sequence is not None:
                    self._last_frame_sequence = sequence
                return ready
            except BaseException:
                self.close()
                raise

    async def flush(self) -> list[LiveTurn]:
        if self._closed or self.process is None:
            raise RuntimeError("Diart worker is not running")
        if self._lock.locked():
            raise RuntimeError("Diart flush must follow completion of the previous feed")
        async with self._lock:
            try:
                return await self._flush_segment()
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        """Terminate and reap only this child, including a cancelled IPC wait."""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
