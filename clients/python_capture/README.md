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
python -m clients.python_capture --stream-mode frames
```

The interactive commands are `start`, `pause`, `resume`, `stop`, and `quit`.
The default endpoint is
`ws://127.0.0.1:8765/v1/sessions/{session_id}/stream`. Use `--url` to override
it. Run `python -m clients.python_capture --help` for VAD and device settings.

Audio retention is off by default. `--retain-audio` asks the service to save the
captured microphone audio as rotating WAV parts next to the transcript.

Language defaults to automatic detection. `--language en` or `--language zh`
sends a manual override in `stream.start`; omitting the flag omits the field.

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
  "vad": {},
  "retention": {"audio": false, "separate": true, "combined": false},
  "language": "zh"
}
```

`language` is optional. Omit it for automatic detection; `en` and `zh` force a
language for every chunk on the stream.

`retention` configures optional audio saving for the session this stream opens.
It is honoured only by the stream that creates the session, and every stream of
one session should send the same values. `audio` is off by default, so omitting
the block keeps the privacy default. Pass `--retain-audio` to request rotating
WAV parts alongside the transcript.

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
time spent paused. In `frames` mode the service anchors its own voice-activity
chunks to the `start_ms` of the frames it receives, so these values define the
session timeline: they are what keeps a stream that joins late, pauses, or drops
frames aligned with the other source instead of restarting at zero.
`captured_at_ns` is the local monotonic clock reading and is included for
diagnostics and precise ordering only.

Manual controls are JSON text messages:

```json
{"type":"stream.pause","stream_id":"microphone-1","at_ms":1200.0}
{"type":"stream.resume","stream_id":"microphone-1","at_ms":2400.0}
{"type":"stream.stop","stream_id":"microphone-1","at_ms":5000.0}
```

A session may carry several source streams. It pauses once every active stream
has paused and resumes as soon as any one of them resumes, so pausing only the
microphone leaves system capture running. `at_ms` on `stream.resume` is only a
fallback anchor for clients that do not stamp each frame with `start_ms`.

While a session is paused its audio is refused rather than buffered: each frame
is answered with `audio.ignored` so a resume cannot replay stale speech onto the
timeline.

Server messages:

| Message | Meaning |
| --- | --- |
| `stream.ready` | `stream.start` accepted |
| `audio.accepted` | frame consumed, no segment finalized yet |
| `audio.ignored` | frame discarded, with a `reason` such as `session_paused` |
| `transcript.final` | a finalized, deduplicated segment |
| `stream.paused` / `stream.resumed` | control applied, with the resulting `session_state` |
| `stream.lag` | transcription is slower than capture (`real_time_factor`) |
| `stream.stopped` | stream released |
| `error` | `code` and `message`; the stream survives a `transcript_not_stored` error, but `session_unavailable` closes it |

A socket that closes without `stream.stop` is not a stop. The service holds
the session open for a grace period (`stream_grace_seconds`, 15 s by default)
so a client whose connection dropped can send `stream.start` again with the
same `stream_id` and continue; the session is finalized only if nothing rejoins
in time. Once a session has stopped, audio or a final for it is answered with
`session_unavailable` and the socket is closed: nothing after Stop is retained.

Exactly one of `transcript.final`, `audio.accepted`, or `audio.ignored` answers
each `audio.frame`. Overlapping audio that the model transcribes twice is
deduplicated before a segment is persisted, so a chunk that repeats only its
predecessor's overlap is answered with `audio.accepted` and never written.

The client currently prints all server JSON unchanged.

The endpoint must remain bound to `127.0.0.1`; the client is intentionally a
microphone-only adapter, so system-audio capture can remain in the native macOS
component.
