# VoxInk portable microphone capture

This is the microphone capture backend that needs no build and no
platform-specific packages. The transcription helper launches it in place of
the native backend wherever that one cannot serve: on macOS when
`./scripts/build-macos-capture` has not been run, on Windows when the native
Windows backend's packages (`comtypes`, `pycaw`, `psutil`) are not
installed. It captures 16-bit mono PCM with `sounddevice` and streams frames
to the helper's WebSocket on localhost. Imports and unit tests work without
either runtime dependency installed.

It is microphone only. System and per-application audio stay in the native
backends, and this backend says so (`--list-sources` prints `microphone`;
`--system-all` or `--app` exit 2 naming the native backend for the platform).
The helper's `--spool-dir` is accepted and ignored: this backend keeps no
spool, it fails explicitly instead.

Install the optional runtime packages in your environment:

```sh
.venv/bin/python -m pip install -r requirements-capture.txt
```

## Backend contract

`clients/python_capture/voxink-mic-capture` (`voxink-mic-capture.cmd` on
Windows) and the native backends accept the same command line, so the helper
builds one and runs whichever it was given:

```sh
./clients/python_capture/voxink-mic-capture --list-devices     # uid<TAB>name[<TAB>default]
./clients/python_capture/voxink-mic-capture --list-sources     # one supported source per line
./clients/python_capture/voxink-mic-capture --mic --endpoint ws://127.0.0.1:8300/v1/sessions/ID/stream \
    --session-id ID [--mic-device UID] [--language en|zh] [--retain-audio] [--spool-dir DIR]
```

- `VOXINK_AUTH_SECRET` in the environment authenticates the WebSocket; the
  credential never appears on the command line.
- Capture starts immediately. SIGUSR1 pauses, SIGUSR2 resumes, SIGINT or
  SIGTERM stops the stream cleanly and exits 0, where those signals exist. On
  every platform the lines `pause`, `resume`, and `stop` on stdin do the
  same; that is how the helper drives this backend on Windows, where it
  cannot send signals, and there the end of the helper's pipe is also a stop.
  On macOS the helper gives no stdin, and a closed stdin means nothing.
- Pause drains queued and in-flight audio before sending its control message;
  Resume waits for that drain. A frame's JSON header and binary payload cannot
  be split by a control message. Stop also drains audio and keeps receiving
  final captions until `stream.stopped` or a two-second acknowledgment timeout.
- A failed send or unexpected connection close stops accepting audio, reports
  `stream_error`, and exits 1 after cleanup. The portable backend fails explicitly
  instead of reconnecting. Queue drain waits at most five seconds; each
  transport send/close waits at most two seconds. An unacknowledged Stop reports
  `stop_timeout` before closing. These bounds keep failed transport work from
  leaving the capture process waiting indefinitely.
- Finalized captions go to stdout as `[MICROPHONE] text`; status lines go to
  stderr as `code: message`, which the helper surfaces as a notice.
- `--mic-device` takes the `uid` from `--list-devices`. `sounddevice` has no
  stable identifiers, so the device index stands in for one.

Frames are sent as captured; the helper's energy VAD (`voxink_core/vad.py`)
forms utterances, exactly as it does for the native backend. Audio retention
is off unless `--retain-audio` is passed. Language defaults to automatic
detection; `--language en` or `--language zh` sends a manual override in
`stream.start`.

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

This backend prints only finalized transcript text; the helper's status feed
is what the interfaces read.

The endpoint must remain bound to `127.0.0.1`; this backend is intentionally
microphone-only, so system-audio capture can remain in the native macOS
component.
