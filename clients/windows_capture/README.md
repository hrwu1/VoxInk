# VoxInk Windows capture

This Python package is the native Windows audio-capture backend, the
counterpart of the Swift package in `clients/macos_capture`. It keeps
microphone and system audio separate, converts both to 16 kHz mono signed
16-bit PCM, labels each frame, and sends frames in capture order to the
WebSocket service bound to this machine's loopback interface. Each source
retains one resampler for the life of the session so the filter keeps its
state across buffers.

It is intentionally independent of Qwen and transcript persistence. The
service owns those concerns. `clients/python_capture` is untouched: it stays
the microphone-only fallback for a checkout without a built backend.

## Requirements

- Windows 10 build 20348 or newer for process loopback (Windows 11 qualifies);
  any Windows 10 for endpoint loopback
- The project's Python 3.12 virtual environment at `.venv`
- The capture extras: `.venv\Scripts\python -m pip install -r requirements-capture.txt`
  (`sounddevice`, `websockets`, `numpy`; on Windows also `comtypes`, `pycaw`,
  and `psutil`)
- A local service listening at the configured WebSocket endpoint

Windows asks no permission for system audio. The microphone follows the
Settings > Privacy > Microphone toggle for desktop apps.

## System audio backends

`--backend auto` (the default) opens a WASAPI process loopback and falls back
to endpoint loopback if the process loopback cannot be created;
`--backend process-loopback` and `--backend loopback` force one. The helper's
names `tap` and `screencapturekit` are accepted as aliases for the same two
roles. A process loopback intercepts what a process tree sends to the audio
engine before the endpoint's volume or mute is applied, so it delivers while
the speakers are muted, follows a change of default output device on its own,
and can isolate one application. With it, every `--app` becomes its own
stream (`system-<exe>`) whose `stream.start` carries `channel` and
`channel_name`, and `--system-all` captures every process except this one.
Endpoint loopback opens the `[Loopback]` input PortAudio exposes for the
default output device: everything the user hears, mixed into one `system-1`
stream, without application isolation.

Applications are identified by executable name (`zoom.exe`), the Windows
counterpart of a bundle identifier; the name is matched case-insensitively
and the root of that executable's process tree is captured with its
children. Identifiers travel in the wire protocol's
`application_bundle_identifiers` and `channel` fields unchanged.

`--list-sources` prints the sources this backend serves: `microphone` and,
on Windows, `system`. Run on macOS or Linux for development it serves the
microphone alone and says so. `--list-apps` lists processes with an audio
session on the default output device, `identifier<TAB>name<TAB>pid=N`;
`--list-devices` lists microphones as `uid<TAB>name[<TAB>default]`, where
the UID is the WASAPI device name (`#2`, `#3` suffixes tell duplicates
apart). `--mic-device UID` picks a microphone, `--language en|zh` fixes the
language the service transcribes in.

The executable is headless: the helper launches it, and the interfaces draw
captions from the helper's status feed.

Run it:

```sh
clients\windows_capture\voxink-windows-capture.cmd --list-apps
clients\windows_capture\voxink-windows-capture.cmd --mic
clients\windows_capture\voxink-windows-capture.cmd --system-all
clients\windows_capture\voxink-windows-capture.cmd --mic --app zoom.exe --app chrome.exe
```

`clients/windows_capture/voxink-windows-capture` (no extension) is the same
program for a POSIX checkout, where it serves only the microphone.

Press Ctrl-C to stop. If none of the selected applications is running,
capture waits and emits an `applications_waiting` status event; a process
that quits and restarts reconnects automatically.

## Backend contract

`voxink-windows-capture` and the Swift `voxink-capture` accept the same
command line, so the helper builds one and runs whichever it was given:

```sh
--list-devices | --list-sources | --list-apps
[--mic] [--mic-device UID] [--system-all | --app EXE ...]
[--backend auto|process-loopback|loopback] [--language en|zh]
[--endpoint ws://127.0.0.1:8300/v1/sessions/ID/stream] [--session-id ID]
[--retain-audio] [--spool-dir DIR]
```

- `VOXINK_AUTH_SECRET` in the environment authenticates the WebSocket; the
  credential never appears on the command line.
- SIGUSR1 pauses, SIGUSR2 resumes, SIGINT or SIGTERM stops, where those
  signals exist. On every platform the lines `pause`, `resume`, and `stop`
  on stdin do the same. Windows cannot send those signals to another process,
  so the helper writes the lines there and keeps signals on macOS; a closed
  stdin is not a stop.
- Finalized captions go to stdout as `[MICROPHONE] text` or `[Zoom] text`;
  status lines go to stderr as `code: message`, which the helper surfaces as a
  notice.
- A refused command line exits 2 with the reason on stderr; a capture that
  cannot start exits 1.

## Local service contract

The default endpoint is `ws://127.0.0.1:8300/v1/sessions/{session_id}/stream`.
`--endpoint` may change its port or path, but the client rejects every
non-loopback hostname and every non-WebSocket scheme.

Each source stream gets an independent WebSocket so microphone and system
state never mix. The connection begins with a `stream.start` JSON message
containing the session, stream, source, full `session_sources` list, 16 kHz
mono PCM description, client metadata (`voxink-windows-capture`), server-side
VAD settings, the selected `application_bundle_identifiers`, and a
`retention` block. Every PCM payload is sent as an `audio.frame` JSON header
immediately followed by a raw binary message; pause, resume, and stop are the
`stream.pause`, `stream.resume`, and `stream.stop` messages, and the client
waits up to two seconds for `stream.stopped` before closing so the service can
finalize its last chunk. These are the same messages and fields the macOS
package documents in `clients/macos_capture/README.md`.

## Frame delivery, ordering, and no-loss guarantees

Every frame passes through one bounded queue per source, drained by a single
task, so headers and payloads never interleave and order is strict. When
transcription falls behind real time the queue holds roughly ten seconds of
audio, then discards its oldest frames; discards are counted and reported at
most once per second as an `audio_frames_dropped` warning.

A frame the service cannot take right now is never dropped. If a stream's
socket dies, the next frame reopens it with a fresh `stream.start`
(`service_reconnecting`, then `service_reconnected` once the service answers
`stream.ready`); a reconnect that fails is retried with exponential backoff
for as long as the recording runs, and the frames captured meanwhile are kept
in memory (about eight minutes of audio) and then written, oldest first, to a
WAV spool file in `--spool-dir` (`<stream>-spool-001.wav`), with
`audio_spooled` statuses naming the file. Only a `session_unavailable` error
from the service, meaning the session is already finalized, ends a stream;
what is still queued goes to the spool then.

## Session controls

`CaptureCoordinator` exposes `start()`, `pause()`, `resume()`, and `stop()`,
and reports `current_state` as `idle`, `recording`, `pausing`, or `paused`.
Pausing stops delivering audio at the capture source and drains what is
already queued before sending `stream.pause`, so the utterance in progress is
finalized rather than truncated. Audio captured while paused is discarded
rather than buffered. The loopbacks stay open across a pause so resuming does
not re-activate them.

## Layout

| Module | Counterpart in `clients/macos_capture` |
| --- | --- |
| `models.py` | `CaptureModels.swift` |
| `frame_queue.py` | `AudioFrameQueue.swift` |
| `backlog.py` | `AudioBacklog.swift` |
| `pcm_normalizer.py` | `PCMNormalizer.swift` |
| `service_client.py` | `LocalServiceClient.swift` |
| `microphone.py` | `MicrophoneCapture.swift` |
| `system_audio.py` | `SystemAudioCapture.swift` |
| `process_loopback.py` | `ProcessTapCapture.swift` |
| `windows_audio.py` | `CoreAudioSupport.swift` |
| `coordinator.py` | `CaptureCoordinator.swift` |
| `cli.py` | `main.swift` |

## Tests

```sh
.venv/bin/python -m unittest discover -s tests/unit/windows_capture -t .
```

The unit tests run on any platform and cover the models, queue, resampler,
service client (against a fake socket), coordinator, control channel, and the
catalogues and process-loopback lifecycle with fakes. The WASAPI layer itself,
`WasapiProcessLoopbackClient` and PortAudio's loopback devices, runs only on
Windows and is exercised there by running the backend against the helper.
