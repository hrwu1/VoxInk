from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from clients.windows_capture.models import CaptureError
from clients.windows_capture.windows_audio import (
    application_root_processes,
    input_devices,
    loopback_device,
    resolve_input_device,
    running_applications,
)


def fake_sounddevice(devices: list[dict], default_input: int = 0, default_output: int | None = None, hostapis=None):
    sounddevice = MagicMock()
    hostapis = hostapis or [{"name": "Windows WASAPI"}]
    sounddevice.query_hostapis.return_value = hostapis
    sounddevice.default.device = [default_input, default_output if default_output is not None else -1]
    sounddevice.default.hostapi = 0

    def query(index=None, kind=None):
        if kind == "output":
            if default_output is None:
                raise ValueError("no output")
            return devices[default_output]
        if index is None:
            return devices
        return devices[index]

    sounddevice.query_devices.side_effect = query
    return sounddevice


class DeviceCatalogueTests(unittest.TestCase):
    DEVICES = [
        {"name": "Headset Mic", "hostapi": 1, "max_input_channels": 1, "default_samplerate": 48000},
        {"name": "Headset Mic", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 48000},
        {"name": "USB Mic", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100},
        {"name": "USB Mic", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100},
        {"name": "Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Speakers [Loopback]", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 48000},
    ]
    HOSTAPIS = [{"name": "Windows WASAPI"}, {"name": "MME"}]

    def test_windows_lists_wasapi_inputs_once_default_first_without_loopbacks(self) -> None:
        sounddevice = fake_sounddevice(self.DEVICES, default_input=0, default_output=4, hostapis=self.HOSTAPIS)
        with patch("clients.windows_capture.windows_audio.sys.platform", "win32"):
            devices = input_devices(sounddevice)
        self.assertEqual(
            [("Headset Mic", "Headset Mic", True), ("USB Mic", "USB Mic", False), ("USB Mic #2", "USB Mic", False)],
            [(device.uid, device.name, device.is_default) for device in devices],
        )

    def test_uids_resolve_to_the_portaudio_index(self) -> None:
        sounddevice = fake_sounddevice(self.DEVICES, default_input=1, default_output=4, hostapis=self.HOSTAPIS)
        with patch("clients.windows_capture.windows_audio.sys.platform", "win32"):
            self.assertIsNone(resolve_input_device(None, sounddevice))
            self.assertEqual(3, resolve_input_device("USB Mic #2", sounddevice))
            self.assertEqual(1, resolve_input_device("Headset Mic", sounddevice))
            self.assertEqual(2, resolve_input_device("2", sounddevice))
            with self.assertRaises(CaptureError):
                resolve_input_device("Unplugged", sounddevice)
            with self.assertRaises(CaptureError):
                resolve_input_device("4", sounddevice)

    def test_the_loopback_twin_of_the_default_output_is_chosen(self) -> None:
        sounddevice = fake_sounddevice(self.DEVICES, default_input=1, default_output=4, hostapis=self.HOSTAPIS)
        with patch("clients.windows_capture.windows_audio.sys.platform", "win32"):
            self.assertEqual((5, "Speakers"), loopback_device(sounddevice))
        with patch("clients.windows_capture.windows_audio.sys.platform", "darwin"):
            with self.assertRaisesRegex(CaptureError, "needs Windows"):
                loopback_device(sounddevice)
        without = fake_sounddevice(self.DEVICES[:5], default_input=1, default_output=4, hostapis=self.HOSTAPIS)
        with patch("clients.windows_capture.windows_audio.sys.platform", "win32"):
            with self.assertRaisesRegex(CaptureError, "no loopback devices"):
                loopback_device(without)


class Process:
    def __init__(self, pid: int, name: str, ppid: int) -> None:
        self.info = {"pid": pid, "name": name, "ppid": ppid}


class ApplicationCatalogueTests(unittest.TestCase):
    def test_audio_sessions_become_applications_keyed_by_executable(self) -> None:
        def session(pid, name):
            item = MagicMock()
            item.ProcessId = pid
            item.Process = MagicMock()
            item.Process.name.return_value = name
            return item

        own = MagicMock()
        own.ProcessId = os.getpid()
        own.Process = MagicMock()
        system_sounds = MagicMock()
        system_sounds.ProcessId = 0
        system_sounds.Process = None
        applications = running_applications([session(10, "Zoom.exe"), session(11, "chrome.exe"), own, system_sounds, session(10, "Zoom.exe")])
        self.assertEqual(
            [("chrome", "chrome.exe", 11), ("Zoom", "zoom.exe", 10)],
            [(item.name, item.identifier, item.process_id) for item in applications],
        )

    def test_root_processes_exclude_children_and_this_process(self) -> None:
        processes = [
            Process(100, "explorer.exe", 1),
            Process(200, "chrome.exe", 100),
            Process(201, "chrome.exe", 200),
            Process(202, "chrome.exe", 200),
            Process(300, "Chrome.exe", 100),
            Process(os.getpid(), "chrome.exe", 100),
        ]
        self.assertEqual([200, 300], application_root_processes("CHROME.EXE", processes))
        self.assertEqual([], application_root_processes("zoom.exe", processes))


if __name__ == "__main__":
    unittest.main()
