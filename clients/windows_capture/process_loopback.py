"""System audio through WASAPI process loopback (Windows 10 build 20348+).

The counterpart of `ProcessTapCapture.swift`. A process loopback intercepts
what a process tree sends to the audio engine, before the endpoint's volume
or mute is applied, so it keeps delivering while the speakers are muted. It
follows the default output device on its own, it can be scoped to one
process tree, so selected-application capture opens one loopback per
application and each one is its own labeled stream ("channel"), and in
exclude mode it captures every process but this one. Endpoint loopback
remains the fallback where it is unavailable.

The COM layer at the bottom of this file is written against the documented
`ActivateAudioInterfaceAsync` contract with `comtypes`; it can only run on
Windows and is exercised there, not by the unit tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import os
import sys
import threading
import time
from typing import Any

from .frame_queue import AudioFrameQueue, AudioFrameSink
from .models import (
    AudioFrame,
    AudioSource,
    CaptureError,
    CaptureStatusEvent,
    StatusLevel,
)
from .pcm_normalizer import PCMNormalizer, require_numpy
from .system_audio import pcm_peak_amplitude
from .windows_audio import application_root_processes

PROCESS_LOOPBACK_MINIMUM_BUILD = 20348

# on_buffer(pcm_bytes, sample_rate, channels): interleaved float32 samples.
BufferHandler = Callable[[bytes, int, int], None]


@dataclass(frozen=True, slots=True)
class ProcessLoopbackChannel:
    """What one loopback listens to and which stream it feeds."""

    # None means every process except this one.
    identifier: str | None
    name: str | None
    stream_id: str


class LoopbackClient:
    """One running WASAPI process-loopback client. `start()` blocks until
    audio is flowing or raises; `stop()` blocks until the capture thread has
    released the audio client."""

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    @property
    def failed(self) -> Exception | None:
        return None


ClientFactory = Callable[[int, bool, BufferHandler], LoopbackClient]


def process_loopback_availability() -> tuple[bool, str]:
    """Whether this machine can open a process loopback, and why not."""
    if sys.platform != "win32":
        return False, "process loopback needs Windows"
    build = getattr(sys.getwindowsversion(), "build", 0)
    if build < PROCESS_LOOPBACK_MINIMUM_BUILD:
        return False, f"process loopback needs Windows build {PROCESS_LOOPBACK_MINIMUM_BUILD} or newer (this is {build})"
    try:
        import comtypes  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False, "the 'comtypes' package is not installed (pip install -r requirements-capture.txt)"
    return True, ""


class ProcessLoopbackCapture:
    MONITOR_INTERVAL_SECONDS = 2.0

    def __init__(
        self,
        channel: ProcessLoopbackChannel,
        session_id: str,
        sample_rate: int,
        sink: AudioFrameSink,
        client_factory: ClientFactory | None = None,
        root_processes: Callable[[str], list[int]] | None = None,
    ) -> None:
        self._channel = channel
        self._session_id = session_id
        self._sink = sink
        self._client_factory = client_factory or WasapiProcessLoopbackClient
        self._root_processes = root_processes or application_root_processes
        self._normalizer = PCMNormalizer(sample_rate)
        self._np = require_numpy()
        self._queue = AudioFrameQueue(sink, session_id, AudioSource.SYSTEM)
        self._client: LoopbackClient | None = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._captured_frames = 0
        self._peak_amplitude = 0
        self._tapped: set[int] = set()
        self._target: int | None = None
        self._needs_rebuild = False
        self._stopping = False
        self._monitor: asyncio.Task[None] | None = None

    @property
    def dropped_frame_count(self) -> int:
        return self._queue.dropped_frame_count

    async def start(self) -> None:
        self._stopping = False
        self._queue.start()
        try:
            await self._rebuild(allow_missing_processes=True)
        except Exception:
            await self._queue.finish_and_drain()
            raise
        self._monitor = asyncio.get_running_loop().create_task(self._monitor_loop(), name=f"monitor-{self._channel.stream_id}")

    async def pause(self) -> None:
        """The loopback stays open across a pause; only delivery stops."""
        await self._queue.finish_and_drain()

    def resume(self) -> None:
        self._queue.start()

    async def stop(self) -> None:
        self._stopping = True
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.cancel()
            try:
                await monitor
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._teardown()
        await self._queue.finish_and_drain()
        with self._lock:
            captured, peak = self._captured_frames, self._peak_amplitude
        self._status(
            StatusLevel.INFO if captured > 0 and peak > 0 else StatusLevel.WARNING,
            "process_loopback_summary",
            f"Process loopback {self._channel.stream_id}: {captured} frames, output peak {peak} of 32768.",
        )

    # MARK: - Loopback lifecycle

    async def _rebuild(self, allow_missing_processes: bool) -> None:
        await self._teardown()
        identifier = self._channel.identifier
        label = self._channel.name or identifier or "all system audio"
        if identifier is not None:
            roots = [pid for pid in self._root_processes(identifier) if pid != os.getpid()]
            if not roots:
                with self._lock:
                    self._tapped = set()
                    self._target = None
                if allow_missing_processes:
                    self._status(StatusLevel.WARNING, "applications_waiting", f"Waiting for {label} to play audio.")
                    return
                raise CaptureError.no_selected_applications({identifier})
            target, include_tree = roots[0], True
            if len(roots) > 1:
                self._status(
                    StatusLevel.INFO, "applications_multiple",
                    f"{label} is running {len(roots)} times; capturing the process tree of pid {target}.",
                )
        else:
            roots = []
            target, include_tree = os.getpid(), False
        client = self._client_factory(target, include_tree, self._on_buffer)
        try:
            await asyncio.to_thread(client.start)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, CaptureError):
                raise
            raise CaptureError.process_loopback_unavailable(str(exc)) from exc
        with self._lock:
            self._client = client
            self._tapped = set(roots)
            self._target = target
            self._needs_rebuild = False
        self._status(StatusLevel.INFO, "process_loopback_started", f"Capturing {label} through a WASAPI process loopback.")

    async def _teardown(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.stop)

    async def _monitor_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.MONITOR_INTERVAL_SECONDS)
            if self._stopping:
                return
            await self._monitor_tick()

    async def _monitor_tick(self) -> None:
        with self._lock:
            client = self._client
            needs_rebuild = self._needs_rebuild
        if needs_rebuild:
            await self._perform_rebuild("an earlier rebuild failed")
            return
        failure = client.failed if client is not None else None
        if failure is not None:
            self._status(StatusLevel.ERROR, "system_capture_stopped", str(failure))
            await self._perform_rebuild("the loopback stopped")
            return
        await self._refresh_processes()

    async def _refresh_processes(self) -> None:
        identifier = self._channel.identifier
        if identifier is None:
            return
        try:
            current = set(pid for pid in self._root_processes(identifier) if pid != os.getpid())
        except Exception as exc:  # noqa: BLE001
            self._status(StatusLevel.ERROR, "application_reconnect_failed", str(exc))
            return
        with self._lock:
            tapped = set(self._tapped)
        if current == tapped:
            return
        self._status(
            StatusLevel.INFO, "applications_changed",
            f"{self._channel.name or identifier} processes changed; rebuilding the loopback.",
        )
        await self._perform_rebuild("the captured processes changed")

    async def _perform_rebuild(self, reason: str) -> None:
        """Rebuild, and leave the session running if it fails: the monitor retries."""
        try:
            await self._rebuild(allow_missing_processes=True)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._needs_rebuild = True
            self._status(
                StatusLevel.WARNING, "loopback_rebuild_failed",
                f"Could not rebuild loopback {self._channel.stream_id} after {reason}: {exc}. Retrying.",
            )

    # MARK: - Audio delivery

    def _on_buffer(self, pcm: bytes, sample_rate: int, channels: int) -> None:
        if not pcm or channels <= 0:
            return
        np = self._np
        try:
            samples = np.frombuffer(pcm, dtype="<f4")
            if channels > 1:
                samples = samples[: samples.size - samples.size % channels].reshape(-1, channels)
            data = self._normalizer.normalize(samples, sample_rate)
        except (CaptureError, ValueError) as exc:
            self._status(StatusLevel.WARNING, "system_frame_dropped", str(exc))
            return
        if not data:
            return
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            self._captured_frames += 1
            self._peak_amplitude = max(self._peak_amplitude, pcm_peak_amplitude(data))
        identifier = self._channel.identifier
        self._queue.submit(AudioFrame(
            session_id=self._session_id,
            stream_id=self._channel.stream_id,
            source=AudioSource.SYSTEM,
            sequence=sequence,
            timestamp_ns=time.monotonic_ns(),
            sample_rate=self._normalizer.output_sample_rate,
            pcm_s16le=data,
            application_identifiers=(identifier,) if identifier is not None else None,
            channel=identifier,
            channel_name=self._channel.name,
        ))

    def _status(self, level: StatusLevel, code: str, message: str) -> None:
        self._sink.send_status(CaptureStatusEvent(
            session_id=self._session_id, source=AudioSource.SYSTEM, level=level, code=code, message=message,
        ))


# MARK: - WASAPI process loopback through comtypes

# Constants from audioclient.h and audioclientactivationparams.h.
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x0002_0000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x0004_0000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VT_BLOB = 0x41
WAVE_FORMAT_IEEE_FLOAT = 3
WAIT_OBJECT_0 = 0

# The format the loopback is asked for. Process loopback has no mix format to
# query; the engine converts to whatever the client declares.
LOOPBACK_SAMPLE_RATE = 48_000
LOOPBACK_CHANNELS = 2
LOOPBACK_BITS = 32


class WasapiProcessLoopbackClient(LoopbackClient):
    ACTIVATION_TIMEOUT_SECONDS = 5.0
    # 100-nanosecond units: a 200 ms engine buffer.
    BUFFER_DURATION_100NS = 2_000_000
    WAIT_MS = 200

    def __init__(self, target_pid: int, include_tree: bool, on_buffer: BufferHandler) -> None:
        self._target_pid = target_pid
        self._include_tree = include_tree
        self._on_buffer = on_buffer
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._failure: Exception | None = None

    @property
    def failed(self) -> Exception | None:
        return self._failure if self._started.is_set() else None

    def start(self) -> None:
        available, reason = process_loopback_availability()
        if not available:
            raise CaptureError.process_loopback_unavailable(reason)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"process-loopback-{self._target_pid}", daemon=True)
        self._thread.start()
        if not self._started.wait(self.ACTIVATION_TIMEOUT_SECONDS):
            self._stop.set()
            raise CaptureError.process_loopback_unavailable("activation timed out")
        if self._failure is not None:
            raise CaptureError.process_loopback_unavailable(str(self._failure))

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self.ACTIVATION_TIMEOUT_SECONDS)

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        try:
            import comtypes  # type: ignore[import-not-found]
            from comtypes import COMObject  # type: ignore[import-not-found]

            api = _WasapiApi.load()
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception as exc:  # noqa: BLE001
            self._failure = exc
            self._started.set()
            return

        audio_client: Any = None
        capture_client: Any = None
        event_handle = None
        kernel32 = ctypes.windll.kernel32
        try:
            # Activation parameters travel as a VT_BLOB PROPVARIANT.
            params = api.AUDIOCLIENT_ACTIVATION_PARAMS()
            params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            params.u.ProcessLoopbackParams.TargetProcessId = self._target_pid
            params.u.ProcessLoopbackParams.ProcessLoopbackMode = (
                PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
                if self._include_tree
                else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
            )
            propvariant = api.PROPVARIANT()
            propvariant.vt = VT_BLOB
            propvariant.blob.cbSize = ctypes.sizeof(params)
            propvariant.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.POINTER(ctypes.c_ubyte))

            class CompletionHandler(COMObject):
                _com_interfaces_ = [api.IActivateAudioInterfaceCompletionHandler, api.IAgileObject]

                def __init__(self) -> None:
                    super().__init__()
                    self.done = threading.Event()
                    self.operation: Any = None

                def ActivateCompleted(self, this: Any, operation: Any) -> int:
                    del this
                    self.operation = operation
                    self.done.set()
                    return 0

            handler = CompletionHandler()
            handler_pointer = ctypes.cast(
                handler._com_pointers_[api.IActivateAudioInterfaceCompletionHandler._iid_], ctypes.c_void_p
            )
            operation = ctypes.c_void_p()
            result = api.activate(
                VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
                ctypes.byref(api.IAudioClient._iid_),
                ctypes.byref(propvariant),
                handler_pointer,
                ctypes.byref(operation),
            )
            if result < 0:
                raise OSError(f"ActivateAudioInterfaceAsync failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")
            if not handler.done.wait(self.ACTIVATION_TIMEOUT_SECONDS):
                raise OSError("ActivateAudioInterfaceAsync did not complete")
            activation = ctypes.cast(operation, ctypes.POINTER(api.IActivateAudioInterfaceAsyncOperation))
            activate_result, activated = activation.GetActivateResult()
            if activate_result < 0:
                raise OSError(f"process loopback activation failed with HRESULT 0x{activate_result & 0xFFFFFFFF:08X}")
            audio_client = activated.QueryInterface(api.IAudioClient)

            wave_format = api.WAVEFORMATEX()
            wave_format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
            wave_format.nChannels = LOOPBACK_CHANNELS
            wave_format.nSamplesPerSec = LOOPBACK_SAMPLE_RATE
            wave_format.wBitsPerSample = LOOPBACK_BITS
            wave_format.nBlockAlign = LOOPBACK_CHANNELS * LOOPBACK_BITS // 8
            wave_format.nAvgBytesPerSec = LOOPBACK_SAMPLE_RATE * wave_format.nBlockAlign
            wave_format.cbSize = 0
            audio_client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                self.BUFFER_DURATION_100NS,
                0,
                ctypes.byref(wave_format),
                None,
            )
            event_handle = kernel32.CreateEventW(None, False, False, None)
            if not event_handle:
                raise OSError("CreateEventW failed")
            audio_client.SetEventHandle(event_handle)
            service = audio_client.GetService(ctypes.byref(api.IAudioCaptureClient._iid_))
            capture_client = service.QueryInterface(api.IAudioCaptureClient)
            audio_client.Start()
            self._started.set()

            block_align = wave_format.nBlockAlign
            while not self._stop.is_set():
                waited = kernel32.WaitForSingleObject(wintypes.HANDLE(event_handle), self.WAIT_MS)
                if waited != WAIT_OBJECT_0:
                    continue
                while True:
                    packet_frames = capture_client.GetNextPacketSize()
                    if packet_frames == 0:
                        break
                    data, frames, flags, _device_position, _qpc_position = capture_client.GetBuffer()
                    try:
                        if frames:
                            length = int(frames) * block_align
                            if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                                pcm = bytes(length)
                            else:
                                pcm = ctypes.string_at(data, length)
                            self._on_buffer(pcm, LOOPBACK_SAMPLE_RATE, LOOPBACK_CHANNELS)
                    finally:
                        capture_client.ReleaseBuffer(frames)
        except Exception as exc:  # noqa: BLE001
            self._failure = exc
            self._started.set()
        finally:
            try:
                if audio_client is not None:
                    audio_client.Stop()
            except Exception:  # noqa: BLE001
                pass
            if event_handle:
                kernel32.CloseHandle(wintypes.HANDLE(event_handle))
            capture_client = None
            audio_client = None
            try:
                comtypes.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


class _WasapiApi:
    """The COM interfaces and structures process loopback needs, built once.

    Declared by hand rather than through a type library because the WASAPI
    headers ship no type library. Vtable order follows audioclient.h and
    mmdeviceapi.h exactly.
    """

    _instance: _WasapiApi | None = None

    @classmethod
    def load(cls) -> _WasapiApi:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # type: ignore[import-not-found]

        class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
            _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", wintypes.DWORD)]

        class _ActivationUnion(ctypes.Union):
            _fields_ = [("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]

        class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
            _fields_ = [("ActivationType", wintypes.DWORD), ("u", _ActivationUnion)]

        class BLOB(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.POINTER(ctypes.c_ubyte))]

        class PROPVARIANT(ctypes.Structure):
            # Only the VT_BLOB arm of the union is needed; the layout matches
            # the 24-byte PROPVARIANT on 64-bit Windows.
            _fields_ = [
                ("vt", wintypes.USHORT),
                ("wReserved1", wintypes.WORD),
                ("wReserved2", wintypes.WORD),
                ("wReserved3", wintypes.WORD),
                ("blob", BLOB),
            ]

        class WAVEFORMATEX(ctypes.Structure):
            _pack_ = 1
            _fields_ = [
                ("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD),
            ]

        class IActivateAudioInterfaceAsyncOperation(IUnknown):
            _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
            _methods_ = [
                COMMETHOD(
                    [], HRESULT, "GetActivateResult",
                    (["out"], ctypes.POINTER(HRESULT), "activateResult"),
                    (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "activatedInterface"),
                ),
            ]

        class IActivateAudioInterfaceCompletionHandler(IUnknown):
            _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
            _methods_ = [
                COMMETHOD(
                    [], HRESULT, "ActivateCompleted",
                    (["in"], ctypes.POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"),
                ),
            ]

        class IAgileObject(IUnknown):
            _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
            _methods_: list[Any] = []

        class IAudioCaptureClient(IUnknown):
            _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
            _methods_ = [
                COMMETHOD(
                    [], HRESULT, "GetBuffer",
                    (["out"], ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), "ppData"),
                    (["out"], ctypes.POINTER(ctypes.c_uint32), "pNumFramesToRead"),
                    (["out"], ctypes.POINTER(wintypes.DWORD), "pdwFlags"),
                    (["out"], ctypes.POINTER(ctypes.c_uint64), "pu64DevicePosition"),
                    (["out"], ctypes.POINTER(ctypes.c_uint64), "pu64QPCPosition"),
                ),
                COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], ctypes.c_uint32, "NumFramesRead")),
                COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], ctypes.POINTER(ctypes.c_uint32), "pNumFramesInNextPacket")),
            ]

        class IAudioClient(IUnknown):
            _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
            _methods_ = [
                COMMETHOD(
                    [], HRESULT, "Initialize",
                    (["in"], wintypes.DWORD, "ShareMode"),
                    (["in"], wintypes.DWORD, "StreamFlags"),
                    (["in"], ctypes.c_longlong, "hnsBufferDuration"),
                    (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                    (["in"], ctypes.POINTER(WAVEFORMATEX), "pFormat"),
                    (["in"], ctypes.POINTER(GUID), "AudioSessionGuid"),
                ),
                COMMETHOD([], HRESULT, "GetBufferSize", (["out"], ctypes.POINTER(ctypes.c_uint32), "pNumBufferFrames")),
                COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsLatency")),
                COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], ctypes.POINTER(ctypes.c_uint32), "pNumPaddingFrames")),
                COMMETHOD(
                    [], HRESULT, "IsFormatSupported",
                    (["in"], wintypes.DWORD, "ShareMode"),
                    (["in"], ctypes.POINTER(WAVEFORMATEX), "pFormat"),
                    (["out"], ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)), "ppClosestMatch"),
                ),
                COMMETHOD([], HRESULT, "GetMixFormat", (["out"], ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
                COMMETHOD(
                    [], HRESULT, "GetDevicePeriod",
                    (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsDefaultDevicePeriod"),
                    (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsMinimumDevicePeriod"),
                ),
                COMMETHOD([], HRESULT, "Start"),
                COMMETHOD([], HRESULT, "Stop"),
                COMMETHOD([], HRESULT, "Reset"),
                COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
                COMMETHOD(
                    [], HRESULT, "GetService",
                    (["in"], ctypes.POINTER(GUID), "riid"),
                    (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "ppv"),
                ),
            ]

        mmdevapi = ctypes.windll.mmdevapi
        activate = mmdevapi.ActivateAudioInterfaceAsync
        activate.restype = ctypes.c_long
        activate.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(GUID),
            ctypes.POINTER(PROPVARIANT),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]

        self.AUDIOCLIENT_ACTIVATION_PARAMS = AUDIOCLIENT_ACTIVATION_PARAMS
        self.PROPVARIANT = PROPVARIANT
        self.WAVEFORMATEX = WAVEFORMATEX
        self.IActivateAudioInterfaceAsyncOperation = IActivateAudioInterfaceAsyncOperation
        self.IActivateAudioInterfaceCompletionHandler = IActivateAudioInterfaceCompletionHandler
        self.IAgileObject = IAgileObject
        self.IAudioCaptureClient = IAudioCaptureClient
        self.IAudioClient = IAudioClient
        self.activate = activate
