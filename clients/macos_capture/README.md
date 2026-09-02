# VoxInk macOS capture

This Swift package is the native macOS audio-capture adapter. It keeps microphone
and system audio separate, converts both to 16 kHz mono signed 16-bit PCM, labels
each frame, and sends frames in capture order to a WebSocket service bound to
this Mac's loopback interface. Each source retains one `AVAudioConverter` for the
life of the session so the resampler keeps its filter state across buffers.

It is intentionally independent of Qwen and transcript persistence. The Python
service owns those concerns.

## Requirements

- macOS 13 or newer; macOS 14.2 or newer for the process-tap backend
- Xcode/Swift 6 toolchain (the package uses Swift 5 language mode for macOS 13 compatibility)
- Microphone permission when `--mic` is used
- System Audio Recording permission for the tap backend, or Screen & System
  Audio Recording permission for the ScreenCaptureKit backend
- A local service listening at the configured WebSocket endpoint

## System audio backends

`--backend auto` (the default) opens a Core Audio process tap and falls back
to ScreenCaptureKit if the tap cannot be created; `--backend tap` and
`--backend screencapturekit` force one. Taps intercept a process's audio
before the output device, so they deliver while the speakers are muted, need
only the narrower System Audio Recording permission
(`NSAudioCaptureUsageDescription`), and can isolate one application. With the
tap backend, every `--app` becomes its own stream (`system-<bundle-id>`) whose
`stream.start` carries `channel` and `channel_name`; ScreenCaptureKit mixes
the selected applications into one `system-1` stream.

Other flags: `--mic-device UID` (from `--list-devices`) picks the microphone,
`--language en|zh` fixes the language the service transcribes in.

## Menu-bar application

`voxink-menubar` (built by `scripts/build-macos-menubar`) is a control
surface over the helper's HTTP API, launched by `voxink-dev --menubar` with
`VOXINK_SERVICE_URL`, `VOXINK_AUTH_SECRET`, and `VOXINK_OUTPUT_ROOT` in its
environment. It starts, pauses, marks, and stops capture, shows the floating
captions, opens the transcript folder, and offers push to talk on ⌃⌥Space
(hold to speak; the finalized text is pasted into the front application when
Accessibility access is granted, else copied). It captures no audio itself.

Build and test:

```sh
./scripts/build-macos-capture
cd clients/macos_capture
swift test
```

Run the build command from the repository root. It wraps the SwiftPM executable
in a background-only development app with a stable bundle identifier and the
privacy usage descriptions required by macOS.

The tests use XCTest and require a full Xcode installation. A standalone Command
Line Tools distribution builds the library and CLI but has no XCTest SDK module,
so `swift test` fails there with `no such module 'XCTest'`.

List the currently running applications through the repository launcher:

```sh
./scripts/voxink-dev --list-apps
```

Capture examples:

```sh
./scripts/voxink-dev --source microphone
./scripts/voxink-dev --source system
./scripts/voxink-dev --source microphone --source applications \
  --app us.zoom.xos --app com.apple.Safari
```

Press Ctrl-C to stop. Selected applications are tracked by bundle identifier.
The adapter polls ScreenCaptureKit and rebuilds its filter when matching process
IDs change, so apps that quit and restart reconnect automatically. If none of the
selected apps is running, capture waits and emits an `applications_waiting`
status event.

The floating caption panel is enabled by default for capture commands. Pass
`--click-through` to let pointer events reach the windows beneath it, or
`--no-captions` for terminal-only operation.

## Local service contract

The default endpoint is
`ws://127.0.0.1:8765/v1/sessions/{session_id}/stream`. `--endpoint` may change
its port or path, but the client rejects every non-loopback hostname and every
non-WebSocket scheme.

Each source stream gets an independent WebSocket so microphone and system state
never mix. The connection begins with a `stream.start` JSON message containing
the session, stream, source, full `session_sources` list, 16 kHz mono PCM
description, client metadata, server-side VAD settings, the selected
`application_bundle_identifiers`, and a `retention` block. Every stream of a
session carries the same application list and retention request, because the
service records whichever `stream.start` arrives first and the microphone socket
usually opens before the system one. Every PCM payload is sent as an `audio.frame` JSON
header immediately followed by a raw binary message:

```json
{
  "type": "audio.frame",
  "stream_id": "system-1",
  "sequence": 42,
  "start_ms": 1200.0,
  "duration_ms": 21.33,
  "captured_at_ns": 391238000000,
  "byte_length": 682
}
```

Manual pause and resume are `stream.pause` and `stream.resume`, each carrying
the `stream_id` and an `at_ms` reading of the session clock:

```json
{"type":"stream.pause","stream_id":"system-1","at_ms":12000.0}
{"type":"stream.resume","stream_id":"system-1","at_ms":41500.0}
```

The connection ends with `stream.stop`, and the client waits up to two seconds
for the service's `stream.stopped` acknowledgement before closing the socket.
The service finalizes its last VAD chunk while stopping, so closing immediately
would discard the final caption of the session. The receiver decodes
`transcript.partial` and `transcript.final` messages into `TranscriptWireEvent`;
a host UI can map these directly to `CaptionItem`. Local capture diagnostics are
delivered through a separate status callback because sending them as unknown
WebSocket messages would violate the service contract.

If a stream's socket dies mid-session, the next frame reopens it with a fresh
`stream.start` and the status callback reports `service_reconnecting`, then
`service_reconnected` once the service answers `stream.ready`. The service
keeps the session open for a grace period, so a prompt reconnect leaves the
transcript whole. Reconnects are bounded (`StreamReconnectPolicy`, three
consecutive attempts): when the budget is spent the stream is abandoned with a
`stream_abandoned` error and later frames are dropped, and a
`session_unavailable` error from the service marks the stream dead at once,
since that session has already been finalized.

## Frame delivery, ordering, and backpressure

Every frame passes through one bounded queue per source, drained by a single
task. Order is strict: the service pairs each JSON header with the binary
message that follows it, so out-of-order delivery would corrupt the stream.

When transcription falls behind real time the queue holds roughly ten seconds of
audio and then discards its oldest frames rather than growing without limit.
Discards are counted, exposed as `CaptureCoordinator.droppedFrameCount`, and
reported at most once per second as an `audio_frames_dropped` warning status
event so the lag is visible instead of silent.

## Session controls

`CaptureCoordinator` exposes `start()`, `pause()`, `resume()`, and `stop()`, and
reports `currentState` as `idle`, `recording`, or `paused`.

Pausing stops delivering audio at the capture source and drains what is already
queued before sending `stream.pause`, so the utterance in progress is finalized
rather than truncated and no audio arrives after the service has paused the
session. Audio captured while paused is discarded rather than buffered: replaying
it after a resume would place it at the wrong point on the session timeline. The
ScreenCaptureKit stream stays open across a pause so resuming does not surrender
the capture filter or re-prompt for permission.

The development CLI still exposes only start and stop, because a pause command
needs interactive stdin that the CLI does not have. Pause and resume are for the
menu-bar app target described in `SHIP_SPEC.md`.

## Permissions and app packaging

macOS privacy grants are identity-sensitive. `./scripts/build-macos-capture`
creates `.build/VoxInkCaptureDev.app` with the stable `dev.voxink.capture`
identifier and the usage descriptions from `Resources/Info.plist`.
ScreenCaptureKit may require restarting the helper after permission is first
granted. The screen/system-audio permission cannot be bypassed programmatically.

System capture excludes the current process twice: from the all-system content
filter and with `SCStreamConfiguration.excludesCurrentProcessAudio`. Selected-app
capture also refuses to include the current process. This prevents feedback when
the eventual VoxInk UI produces sounds.

## Floating caption prototype

`FloatingCaptionPanelController` is an AppKit/SwiftUI prototype used by the CLI
and available to the future menu-bar app. It is always on top, can be made click-through, retains
the four newest captions in one chronological stream, replaces partial captions
by stable event ID, and uses textual `MIC`/`SYSTEM` badges in addition to color.
The CLI prints received transcript events and maps them into the floating panel.

## Current scope

This milestone is a capture CLI/library plus a minimal floating-panel prototype, not a complete UI. It does
not mix mic and system tracks or write audio files. Both streams can run at once,
and downstream code can independently persist, transcribe, or combine them.
