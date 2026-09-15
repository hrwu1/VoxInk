"""Thin, typed wrappers over the Windows audio catalogues.

The counterpart of `CoreAudioSupport.swift`. Nothing here holds state; each
call reads the system as it is right now. Microphones and loopback endpoints
come from PortAudio through `sounddevice`; audio sessions, the Windows
equivalent of "processes coreaudiod knows about", come from `pycaw`.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .models import CapturableApplication, CaptureError, InputDevice


class MissingDependency(RuntimeError):
    """Raised with an actionable message for an optional runtime dependency."""


def require_sounddevice() -> Any:
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "Audio capture requires 'sounddevice'. Install capture extras "
            "with: python -m pip install -r requirements-capture.txt"
        ) from exc
    return sounddevice


LOOPBACK_SUFFIX = " [Loopback]"
WASAPI_NAME = "Windows WASAPI"


def _preferred_hostapi(sounddevice: Any) -> int | None:
    """The host API whose devices are listed: WASAPI on Windows, where the
    same microphone otherwise appears once per host API; elsewhere the
    default one. None lists every host API."""
    hostapis = list(sounddevice.query_hostapis())
    if sys.platform == "win32":
        for index, hostapi in enumerate(hostapis):
            if hostapi.get("name") == WASAPI_NAME:
                return index
        return None
    try:
        return int(sounddevice.default.hostapi)
    except Exception:  # noqa: BLE001 - an unset default lists everything
        return None


def _devices(sounddevice: Any) -> list[tuple[int, dict[str, Any]]]:
    hostapi = _preferred_hostapi(sounddevice)
    listed = []
    for index, device in enumerate(sounddevice.query_devices()):
        if hostapi is not None and device.get("hostapi") != hostapi:
            continue
        listed.append((index, device))
    return listed


def _is_loopback(device: dict[str, Any]) -> bool:
    return str(device.get("name", "")).endswith(LOOPBACK_SUFFIX)


def input_devices(sounddevice: Any | None = None) -> list[InputDevice]:
    """Microphones the backend can open, default first.

    PortAudio has no stable device identifiers, so the device name stands in
    for the UID; a name that appears twice is suffixed `#2`, `#3`, and so on.
    """
    sounddevice = sounddevice or require_sounddevice()
    try:
        default_index = sounddevice.default.device[0]
    except Exception:  # noqa: BLE001
        default_index = -1
    default_name = None
    if isinstance(default_index, int) and default_index >= 0:
        try:
            default_name = sounddevice.query_devices(default_index).get("name")
        except Exception:  # noqa: BLE001
            default_name = None
    devices: list[InputDevice] = []
    seen: dict[str, int] = {}
    for index, device in _devices(sounddevice):
        if device.get("max_input_channels", 0) <= 0 or _is_loopback(device):
            continue
        name = str(device.get("name", "")) or f"Input {index}"
        seen[name] = seen.get(name, 0) + 1
        uid = name if seen[name] == 1 else f"{name} #{seen[name]}"
        is_default = index == default_index or (default_name is not None and name == default_name and seen[name] == 1)
        devices.append(InputDevice(uid=uid, name=name, is_default=is_default))
    devices.sort(key=lambda item: (not item.is_default, item.name.lower()))
    return devices


def resolve_input_device(uid: str | None, sounddevice: Any | None = None) -> int | None:
    """The PortAudio index for a UID from `input_devices`, or None for the
    default input. A bare device index is accepted as well."""
    if uid is None or not uid.strip():
        return None
    sounddevice = sounddevice or require_sounddevice()
    wanted = uid.strip()
    if wanted.isdecimal():
        index = int(wanted)
        try:
            device = sounddevice.query_devices(index)
        except Exception as exc:  # noqa: BLE001
            raise CaptureError.microphone_device_unavailable(wanted) from exc
        if device.get("max_input_channels", 0) <= 0:
            raise CaptureError.microphone_device_unavailable(wanted)
        return index
    names_seen: dict[str, int] = {}
    for index, device in _devices(sounddevice):
        if device.get("max_input_channels", 0) <= 0 or _is_loopback(device):
            continue
        name = str(device.get("name", "")) or f"Input {index}"
        names_seen[name] = names_seen.get(name, 0) + 1
        candidate = name if names_seen[name] == 1 else f"{name} #{names_seen[name]}"
        if candidate == wanted:
            return index
    raise CaptureError.microphone_device_unavailable(wanted)


def loopback_device(sounddevice: Any | None = None) -> tuple[int, str]:
    """The WASAPI loopback input that mirrors the default output device.

    PortAudio 19.7 and newer expose every WASAPI render endpoint a second time
    as an input named `<endpoint> [Loopback]`; opening it captures what the
    endpoint plays. Returns `(index, endpoint name)`.
    """
    sounddevice = sounddevice or require_sounddevice()
    if sys.platform != "win32":
        raise CaptureError.loopback_unavailable("WASAPI loopback needs Windows")
    try:
        output = sounddevice.query_devices(kind="output")
        output_name = str(output.get("name", ""))
    except Exception as exc:  # noqa: BLE001
        raise CaptureError.loopback_unavailable(f"no default output device ({exc})") from exc
    loopbacks = [(index, device) for index, device in _devices(sounddevice) if _is_loopback(device) and device.get("max_input_channels", 0) > 0]
    if not loopbacks:
        raise CaptureError.loopback_unavailable(
            "PortAudio lists no loopback devices; sounddevice 0.4.4 or newer with PortAudio 19.7 is required"
        )
    for index, device in loopbacks:
        if str(device.get("name", "")) == f"{output_name}{LOOPBACK_SUFFIX}":
            return index, output_name
    # Names are truncated by some drivers; fall back to a prefix match, then to
    # the first loopback there is rather than none.
    for index, device in loopbacks:
        name = str(device.get("name", ""))[: -len(LOOPBACK_SUFFIX)]
        if output_name.startswith(name) or name.startswith(output_name):
            return index, output_name
    index, device = loopbacks[0]
    return index, str(device.get("name", ""))[: -len(LOOPBACK_SUFFIX)]


def require_pycaw() -> Any:
    try:
        from pycaw import utils  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "Listing applications requires 'pycaw'. Install capture extras "
            "with: python -m pip install -r requirements-capture.txt"
        ) from exc
    return utils


def require_psutil() -> Any:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingDependency(
            "Application capture requires 'psutil'. Install capture extras "
            "with: python -m pip install -r requirements-capture.txt"
        ) from exc
    return psutil


def application_identifier(executable: str) -> str:
    """The stable identifier for a process: its executable name, lower-cased."""
    return os.path.basename(executable).strip().lower()


def running_applications(sessions: Any | None = None) -> list[CapturableApplication]:
    """Processes with an audio session on the default output device.

    `sessions` is an iterable of objects with `Process` (a psutil process or
    None) and `ProcessId`, the shape `pycaw.utils.AudioUtilities.GetAllSessions`
    returns; it is looked up there when omitted.
    """
    if sessions is None:
        if sys.platform != "win32":
            raise CaptureError.process_loopback_unavailable("audio sessions can only be listed on Windows")
        sessions = require_pycaw().AudioUtilities.GetAllSessions()
    own = os.getpid()
    applications: dict[int, CapturableApplication] = {}
    for session in sessions:
        process = getattr(session, "Process", None)
        pid = int(getattr(session, "ProcessId", 0) or 0)
        if process is None or pid <= 0 or pid == own:
            continue
        try:
            executable = str(process.name())
        except Exception:  # noqa: BLE001 - the process may be gone already
            continue
        identifier = application_identifier(executable)
        if not identifier:
            continue
        name = executable[:-4] if executable.lower().endswith(".exe") else executable
        applications[pid] = CapturableApplication(name=name, identifier=identifier, process_id=pid)
    return sorted(applications.values(), key=lambda item: (item.name.lower(), item.process_id))


def application_root_processes(identifier: str, processes: Any | None = None) -> list[int]:
    """Process IDs of `identifier` whose parent is not the same executable.

    A browser or meeting client is a tree of processes; process loopback
    captures a target and its children, so the root of each tree is the
    process to target. The current process is never a candidate.
    """
    wanted = application_identifier(identifier)
    if processes is None:
        processes = list(require_psutil().process_iter(["pid", "name", "ppid"]))
    own = os.getpid()
    by_pid: dict[int, tuple[str, int]] = {}
    for process in processes:
        try:
            info = getattr(process, "info", None) or {
                "pid": process.pid, "name": process.name(), "ppid": process.ppid(),
            }
            pid = int(info["pid"])
            by_pid[pid] = (application_identifier(str(info.get("name") or "")), int(info.get("ppid") or 0))
        except Exception:  # noqa: BLE001 - gone, or access denied
            continue
    roots = []
    for pid, (name, parent) in by_pid.items():
        if name != wanted or pid == own:
            continue
        parent_name = by_pid.get(parent, ("", 0))[0]
        if parent_name == wanted:
            continue
        roots.append(pid)
    return sorted(roots)
