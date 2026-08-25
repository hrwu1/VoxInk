# VoxInk macOS capture

This Swift package is the native macOS audio-capture adapter. It keeps microphone
and system audio separate, converts both to 16 kHz mono signed 16-bit PCM, labels
each frame, and sends frames to a WebSocket service bound to this Mac's loopback
interface.

It is intentionally independent of Qwen and transcript persistence. The Python
service owns those concerns.

## Requirements

- macOS 13 or newer
- Xcode/Swift 6 toolchain (the package uses Swift 5 language mode for macOS 13 compatibility)
- Microphone permission when `--mic` is used
- Screen & System Audio Recording permission when system audio is used
- A local service listening at the configured WebSocket endpoint

Build and test:

```sh
cd apps/macos-capture
swift build
swift test
```

The tests use XCTest and require a full Xcode installation. A standalone Command
Line Tools distribution can build the targets but may not include the XCTest SDK
module.

List the currently running applications available to ScreenCaptureKit:

```sh
swift run voxink-capture --list-apps
```

Capture examples:

```sh
swift run voxink-capture --mic
swift run voxink-capture --system-all
swift run voxink-capture --mic --app us.zoom.xos --app com.apple.Safari
```

Press Ctrl-C to stop. Selected applications are tracked by bundle identifier.
The adapter polls ScreenCaptureKit and rebuilds its filter when matching process
IDs change, so apps that quit and restart reconnect automatically. If none of the
selected apps is running, capture waits and emits an `applications_waiting`
status event.

## Local service contract

The default endpoint is
`ws://127.0.0.1:8765/v1/sessions/{session_id}/stream`. `--endpoint` may change
its port or path, but the client rejects every non-loopback hostname and every
non-WebSocket scheme.

Each source stream gets an independent WebSocket so microphone and system state
never mix. The connection begins with a `stream.start` JSON message containing
the session, stream, source, full `session_sources` list, 16 kHz mono PCM
description, client metadata, and server-side VAD settings. Every PCM payload is sent as an `audio.frame` JSON
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

The connection ends with `stream.stop`. The receiver decodes
`transcript.partial` and `transcript.final` messages into `TranscriptWireEvent`;
a host UI can map these directly to `CaptionItem`. Local capture diagnostics are
delivered through a separate status callback because sending them as unknown
WebSocket messages would violate the service contract.

## Permissions and app packaging

The SwiftPM executable is useful for development, but macOS privacy grants are
identity-sensitive. For reliable interactive permission prompts, embed the
`VoxInkCapture` library in the future signed `.app` and add the provided
`Support/Info.plist` usage string and `Support/VoxInkCapture.entitlements`.
ScreenCaptureKit may require restarting the host app after permission is first
granted. The screen/system-audio permission cannot be bypassed programmatically.

System capture excludes the current process twice: from the all-system content
filter and with `SCStreamConfiguration.excludesCurrentProcessAudio`. Selected-app
capture also refuses to include the current process. This prevents feedback when
the eventual VoxInk UI produces sounds.

## Floating caption prototype

`FloatingCaptionPanelController` is an embeddable AppKit/SwiftUI prototype for
the future menu-bar app. It is always on top, can be made click-through, retains
the four newest captions in one chronological stream, replaces partial captions
by stable event ID, and uses textual `MIC`/`SYSTEM` badges in addition to color.
Call `show(_:)` with `event.captionItem` from the sink's transcript callback.
The CLI prints those received transcript events; the panel remains embeddable so
the CLI does not need to take ownership of an AppKit application lifecycle.

## Current scope

This milestone is a capture CLI/library plus a minimal floating-panel prototype, not a complete UI. It does
not mix mic and system tracks or write audio files. Both streams can run at once,
and downstream code can independently persist, transcribe, or combine them.
