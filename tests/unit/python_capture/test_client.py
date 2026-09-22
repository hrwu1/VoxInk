from __future__ import annotations

import asyncio
from io import StringIO
import json
import os
from pathlib import Path
import signal
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from clients.python_capture.cli import build_parser, capture_config, main, run, stdin_is_control_pipe
from clients.python_capture.client import CapturedFrame, CaptureConfig, MicrophoneCaptureClient
from voxink_core.models import TranscriptEvent


CONTROL_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "control-contract.json").read_text(encoding="utf-8")
)


def _start_message(**overrides) -> dict:
    config = CaptureConfig(
        websocket_url="ws://127.0.0.1:8300/v1/sessions/demo/stream",
        session_id="demo",
        **overrides,
    )
    return MicrophoneCaptureClient(config)._start_message()


class StartMessageTests(unittest.TestCase):
    def test_retention_is_declared_and_off_by_default(self) -> None:
        message = _start_message()
        self.assertEqual(
            {"audio": False, "separate": True, "combined": False}, message["retention"]
        )

    def test_retention_is_requested_when_the_user_asks_for_it(self) -> None:
        self.assertTrue(_start_message(retain_audio=True)["retention"]["audio"])

    def test_language_stays_absent_for_automatic_detection(self) -> None:
        self.assertNotIn("language", _start_message())
        self.assertEqual("zh", _start_message(language="zh")["language"])

    def test_frames_are_sent_and_chunking_is_left_to_the_helper(self) -> None:
        message = _start_message()
        self.assertEqual("frames", message["stream_mode"])
        self.assertEqual({}, message["vad"])

    def test_only_the_microphone_source_is_accepted(self) -> None:
        with self.assertRaisesRegex(ValueError, "microphone only"):
            CaptureConfig(websocket_url="ws://x", session_id="demo", source="system")


class CommandLineContractTests(unittest.TestCase):
    """The helper builds one command line for either backend."""

    def _config(self, argv: list[str]) -> CaptureConfig:
        return capture_config(build_parser().parse_args(argv))

    def test_the_helpers_command_line_is_accepted(self) -> None:
        config = self._config(
            [
                "--endpoint", "ws://127.0.0.1:4321/v1/sessions/demo/stream",
                "--session-id", "demo", "--retain-audio",
                "--mic-device", "3", "--language", "en", "--backend", "auto", "--mic",
                "--hold-mic-gain",
            ]
        )
        self.assertEqual("ws://127.0.0.1:4321/v1/sessions/demo/stream", config.websocket_url)
        self.assertEqual("demo", config.session_id)
        self.assertTrue(config.retain_audio)
        self.assertEqual(3, config.device)
        self.assertEqual("en", config.language)

    def test_the_credential_comes_from_the_environment_not_the_command_line(self) -> None:
        with patch.dict("os.environ", {"VOXINK_AUTH_SECRET": "per-launch"}):
            self.assertEqual("per-launch", self._config(["--mic"]).auth_token)

    def test_system_sources_are_refused_with_the_native_backend_named(self) -> None:
        for argv in (["--system-all"], ["--mic", "--app", "us.zoom.xos"]):
            with self.assertRaisesRegex(ValueError, "build-macos-capture"):
                self._config(argv)
        with patch("clients.python_capture.cli.sys.platform", "win32"):
            with self.assertRaisesRegex(ValueError, "requirements-capture.txt"):
                self._config(["--system-all"])

    def test_the_helpers_spool_directory_is_accepted_and_ignored(self) -> None:
        config = self._config(["--mic", "--spool-dir", "/tmp/session", "--backend", "auto"])
        self.assertEqual("microphone", config.source)

    def test_list_sources_reports_the_microphone_only(self) -> None:
        with patch("sys.stdout", new=StringIO()) as out:
            self.assertEqual(0, main(["--list-sources"]))
        self.assertEqual(["microphone"], out.getvalue().split())

    def test_list_apps_fails_like_a_system_source(self) -> None:
        with patch("sys.stderr", new=StringIO()) as err:
            self.assertEqual(2, main(["--list-apps"]))
        self.assertIn("native macOS", err.getvalue())

    def test_list_devices_uses_the_native_tab_separated_form(self) -> None:
        devices = [("0", "Built-in Microphone", True), ("2", "USB Mic", False)]
        with (
            patch("clients.python_capture.cli.list_input_devices", return_value=devices),
            patch("sys.stdout", new=StringIO()) as out,
        ):
            self.assertEqual(0, main(["--list-devices"]))
        self.assertEqual(
            ["0\tBuilt-in Microphone\tdefault", "2\tUSB Mic"], out.getvalue().splitlines()
        )

    def test_a_refused_command_line_exits_two_with_the_reason_on_stderr(self) -> None:
        with patch("sys.stderr", new=StringIO()) as err:
            self.assertEqual(2, main(["--system-all"]))
        self.assertIn("native macOS capture helper", err.getvalue())


class SignalContractTests(unittest.TestCase):
    """SIGUSR1 pauses, SIGUSR2 resumes, SIGINT stops: what the helper sends."""

    def test_signals_drive_pause_resume_and_stop(self) -> None:
        client = MagicMock()
        client.config.session_id = "demo"
        client.start = AsyncMock()
        client.pause = AsyncMock()
        client.resume = AsyncMock()
        client.stop = AsyncMock()
        client.failure = None

        async def scenario() -> int:
            client.wait_failed = asyncio.Event().wait
            task = asyncio.ensure_future(run(client))
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGUSR1)
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGUSR2)
            await asyncio.sleep(0.05)
            signal.raise_signal(signal.SIGINT)
            return await asyncio.wait_for(task, timeout=2)

        with patch("builtins.print"):
            code = asyncio.run(scenario())
        self.assertEqual(0, code)
        client.start.assert_awaited_once()
        client.pause.assert_awaited_once()
        client.resume.assert_awaited_once()
        client.stop.assert_awaited_once()


    def test_stdin_lines_drive_pause_resume_and_stop(self) -> None:
        client = MagicMock()
        client.config.session_id = "demo"
        client.start = AsyncMock()
        client.pause = AsyncMock()
        client.resume = AsyncMock()
        client.stop = AsyncMock()
        client.failure = None
        read_end, write_end = os.pipe()
        reader = os.fdopen(read_end, "r")
        writer = os.fdopen(write_end, "w")

        async def scenario() -> int:
            client.wait_failed = asyncio.Event().wait
            task = asyncio.ensure_future(run(client, stdin=reader))
            await asyncio.sleep(0.05)
            writer.write("PAUSE\n")
            writer.flush()
            await asyncio.sleep(0.05)
            writer.write("nonsense\nresume\n")
            writer.flush()
            await asyncio.sleep(0.05)
            writer.write("stop\n")
            writer.flush()
            return await asyncio.wait_for(task, timeout=2)

        try:
            with patch("builtins.print"):
                self.assertEqual(0, asyncio.run(scenario()))
        finally:
            writer.close()
            reader.close()
        client.pause.assert_awaited_once()
        client.resume.assert_awaited_once()
        client.stop.assert_awaited_once()

    def test_a_closed_stdin_stops_only_when_it_is_the_helpers_windows_pipe(self) -> None:
        for stop_on_eof, expected_done in ((True, True), (False, False)):
            with self.subTest(stop_on_eof=stop_on_eof):
                client = MagicMock()
                client.config.session_id = "demo"
                client.start = AsyncMock()
                client.stop = AsyncMock()
                client.failure = None
                read_end, write_end = os.pipe()
                os.close(write_end)
                reader = os.fdopen(read_end, "r")

                async def scenario() -> bool:
                    client.wait_failed = asyncio.Event().wait
                    task = asyncio.ensure_future(run(client, stdin=reader, stop_on_eof=stop_on_eof))
                    await asyncio.sleep(0.1)
                    done = task.done()
                    if not done:
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return done

                try:
                    with patch("builtins.print"):
                        self.assertEqual(expected_done, asyncio.run(scenario()))
                finally:
                    reader.close()
        pipe = MagicMock()
        pipe.isatty.return_value = False
        with patch("clients.python_capture.cli.sys.platform", "win32"):
            self.assertTrue(stdin_is_control_pipe(pipe))
        self.assertFalse(stdin_is_control_pipe(pipe))


class FakeSocket:
    """A transport that can stall/fail writes and deliver service envelopes."""

    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.incoming: asyncio.Queue[str | Exception | None] = asyncio.Queue()
        self.block_kind: str | None = None
        self.fail_kind: str | None = None
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.stop_sent = asyncio.Event()
        self.acknowledge_stop = True
        self.closed = False

    async def send(self, value: str | bytes) -> None:
        kind = json.loads(value)["type"] if isinstance(value, str) else "binary"
        self.sent.append(value)
        if kind == self.fail_kind:
            raise ConnectionError("fake transport failure")
        if kind == self.block_kind:
            self.blocked.set()
            await self.release.wait()
        if kind == "stream.stop":
            self.stop_sent.set()
            if self.acknowledge_stop:
                self.receive({"type": "stream.stopped", "session_id": "demo"})

    def receive(self, event: dict) -> None:
        self.incoming.put_nowait(json.dumps(event))

    def kinds(self) -> list[str]:
        return [json.loads(value)["type"] if isinstance(value, str) else "binary" for value in self.sent]

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)


class CaptureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.socket = FakeSocket()
        self.stream = MagicMock()
        self.sounddevice = MagicMock()
        self.sounddevice.RawInputStream.return_value = self.stream
        self.websockets = MagicMock()
        self.websockets.connect = AsyncMock(return_value=self.socket)
        for dependency, value in (("sounddevice", self.sounddevice), ("websockets", self.websockets)):
            replacement = patch(f"clients.python_capture.client.require_{dependency}", return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = MicrophoneCaptureClient(CaptureConfig(
            websocket_url="ws://127.0.0.1:8300/v1/sessions/demo/stream", session_id="demo",
        ))
        self.client.DRAIN_TIMEOUT_SECONDS = 0.5
        self.client.TRANSPORT_TIMEOUT_SECONDS = 0.5
        self.client.STOP_ACK_TIMEOUT_SECONDS = 0.5
        self.output = StringIO()
        self.errors = StringIO()
        for name, value in (("stdout", self.output), ("stderr", self.errors)):
            replacement = patch(f"sys.{name}", new=value)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def asyncTearDown(self) -> None:
        await self.client.stop()

    def enqueue(self) -> None:
        self.client._enqueue_frame(CapturedFrame(b"\0\0" * 320, time.monotonic_ns()))

    async def test_pause_drains_the_tail_and_queues_a_concurrent_resume(self) -> None:
        self.socket.block_kind = "audio.frame"
        await self.client.start()
        self.enqueue()
        self.enqueue()
        await asyncio.wait_for(self.socket.blocked.wait(), 1)
        pause = asyncio.create_task(self.client.pause())
        await asyncio.sleep(0)
        resume = asyncio.create_task(self.client.resume())
        await asyncio.sleep(0)
        self.assertFalse(pause.done())
        self.assertFalse(resume.done())
        self.socket.release.set()
        await asyncio.wait_for(asyncio.gather(pause, resume), 1)
        self.assertEqual([
            "stream.start", "audio.frame", "binary", "audio.frame", "binary", "stream.pause", "stream.resume",
        ], self.socket.kinds())
        self.assertFalse(self.client.paused)

    async def test_control_cannot_split_a_frame_header_and_payload(self) -> None:
        self.socket.block_kind = "audio.frame"
        await self.client.start()
        frame = asyncio.create_task(self.client._send_frame(CapturedFrame(b"\0\0", time.monotonic_ns())))
        await asyncio.wait_for(self.socket.blocked.wait(), 1)
        control = asyncio.create_task(self.client._send_json({"type": "stream.pause"}))
        await asyncio.sleep(0)
        self.assertFalse(control.done())
        self.socket.release.set()
        await asyncio.wait_for(asyncio.gather(frame, control), 1)
        self.assertEqual(["stream.start", "audio.frame", "binary", "stream.pause"], self.socket.kinds())

    async def test_failed_sender_wakes_the_cli_and_cleans_an_orphaned_queue(self) -> None:
        self.socket.fail_kind = "audio.frame"
        # Queue two frames as soon as the device starts, before the sender runs.
        self.stream.start.side_effect = lambda: (self.enqueue(), self.enqueue())
        self.assertEqual(1, await asyncio.wait_for(run(self.client), 1))
        self.assertFalse(self.client.running)
        self.assertIsInstance(self.client.failure, ConnectionError)
        self.assertTrue(self.socket.closed)
        self.stream.stop.assert_called_once()
        self.stream.close.assert_called_once()
        await asyncio.wait_for(self.client._queue.join(), 0.1)
        self.assertIsNone(self.client._sender_task)
        self.assertIsNone(self.client._receiver_task)

    async def test_unexpected_clean_disconnect_wakes_the_cli(self) -> None:
        self.socket.incoming.put_nowait(None)
        self.assertEqual(1, await asyncio.wait_for(run(self.client), 1))
        self.assertFalse(self.client.running)
        self.assertIn("closed the capture connection", str(self.client.failure))
        self.assertTrue(self.socket.closed)

    async def test_sender_failure_changes_running_state_before_cleanup(self) -> None:
        self.socket.fail_kind = "audio.frame"
        await self.client.start()
        self.enqueue()
        self.enqueue()
        await asyncio.wait_for(self.client.wait_failed(), 1)
        self.assertFalse(self.client.running)
        self.assertFalse(self.client._accept_audio.is_set())
        self.assertIsNotNone(self.client._websocket)
        await asyncio.wait_for(self.client.stop(), 1)
        self.assertNotIn("stream.stop", self.socket.kinds())

    async def test_receive_failure_wakes_the_cli(self) -> None:
        self.socket.incoming.put_nowait(ConnectionError("lost helper"))
        self.assertEqual(1, await asyncio.wait_for(run(self.client), 1))
        self.assertEqual("lost helper", str(self.client.failure))

    async def test_stop_delivers_final_text_before_closing_on_acknowledgment(self) -> None:
        self.socket.acknowledge_stop = False
        await self.client.start()
        stopping = asyncio.create_task(self.client.stop())
        await asyncio.wait_for(self.socket.stop_sent.wait(), 1)
        final = CONTROL_CONTRACT["transcript_final"]
        # Keep the cross-language fixture aligned with the producer's event
        # serialization, including nullable fields and Unicode text.
        self.assertEqual(TranscriptEvent.from_dict(final["event"]).to_dict(), final["event"])
        self.socket.receive(final)
        await asyncio.sleep(0)
        self.assertIn("[MICROPHONE] Shared fixture: hello, 世界.", self.output.getvalue())
        self.assertFalse(stopping.done())
        self.assertFalse(self.socket.closed)
        self.socket.receive({"type": "stream.stopped", "session_id": "demo"})
        await asyncio.wait_for(stopping, 1)
        self.assertTrue(self.socket.closed)
        self.assertIsNone(self.client.failure)

    async def test_missing_stop_acknowledgment_has_a_bounded_wait(self) -> None:
        self.socket.acknowledge_stop = False
        self.client.STOP_ACK_TIMEOUT_SECONDS = 0.02
        await self.client.start()
        await asyncio.wait_for(self.client.stop(), 1)
        self.assertIn("stop_timeout", self.errors.getvalue())
        self.assertTrue(self.socket.closed)
        self.assertFalse(self.client.running)

    async def test_a_stalled_sender_cannot_block_stop_indefinitely(self) -> None:
        self.socket.block_kind = "audio.frame"
        self.client.DRAIN_TIMEOUT_SECONDS = 0.02
        await self.client.start()
        self.enqueue()
        self.enqueue()
        await asyncio.wait_for(self.socket.blocked.wait(), 1)
        await asyncio.wait_for(self.client.stop(), 1)
        self.assertIsInstance(self.client.failure, TimeoutError)
        self.assertTrue(self.socket.closed)
        await asyncio.wait_for(self.client._queue.join(), 0.1)

    async def test_a_stalled_socket_close_is_bounded(self) -> None:
        self.client.TRANSPORT_TIMEOUT_SECONDS = 0.02
        await self.client.start()
        self.socket.close = AsyncMock(side_effect=asyncio.Event().wait)
        await asyncio.wait_for(self.client.stop(), 1)
        self.assertIsInstance(self.client.failure, TimeoutError)
        self.assertIsNone(self.client._websocket)
        self.stream.close.assert_called_once()

    async def test_cli_cancellation_still_closes_capture_resources(self) -> None:
        started = asyncio.Event()
        self.stream.start.side_effect = started.set
        task = asyncio.create_task(run(self.client))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(self.socket.closed)
        self.assertFalse(self.client.running)
        self.stream.close.assert_called_once()

    async def test_start_failure_releases_partially_opened_resources(self) -> None:
        self.stream.start.side_effect = RuntimeError("microphone unavailable")
        with self.assertRaisesRegex(RuntimeError, "microphone unavailable"):
            await self.client.start()
        self.assertFalse(self.client.running)
        self.assertTrue(self.socket.closed)
        self.stream.close.assert_called_once()

    async def test_late_callback_after_pause_does_not_leave_unsent_audio(self) -> None:
        await self.client.start()
        await self.client.pause()
        self.enqueue()
        self.assertTrue(self.client._queue.empty())
        await asyncio.wait_for(self.client._queue.join(), 0.1)


if __name__ == "__main__":
    unittest.main()
