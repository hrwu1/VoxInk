# VoxInk Python microphone capture

This is an experimental, dependency-light microphone adapter for bringing up
the local service. It captures 16-bit mono PCM with `sounddevice`, then sends it
to a WebSocket bound to localhost. Imports and unit tests work without either
runtime dependency installed.

Install the optional runtime packages in your environment:

```sh
python -m pip install sounddevice websockets
```

Run from the repository root:

```sh
python -m apps.python_capture --stream-mode frames
```

The interactive commands are `start`, `pause`, `resume`, `stop`, and `quit`.
The default endpoint is
`ws://127.0.0.1:8765/v1/sessions/{session_id}/stream`. Use `--url` to override
it. Run `python -m apps.python_capture --help` for VAD and device settings.

`frames` mode has the lowest latency and leaves utterance formation to the
service. `vad-chunks` uses the experimental client-side energy VAD, finalizing
after 700 ms of silence or 15 seconds. It includes 200 ms of pre-roll and
overlap by default. These values are deliberately configurable and need tuning
against real English and Chinese audio.

## WebSocket contract (schema version 1)

The client begins with a JSON text message:

```json
{
  "type": "stream.start",
  "schema_version": 1,
  "session_id": "2026-08-25_14-30-00",
  "stream_id": "microphone-1",
  "source": "microphone",
  "audio": {
    "encoding": "pcm_s16le",
    "sample_rate_hz": 16000,
    "channels": 1,
    "frame_duration_ms": 20
  },
  "device": {"id": null, "name": null},
  "client": {"name": "voxink-python-capture", "version": "0.1.0"},
  "stream_mode": "frames",
  "vad": {}
}
```

Each binary message is immediately preceded by one JSON text header. The
binary payload is headerless signed 16-bit little-endian mono PCM and its size
must equal `byte_length`. There is never more than one outstanding header.

Continuous mode uses:

```json
{
  "type": "audio.frame",
  "sequence": 0,
  "stream_id": "microphone-1",
  "start_ms": 0.0,
  "duration_ms": 20.0,
  "captured_at_ns": 123456789,
  "byte_length": 640
}
```

Local-VAD mode uses `audio.chunk` with `start_ms`, `end_ms`, `speech_ms`,
`captured_at_ns`, `byte_length`, and `finalization_reason` (`silence`,
`max_duration`, `pause`, or `stop`).

`start_ms` and `end_ms` are relative to session start and therefore retain
time spent paused; `captured_at_ns` is the local monotonic clock reading and is
included for diagnostics and precise ordering only.

Manual controls are JSON text messages:

```json
{"type":"stream.pause","stream_id":"microphone-1","at_ms":1200.0}
{"type":"stream.resume","stream_id":"microphone-1","at_ms":2400.0}
{"type":"stream.stop","stream_id":"microphone-1","at_ms":5000.0}
```

The server may send `stream.ready`, `transcript.partial`, `transcript.final`,
or `error` JSON events. The client currently prints all server JSON unchanged.

The endpoint must remain bound to `127.0.0.1`; the client is intentionally a
microphone-only adapter, so system-audio capture can remain in the native macOS
component.
